"""Tests for ``app/services/social_report_pdf.py`` — the report typesetter.

These tests render REAL PDF bytes from fixture dicts built here (not from
``social_report.py``, so a change in the data assembly cannot mask a rendering
regression) and then read the bytes back with PyMuPDF, which is already a
dependency. Asserting on extracted text and on word coordinates is the only way
to catch the failures that actually matter in a PDF: text that never reached the
page, text that ran off the right margin, numbers that did not right-align, a
long word that overflowed, and non-Latin text that silently turned into a row of
capital "I"s.

Three rules are pinned here because they are the product requirement, not a
detail:

  * a section with ``available=False`` must print its title AND its reason —
    never a blank space, never a placeholder number;
  * a section with ``estimated=True`` must carry a visible AI-ASSESSED chip;
  * every section prints its ``basis`` line.

Runs under pytest and standalone
(``cd backend && python tests/test_social_report_pdf.py``).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from reportlab.graphics.shapes import Drawing  # noqa: E402
from reportlab.platypus import CondPageBreak  # noqa: E402

from app.services import social_report_pdf as pdf  # noqa: E402

try:  # PyMuPDF ships in requirements.txt; the suite still runs without it.
    import fitz  # type: ignore
except Exception:  # pragma: no cover - only when the optional reader is absent
    fitz = None  # type: ignore

needs_reader = pytest.mark.skipif(fitz is None, reason="PyMuPDF not installed")


# --------------------------------------------------------------------------
# Fixtures — CONTRACT-shaped dicts, built by hand.
# --------------------------------------------------------------------------

META: dict[str, Any] = {
    "title": "Social-Listening Signals",
    "brand": "42 Abu Dhabi",
    "mode": "simple",
    "generated_at": "2026-08-20T09:12:00+00:00",
    "window": {"days": 30, "from": "2026-07-21", "to": "2026-08-20"},
    "previous_window": {"from": "2026-06-21", "to": "2026-07-21"},
}

# The real measured volumes, so the cover assertions are about true numbers.
BASIS: dict[str, Any] = {
    "competitor_handles": 2,
    "competitor_posts": 20,
    "news_items": 385,
    "viral_trends": 95,
    "notes": [
        "Shares are not available: the Instagram Graph API does not expose them.",
        "No comment text is stored, so nothing here measures audience sentiment.",
    ],
}

LONG_URL = "https://www.instagram.com/p/" + "A" * 90
LONG_WORD = "z" * 200


def _section(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "s",
        "title": "A Section",
        "available": True,
        "unavailable_reason": None,
        "basis": "Computed from 20 stored posts.",
        "estimated": False,
        "kind": "prose",
        "body": {"paragraphs": ["Some prose."]},
    }
    base.update(over)
    return base


def _report(sections: list[dict[str, Any]], **meta_over: Any) -> dict[str, Any]:
    meta = dict(META)
    meta.update(meta_over)
    return {"meta": meta, "basis": BASIS, "sections": sections}


def _simple_report() -> dict[str, Any]:
    """The owner's sketch: one `signals` section, three groups."""
    return _report(
        [
            _section(
                id="signals",
                title="Signals",
                kind="signals",
                basis="20 competitor posts, 385 news items, 95 viral trends.",
                body={
                    "groups": [
                        {
                            "group": "Competitors / Sentiment",
                            "items": [
                                {
                                    "name": "1337",
                                    "link": LONG_URL,
                                    "extra_links": [],
                                    "metrics": [
                                        {"label": "Likes", "value": "108,398"},
                                        {"label": "Comments", "value": "78,789"},
                                        {
                                            "label": "Shares",
                                            "value": "not available (Instagram API)",
                                        },
                                    ],
                                    "sentiment": {
                                        "label": "Positive",
                                        "estimated": True,
                                    },
                                    "suggestion": "Film three alumni cuts this month.",
                                }
                            ],
                        },
                        {
                            "group": "Industry Signal",
                            "items": [
                                {
                                    "name": "Anthropic",
                                    "link": "https://www.anthropic.com/news",
                                    "extra_links": [
                                        {
                                            "label": "Social",
                                            "url": "https://x.com/anthropicai",
                                        }
                                    ],
                                    "metrics": [
                                        {"label": "Published", "value": "19 Aug 2026"}
                                    ],
                                    "sentiment": None,
                                    "suggestion": None,
                                }
                            ],
                        },
                        {
                            "group": "Viral Social Trends",
                            "items": [
                                {
                                    "name": "Kids taking pictures",
                                    "link": "https://www.tiktok.com/tag/kids",
                                    "extra_links": [],
                                    "metrics": [{"label": "Posts", "value": "12,004"}],
                                    "sentiment": {
                                        "label": "Neutral",
                                        "estimated": True,
                                    },
                                    "suggestion": "Swap the kid for a first-year student.",
                                }
                            ],
                        },
                    ]
                },
            )
        ]
    )


def _all_kinds_report() -> dict[str, Any]:
    """Every `kind` in the contract, plus an unavailable section."""
    return _report(
        [
            _section(
                id="prose", title="Executive Summary", kind="prose", estimated=True,
                basis="Written by the model from 500 stored items.",
                body={"paragraphs": ["First paragraph.", "Second paragraph."]},
            ),
            _section(
                id="kpis", title="Mention Volume", kind="kpis",
                basis="Counted rows in trend_posts.",
                body={"items": [
                    {"label": "Total items", "value": "500", "delta": "+64",
                     "delta_dir": "up", "note": "vs previous 30 days"},
                    {"label": "Competitor posts", "value": "20", "delta": "-3",
                     "delta_dir": "down", "note": "2 handles tracked"},
                    {"label": "Viral trends", "value": "95", "delta": "0",
                     "delta_dir": "flat", "note": None},
                ]},
            ),
            _section(
                id="line", title="Conversation Trend", kind="chart",
                basis="Item counts bucketed weekly by posted_at.",
                body={"chart": "line", "labels": ["21 Jul", "28 Jul", "4 Aug"],
                      "series": [{"name": "News", "values": [61, 84, 96]},
                                 {"name": "Viral", "values": [12, 19, 24]}],
                      "y_label": "Items per week"},
            ),
            _section(
                id="bar", title="Sentiment Analysis", kind="chart", estimated=True,
                basis="AI-assessed from post text - not measured from comments.",
                body={"chart": "bar", "labels": ["Positive", "Neutral", "Negative"],
                      "series": [{"name": "Items", "values": [58, 61, 12]}],
                      "y_label": "Items"},
            ),
            _section(
                id="bullets", title="Emerging Trends", kind="bullets", estimated=True,
                basis="Top general_trends by heat.",
                body={"items": ["Kids taking pictures - 12,004 posts.", LONG_WORD]},
            ),
            _section(
                id="table", title="Top Topics", kind="table", estimated=True,
                basis="Model-assigned topic on 500 items, counted.",
                body={"columns": ["Topic", "Items", "Share"],
                      "rows": [["AI model releases", "112", "22.4%"],
                               ["Student life", "38", "7.6%"],
                               ["Hiring and careers", "31", "6.2%"]],
                      "align": ["left", "right", "right"]},
            ),
            _section(
                id="sov", title="Share of Voice", kind="chart", available=False,
                unavailable_reason=(
                    "Only 2 competitor handles are tracked, so a split across two "
                    "accounts would be a ratio, not a market position."
                ),
                basis="Requires at least 5 tracked handles.",
                body={},
            ),
            _section(
                id="signals", title="Top Content", kind="signals",
                basis="Top posts by engagement.",
                body={"groups": [{"group": "Competitors / Sentiment", "items": [
                    {"name": "1337", "link": "https://instagram.com/p/x",
                     "extra_links": [], "metrics": [{"label": "Likes", "value": "108,398"}],
                     "sentiment": {"label": "Positive", "estimated": True},
                     "suggestion": "Do the alumni cut."}]}]},
            ),
        ],
        mode="full",
        title="Social-Listening Report",
    )


# --------------------------------------------------------------------------
# Reader helpers
# --------------------------------------------------------------------------


def _open(data: bytes):
    return fitz.open(stream=data, filetype="pdf")  # type: ignore[union-attr]


def _text(data: bytes) -> str:
    with _open(data) as doc:
        return "\n".join(page.get_text() for page in doc)


def _flat(data: bytes) -> str:
    """All the text with every run of whitespace collapsed, so an assertion on
    a phrase is not defeated by the line break the typesetter chose."""
    return re.sub(r"\s+", " ", _text(data))


def _pages(data: bytes) -> int:
    with _open(data) as doc:
        return doc.page_count


def _spans(data: bytes, from_page: int = 0) -> list[dict[str, Any]]:
    """Every drawn span with its text, colour and box, from ``from_page`` on."""
    out: list[dict[str, Any]] = []
    with _open(data) as doc:
        for page in list(doc)[from_page:]:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    out.extend(line.get("spans", []))
    return out


def _span_color(data: bytes, needle: str, from_page: int = 0) -> Optional[int]:
    """The colour of the first span containing `needle`.

    Page 0 is in scope. It used to be skipped because the cover sheet carried a
    standing "labelled AI-ASSESSED" disclaimer in muted type that would answer
    colour assertions about the chip; there is no cover sheet any more, and the
    content starts on page one."""
    for span in _spans(data, from_page):
        if needle in span.get("text", ""):
            return int(span.get("color", -1))
    return None


# ==========================================================================
# The bytes themselves
# ==========================================================================


def test_render_returns_a_real_pdf_of_a_plausible_size() -> None:
    data = pdf.render_pdf(_simple_report())
    assert data[:5] == b"%PDF-"
    assert data.rstrip().endswith(b"%%EOF")
    # One page of signals under a masthead. The floor came down when the cover
    # SHEET went: a one-section report is legitimately short now.
    assert 2_000 < len(data) < 400_000


def test_rendering_the_same_input_twice_is_byte_stable() -> None:
    """invariant=1 fixes the internal timestamp and /ID, so a caching layer or
    an ETag over these bytes is safe."""
    report = _all_kinds_report()
    assert pdf.render_pdf(report) == pdf.render_pdf(report)


@needs_reader
def test_page_counts_are_what_we_laid_out() -> None:
    # One page shorter than they were: the cover sheet is a masthead now, and
    # the first section starts under it instead of behind a page break.
    assert _pages(pdf.render_pdf(_simple_report())) == 1
    # And one shorter again since the chart became the strip the screen
    # draws: 74pt of bars instead of 200pt of axes, grid and legend.
    assert _pages(pdf.render_pdf(_all_kinds_report())) == 2


# ==========================================================================
# The masthead, and what is NOT on it
#
# This was a cover SHEET: the title alone on a page, a stamp block of dates, a
# KPI row built from `basis` and the standing caveats. The screen drew none of
# it, so the file and the page it was downloaded from opened differently and a
# reader had no way to tell which one was the report. Both sides now draw the
# same masthead and then the same sections.
# ==========================================================================


@needs_reader
def test_the_masthead_names_the_report_the_brand_and_the_window() -> None:
    data = pdf.render_pdf(_simple_report())
    with _open(data) as doc:
        first = doc[0].get_text()
    assert "Social-Listening Signals" in first
    assert "42 Abu Dhabi" in first
    assert "21 Jul 2026" in first and "20 Aug 2026" in first


@needs_reader
def test_the_first_page_is_the_document_not_a_title_sheet() -> None:
    """The first section starts under the masthead. A cover sheet meant the
    reader turned a page before the report began, and the screen never had
    one."""
    with _open(pdf.render_pdf(_simple_report())) as doc:
        first = doc[0].get_text()
    assert "Social-Listening Signals" in first
    assert "COMPETITORS / SENTIMENT" in first, (
        "the first section's content is on page one, not behind a page break"
    )


@needs_reader
def test_the_masthead_carries_no_counts_of_its_own() -> None:
    """The sample sizes used to be typeset twice from two different sources:
    a KPI row here, built from `basis`, and the AT A GLANCE section, built by
    the section builders. One of them had to go, and the section is the one
    both renderers share."""
    report = _report([_section()])
    report["basis"] = {"competitor_posts": 20, "news_items": 385, "notes": []}
    with _open(pdf.render_pdf(report)) as doc:
        first = doc[0].get_text()
    assert "COMPETITOR POSTS" not in first
    assert "INDUSTRY NEWS ITEMS" not in first


# ==========================================================================
# Sections: every kind, the basis line, the chip, the unavailable reason
# ==========================================================================


@needs_reader
def test_every_kind_reaches_the_page() -> None:
    text = _text(pdf.render_pdf(_all_kinds_report()))
    for title in (
        "Executive Summary",  # prose
        "Mention Volume",  # kpis
        "Conversation Trend",  # chart (line)
        "Sentiment Analysis",  # chart (bar)
        "Emerging Trends",  # bullets
        "Top Topics",  # table
        "Share of Voice",  # unavailable
        "Top Content",  # signals
    ):
        assert title in text, f"section {title!r} missing from the PDF"
    assert "First paragraph." in text and "Second paragraph." in text
    assert "Total items" in text.replace("TOTAL ITEMS", "Total items")
    # The chart draws bars and one caption, exactly as the screen does: no
    # value axis, no grid, no legend, no y-label.
    assert "busiest:" in text
    # Only the first and last labels are printed, under the bars. The one
    # chart the report builds is a time series ("what was collected, day by
    # day"), where the ends are the window and the middle is the shape; the
    # screen prints the same two.
    assert "4 Aug" in text, "the last bucket's label, under the bars"
    assert "AI model releases" in text and "22.4%" in text  # table
    assert "Kids taking pictures" in text  # bullets


@needs_reader
def test_a_section_basis_line_is_not_printed_at_all() -> None:
    """Every section carries a `basis` string, and printing them was ~7,900
    characters of methodology wrapped around the numbers a reader came for:
    first under every heading, then as an appendix at the back once the screen
    stopped showing them.

    Neither renderer prints them now. What a reader needs to judge the
    document is the standing caveats, and those are a SECTION ("What this
    report is based on") that both renderers draw from the same envelope. The
    strings stay in the contract for anyone reading the JSON."""
    text = _text(pdf.render_pdf(_all_kinds_report()))
    for basis in (
        "Written by the model from 500 stored items.",
        "Counted rows in trend_posts.",
        "Item counts bucketed weekly by posted_at.",
        "Model-assigned topic on 500 items, counted.",
    ):
        assert basis not in text, f"basis line still printed: {basis!r}"
    # The section itself is untouched: only its methodology footnote is gone.
    assert "Mention Volume" in text and "Top Topics" in text


@needs_reader
def test_unavailable_section_prints_its_title_and_its_reason() -> None:
    """The product rule: 'Not enough data' with the reason is a SUCCESS."""
    data = pdf.render_pdf(_all_kinds_report())
    text = _text(data)
    assert "Share of Voice" in text
    assert "Not enough data." in text
    assert "Only 2 competitor handles are tracked" in _flat(data)
    assert "would be a ratio, not a market position" in _flat(data)
    # The reason is muted, not body ink.
    colour = _span_color(data, "Only 2 competitor handles")
    assert colour == int(pdf.MUTED.hexval()[2:], 16)


@needs_reader
def test_unavailable_section_without_a_reason_still_says_something() -> None:
    report = _report([
        _section(title="Gone", available=False, unavailable_reason=None, kind="table",
                 body=None)
    ])
    text = _text(pdf.render_pdf(report))
    assert "Gone" in text
    assert "Not enough data." in text
    assert "No reason was recorded." in text


@needs_reader
def test_estimated_sections_carry_the_ai_chip_and_measured_ones_do_not() -> None:
    estimated = pdf.render_pdf(_report([_section(estimated=True)]))
    measured = pdf.render_pdf(_report([_section(estimated=False)]))
    assert "AI-ASSESSED" in _text(estimated)
    assert "AI-ASSESSED" not in _text(measured).replace(
        "labelled AI-ASSESSED", ""  # the cover's standing disclaimer
    )
    # The chip is white type (on the teal pill), not body ink.
    assert _span_color(estimated, "AI-ASSESSED") == 0xFFFFFF


@needs_reader
def test_ai_assessed_tone_is_chipped_inside_a_signal_item() -> None:
    text = _text(pdf.render_pdf(_simple_report()))
    assert "Tone" in text and "Positive" in text
    assert "AI-ASSESSED" in text
    # Shares must be reported as unavailable, never as a zero.
    assert "not available (Instagram API)" in text
    assert not re.search(r"Shares\s+0\b", text)


@needs_reader
def test_a_wrapping_metrics_line_never_breaks_inside_a_value() -> None:
    """Real data ("Shares not available (Instagram API)  ·  Tone Positive")
    overflows one line. Each label/value pair is glued with non-breaking
    spaces so it moves as a unit — but the separators must stay breakable, or
    the whole run becomes one token and reportlab chops it mid-word
    ("Tone Po / sitive"). Both halves of that trade-off are pinned here.
    """
    report = _report([
        _section(title="Wrapping", kind="signals", basis="b", body={"groups": [{
            "group": "Competitors / Sentiment",
            "items": [{
                "name": "@hub71ad - a long competitor post headline",
                "link": "https://instagram.com/p/abc", "extra_links": [],
                "metrics": [
                    {"label": "Likes", "value": "174"},
                    {"label": "Comments", "value": "8"},
                    {"label": "Engagement", "value": "182"},
                    {"label": "vs this account's own median", "value": "3.4x"},
                    {"label": "Shares", "value": "not available (Instagram API)"},
                ],
                "sentiment": {"label": "Positive", "estimated": True},
                "suggestion": None,
            }],
        }]})
    ])
    flat = _flat(pdf.render_pdf(report))
    for intact in (
        "Likes 174",
        "Comments 8",
        "Engagement 182",
        "not available (Instagram API)",
        "Tone Positive",
    ):
        assert intact in flat, f"{intact!r} was broken across lines: {flat!r}"


@needs_reader
def test_unknown_kind_degrades_without_losing_the_section() -> None:
    report = _report([_section(title="Weird", kind="sparkline", body={})])
    text = _text(pdf.render_pdf(report))
    assert "Weird" in text
    assert "Unsupported section kind" in text


@needs_reader
def test_a_body_that_explodes_does_not_lose_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(body: Any, width: float) -> list[Any]:
        raise RuntimeError("bad body")

    monkeypatch.setitem(pdf._RENDERERS, "prose", boom)
    text = _text(pdf.render_pdf(_report([_section(title="Explodes"), _section(title="Survives")])))
    assert "Explodes" in text and "Survives" in text
    assert "could not be rendered" in text


# ==========================================================================
# Layout: margins, alignment, page furniture, links
# ==========================================================================


@needs_reader
def test_nothing_overflows_the_right_margin_even_a_200_char_word() -> None:
    """splitLongWords is what keeps a 200-character token inside the frame; if
    it ever regresses the word runs off the page instead of wrapping."""
    data = pdf.render_pdf(_all_kinds_report())
    limit = pdf.PAGE_SIZE[0] - pdf.MARGIN_X + 1.0
    with _open(data) as doc:
        for number, page in enumerate(doc, start=1):
            for word in page.get_text("words"):
                assert word[2] <= limit, (
                    f"page {number}: {word[4]!r} ends at {word[2]:.1f} > {limit:.1f}"
                )
    assert LONG_WORD[:40] in _text(data).replace("\n", "")


@needs_reader
def test_numeric_columns_are_right_aligned() -> None:
    data = pdf.render_pdf(_all_kinds_report())
    ends: dict[str, float] = {}
    with _open(data) as doc:
        for page in doc:
            for word in page.get_text("words"):
                if word[4] in ("112", "38", "31"):
                    ends[word[4]] = word[2]
    assert len(ends) == 3, f"expected the three Items values, got {ends}"
    assert max(ends.values()) - min(ends.values()) < 1.0


@needs_reader
def test_body_pages_carry_a_running_header_and_a_real_page_n_of_m() -> None:
    data = pdf.render_pdf(_all_kinds_report())
    with _open(data) as doc:
        total = doc.page_count
        # Page one is numbered like every other page now: it is the first page
        # of the document rather than a cover sheet in front of it. What it
        # does NOT carry is the running header, because the masthead is there.
        assert f"Page 1 of {total}" in doc[0].get_text()
        assert "21 Jul 2026 – 20 Aug 2026" in doc[0].get_text(), "masthead window"
        for number in range(2, total + 1):
            page_text = doc[number - 1].get_text()
            assert f"Page {number} of {total}" in page_text
            assert "Social-Listening Report" in page_text  # running header
            assert "21 Jul 2026" in page_text  # running window


@needs_reader
def test_a_section_title_is_never_the_last_thing_on_a_page() -> None:
    """CondPageBreak(96) + KeepTogether on the head block. A title alone at the
    bottom is the classic widow this guards against."""
    titles = {s["title"] for s in _all_kinds_report()["sections"]}
    with _open(pdf.render_pdf(_all_kinds_report())) as doc:
        for page in doc:
            lines = [ln for ln in page.get_text().splitlines() if ln.strip()]
            # Drop the footer (mono stamp + page number) before looking.
            tail = [ln for ln in lines[-3:] if "Page " not in ln and "social listening" not in ln]
            for line in tail:
                assert line.strip() not in titles, f"widow title {line!r}"


@needs_reader
def test_a_long_link_is_shortened_on_the_page_but_whole_in_the_annotation() -> None:
    data = pdf.render_pdf(_simple_report())
    text = _text(data)
    assert LONG_URL not in text  # not printed in full
    assert "instagram.com/" in text
    assert "…" in text or "..." in text
    with _open(data) as doc:
        uris = {link.get("uri") for page in doc for link in page.get_links()}
    assert LONG_URL in uris, f"full URL lost from the link annotations: {uris}"


@needs_reader
def test_kpi_deltas_are_coloured_by_direction() -> None:
    data = pdf.render_pdf(_all_kinds_report())
    assert _span_color(data, "+64") == int(pdf.SUCCESS.hexval()[2:], 16)
    assert _span_color(data, "-3") == int(pdf.DANGER.hexval()[2:], 16)


@needs_reader
def test_a_sixty_row_table_spans_pages_and_repeats_its_header() -> None:
    rows = [[f"@acct{i}", str(i), f"{i * 1234:,}"] for i in range(60)]
    report = _report([
        _section(title="Handles", kind="table",
                 body={"columns": ["Handle", "Posts", "Engagement"], "rows": rows,
                       "align": ["left", "right", "right"]})
    ])
    data = pdf.render_pdf(report)
    assert _pages(data) >= 3
    with _open(data) as doc:
        with_header = sum(1 for page in doc if "Engagement" in page.get_text())
    assert with_header >= 2, "repeatRows=1 should reprint the header on each page"
    text = _text(data)
    assert "@acct0" in text and "@acct59" in text


# ==========================================================================
# Unicode
# ==========================================================================


def test_sanitize_transliterates_arabic_instead_of_dropping_it() -> None:
    assert pdf.sanitize_text("نانو") == "nanw"
    assert pdf.sanitize_text("أطفال") == "atfal"
    assert pdf.sanitize_text("١٢٣") == "123"  # Arabic-Indic digits


def test_sanitize_strips_emoji_and_falls_back_when_nothing_is_left() -> None:
    assert pdf.sanitize_text("Kids taking pictures 📸") == "Kids taking pictures"
    assert pdf.sanitize_text("🔥🔥🔥", "(untitled)") == "(untitled)"


def test_sanitize_keeps_what_cp1252_can_actually_draw() -> None:
    # Real titles from general_trends: em dash, acute accents.
    assert pdf.sanitize_text("MONTAGEM DELÍRIO — ATLXS") == "MONTAGEM DELÍRIO — ATLXS"
    assert pdf.sanitize_text("Ela ké Leitada") == "Ela ké Leitada"
    assert pdf.sanitize_text("don’t") == "don’t"  # curly apostrophe IS cp1252


def test_sanitize_degrades_latin_extended_rather_than_dropping_the_letter() -> None:
    assert pdf.sanitize_text("Erdős") == "Erdos"
    assert pdf.sanitize_text("Ōsaka") == "Osaka"


def test_sanitize_handles_none_numbers_and_control_characters() -> None:
    assert pdf.sanitize_text(None) == ""
    assert pdf.sanitize_text(None, "-") == "-"
    assert pdf.sanitize_text(42) == "42"
    assert pdf.sanitize_text("a ​b") == "a b"
    assert pdf.sanitize_text("a\nb\tc") == "a b c"
    assert pdf.sanitize_text("   ") == ""


@needs_reader
def test_arabic_renders_as_letters_not_as_a_row_of_capital_i() -> None:
    """The measured failure mode of the core fonts: every unencodable char is
    drawn as "I". If this regresses the page fills with IIIII."""
    report = _report([
        _section(title="نانو — TUL8TE", kind="signals", basis="masdr",
                 body={"groups": [{"group": "المنافسون", "items": [
                     {"name": "أطفال يلتقطون الصور 📸", "link": None,
                      "extra_links": [], "metrics": [{"label": "إعجابات", "value": "١٢٣"}],
                      "sentiment": None, "suggestion": None}]}]})
    ])
    text = _text(pdf.render_pdf(report))
    assert "nanw" in text
    assert "atfal yltqtwn alswr" in text
    assert "123" in text
    assert "IIII" not in text


# ==========================================================================
# Pathological inputs
# ==========================================================================


@needs_reader
def test_an_empty_sections_list_says_so_rather_than_faking_content() -> None:
    data = pdf.render_pdf(_report([]))
    assert _pages(data) == 1
    text = _text(data)
    assert "No sections" in text
    assert "Nothing has been inferred" in text


@pytest.mark.parametrize(
    "report",
    [
        {},
        None,
        [1, 2, 3],
        {"meta": None, "basis": None, "sections": None},
        {"meta": {"window": None}, "basis": {"notes": None}, "sections": ["x", 7, None]},
    ],
)
def test_render_survives_a_malformed_report(report: Any) -> None:
    data = pdf.render_pdf(report)  # type: ignore[arg-type]
    assert data[:5] == b"%PDF-"


def test_render_survives_none_where_every_string_was_expected() -> None:
    report = {
        "meta": {"title": None, "brand": None, "mode": None, "generated_at": None,
                 "window": {"days": None, "from": None, "to": None},
                 "previous_window": None},
        "basis": {"competitor_handles": None, "notes": [None, ""]},
        "sections": [
            {"id": None, "title": None, "available": True, "unavailable_reason": None,
             "basis": None, "estimated": None, "kind": "signals",
             "body": {"groups": [{"group": None, "items": [
                 {"name": None, "link": None, "extra_links": None, "metrics": None,
                  "sentiment": None, "suggestion": None}]}]}},
        ],
    }
    data = pdf.render_pdf(report)
    assert data[:5] == b"%PDF-"
    if fitz is not None:
        text = _text(data)
        assert "(untitled)" in text  # the name placeholder, not a blank line


@pytest.mark.parametrize(
    "body",
    [
        {"chart": "bar", "labels": [], "series": []},
        {"chart": "line", "labels": ["a", "b"], "series": [{"name": "s", "values": []}]},
        {"chart": "bar", "labels": ["a"], "series": [{"name": "s", "values": ["nope"]}]},
        {"chart": "bar", "labels": ["a", "b"], "series": [{"name": "s", "values": [-5, -12]}]},
        {"chart": "bar", "labels": [f"L{i}" for i in range(24)],
         "series": [{"name": "s", "values": list(range(24))}]},
        {"chart": "line", "labels": ["a", "b", "c"],
         "series": [{"name": "s", "values": [1, 2]}]},  # short series is padded
    ],
)
def test_charts_survive_awkward_data(body: dict[str, Any]) -> None:
    data = pdf.render_pdf(_report([_section(title="Chart", kind="chart", body=body)]))
    assert data[:5] == b"%PDF-"


def test_ragged_table_rows_are_squared_off() -> None:
    body = {
        "columns": ["A", "B", "C"],
        "rows": [["1"], ["1", "2", "3", "4"], [None, None, None]],
        "align": ["right"],
    }
    data = pdf.render_pdf(_report([_section(title="Ragged", kind="table", body=body)]))
    assert data[:5] == b"%PDF-"


# ==========================================================================
# suggested_filename
# ==========================================================================


def test_suggested_filename_uses_the_mode_and_the_generated_date() -> None:
    assert (
        pdf.suggested_filename(_all_kinds_report())
        == "42-social-listening-full-2026-08-20.pdf"
    )
    assert (
        pdf.suggested_filename(_simple_report())
        == "42-social-listening-simple-2026-08-20.pdf"
    )


def test_suggested_filename_falls_back_to_the_window_end() -> None:
    report = _report([], generated_at=None)
    assert pdf.suggested_filename(report).endswith("2026-08-20.pdf")


def test_suggested_filename_is_always_ascii_and_path_safe() -> None:
    nasty = {
        "meta": {
            "mode": "../../etc/passwd نانو 🔥",
            "generated_at": "2026-08-20T00:00:00Z",
        }
    }
    name = pdf.suggested_filename(nasty)
    assert name.isascii()
    assert "/" not in name and "\\" not in name and ".." not in name
    assert name.endswith(".pdf")
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name


def test_suggested_filename_survives_a_missing_meta() -> None:
    name = pdf.suggested_filename({})
    assert name.startswith("42-social-listening-report-")
    assert name.endswith(".pdf")
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name)


# ==========================================================================
# A reader-free safety net: prove the critical strings are typeset even if no
# PDF text extractor is installed on the machine running the suite.
# ==========================================================================


def test_reason_and_chip_are_typeset_even_without_a_pdf_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    real_paragraph = pdf.Paragraph

    def spy(text: str, *args: Any, **kwargs: Any) -> Any:
        captured.append(text)
        return real_paragraph(text, *args, **kwargs)

    monkeypatch.setattr(pdf, "Paragraph", spy)
    pdf.render_pdf(_all_kinds_report())

    blob = "\n".join(captured)
    assert "AI-ASSESSED" in blob
    assert "Not enough data." in blob
    assert "Only 2 competitor handles are tracked" in blob
    # The section's own basis footnote is not typeset any more; the REASON a
    # section is empty still is, because that is the finding.
    assert "Written by the model from 500 stored items." not in blob


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# A column may never be squeezed narrower than a word it has to hold
# ---------------------------------------------------------------------------

def test_a_column_is_never_narrower_than_its_longest_word():
    """Found on a live 7-column Emerging Trends table: the uniform scale-down
    that keeps a wide table inside the frame pushed the "Type (AI)" column
    under the width of "culture", and reportlab broke the word across two
    lines as "cultur" / "e". A reader reads that as corrupted data."""
    columns = ["Trend", "Type (AI)", "Heat", "Growth reported by source",
               "Seen in", "Sources / evidence", "First seen"]
    rows = [[
        "MONTAGEM GUERREIRO (Slowed) sound", "culture", "0.46 of 1.00 (rising)",
        "not reported", "AE, SA, US", "2 / 2", "20 Aug",
    ]]
    widths = pdf._column_widths(columns, rows, pdf.CONTENT_WIDTH)
    assert len(widths) == len(columns)
    assert abs(sum(widths) - pdf.CONTENT_WIDTH) < 0.5
    for idx, column in enumerate(columns):
        longest = max(
            pdf._widest_word(column, pdf.FONT_BOLD, 7.6),
            pdf._widest_word(rows[0][idx], pdf.FONT, 8.6),
        )
        cap = pdf.CONTENT_WIDTH * pdf._MAX_COL_FRACTION
        # Either the word fits, or the column is at the per-column cap and no
        # width would have fitted it.
        assert widths[idx] + 0.5 >= min(longest, cap), (
            f"column {column!r} is {widths[idx]:.1f}pt but must hold a "
            f"{longest:.1f}pt word"
        )


def test_the_rendered_emerging_trends_table_does_not_break_words():
    """The end-to-end version of the check above, read back off the page."""
    report = {
        "meta": {"title": "T", "brand": "B", "mode": "full",
                 "generated_at": "2026-08-20T00:00:00+00:00",
                 "window": {"days": 30, "from": "2026-07-21", "to": "2026-08-20"},
                 "previous_window": {"from": "2026-06-21", "to": "2026-07-21"}},
        "basis": {"notes": []},
        "sections": [{
            "id": "emerging_trends", "title": "Emerging Trends",
            "available": True, "unavailable_reason": None,
            "basis": "Measured heat and growth.", "estimated": True,
            "kind": "table",
            "body": {
                "columns": ["Trend", "Type (AI)", "Heat",
                            "Growth reported by source", "Seen in",
                            "Sources / evidence", "First seen"],
                "rows": [["Udin Din Din Dun AI meme", "culture",
                          "0.46 of 1.00 (rising)", "not reported", "global",
                          "2 / 2", "20 Aug"],
                         ["MONTAGEM GUERREIRO (Slowed) sound", "sound",
                          "0.23 of 1.00 (warm)", "1.70x", "AE, SA, US",
                          "1 / 3", "20 Aug"]],
                "align": ["left", "left", "left", "right", "left", "right",
                          "right"],
            },
        }],
    }
    text = _text(pdf.render_pdf(report))
    assert "culture" in text
    assert "sound" in text
    # The exact split the bug produced.
    assert "cultur\ne" not in text
    assert "soun\nd" not in text


def test_a_chart_is_not_separated_from_its_own_heading():
    """A chart is one indivisible Drawing. Reserving only the head block's
    height let the heading and basis line sit at the foot of a page with 250pt
    of white space under them and the chart overleaf."""
    filler = {
        "id": "filler", "title": "Filler", "available": True,
        "unavailable_reason": None, "basis": "Measured.", "estimated": False,
        "kind": "prose",
        "body": {"paragraphs": ["Body text. " * 40] * 6},
    }
    chart = {
        "id": "conversation_trend", "title": "Conversation Trend",
        "available": True, "unavailable_reason": None,
        "basis": "Item counts per day.", "estimated": False, "kind": "chart",
        "body": {"chart": "line", "labels": [str(i) for i in range(1, 31)],
                 "series": [{"name": "Industry Signal",
                             "values": [float(i) for i in range(30)]}],
                 "y_label": "items"},
    }
    report = {
        "meta": {"title": "T", "brand": "B", "mode": "full",
                 "generated_at": "2026-08-20T00:00:00+00:00",
                 "window": {"days": 30, "from": "2026-07-21", "to": "2026-08-20"},
                 "previous_window": {"from": "2026-06-21", "to": "2026-07-21"}},
        "basis": {"notes": []},
        "sections": [filler, chart],
    }
    data = pdf.render_pdf(report)
    with _open(data) as doc:
        heading_page = next(
            i for i, page in enumerate(doc)
            if "Conversation Trend" in page.get_text()
        )
        # The caption is drawn with the bars, so it lands on the chart's page.
        chart_page = next(
            i for i, page in enumerate(doc) if "busiest:" in page.get_text()
        )
    assert heading_page == chart_page, (
        f"heading landed on page {heading_page + 1} and the chart on "
        f"page {chart_page + 1}"
    )


def test_the_chart_reserve_can_never_exceed_the_page():
    """A CondPageBreak taller than the frame can never be satisfied, so it
    would break every page forever."""
    tall = Drawing(pdf.CONTENT_WIDTH, pdf.FRAME_HEIGHT * 4)
    assert pdf._indivisible_height([tall], pdf.CONTENT_WIDTH) > pdf.FRAME_HEIGHT
    # The flow builder clamps it.
    section = {
        "id": "s", "title": "S", "available": True, "unavailable_reason": None,
        "basis": "b", "estimated": False, "kind": "chart",
        "body": {"chart": "bar", "labels": ["a"],
                 "series": [{"name": "n", "values": [1.0]}], "y_label": "y"},
    }
    flow = pdf._section(section, 1, 1, pdf.CONTENT_WIDTH)
    breaks = [f for f in flow if isinstance(f, CondPageBreak)]
    assert breaks and all(b.height <= pdf.FRAME_HEIGHT for b in breaks)


def test_a_heading_is_never_stranded_above_its_own_table():
    """The fixed 96pt reserve was tuned for a one-line basis. These basis lines
    run to five lines, so the head fitted at the foot of a page while the table
    went overleaf, leaving a title and a paragraph pointing at nothing."""
    long_basis = (
        "7 of 80 trend(s) first seen in 21 Jul 2026 - 20 Aug 2026 are "
        "accelerating: heat at or above 0.45 on the measured 0-1 scale, or a "
        "source reporting growth of 1.2x or more against its own previous "
        "reading. Heat and growth are measured from each source's counters, "
        "rank and content age; the Type column is an AI label. 'Seen in' lists "
        "the region codes the source itself reported the trend in - it is the "
        "only geography we hold."
    )
    sections = [
        {"id": f"f{i}", "title": f"Filler {i}", "available": True,
         "unavailable_reason": None, "basis": "Measured.", "estimated": False,
         "kind": "prose", "body": {"paragraphs": ["Body text. " * 30] * 3}}
        for i in range(3)
    ]
    sections.append({
        "id": "emerging_trends", "title": "Emerging Trends", "available": True,
        "unavailable_reason": None, "basis": long_basis, "estimated": True,
        "kind": "table",
        "body": {"columns": ["Trend", "Heat"],
                 "rows": [["Udin Din Din Dun AI meme", "0.46 of 1.00"],
                          ["MONTAGEM GUERREIRO sound", "0.23 of 1.00"]],
                 "align": ["left", "right"]},
    })
    report = {
        "meta": {"title": "T", "brand": "B", "mode": "full",
                 "generated_at": "2026-08-20T00:00:00+00:00",
                 "window": {"days": 30, "from": "2026-07-21", "to": "2026-08-20"},
                 "previous_window": {"from": "2026-06-21", "to": "2026-07-21"}},
        "basis": {"notes": []},
        "sections": sections,
    }
    with _open(pdf.render_pdf(report)) as doc:
        heading_page = next(i for i, p in enumerate(doc)
                            if "Emerging Trends" in p.get_text())
        first_row_page = next(i for i, p in enumerate(doc)
                              if "Udin Din Din Dun" in p.get_text())
    assert heading_page == first_row_page, (
        f"heading on page {heading_page + 1}, first row on "
        f"page {first_row_page + 1}"
    )


# ==========================================================================
# Scoped reports and item reports — the same renderer, three widths
# ==========================================================================
#
# The renderer is contract-driven, so a scoped or per-item report is not a new
# code path: it is the same dict with a different `meta` and a different set of
# sections. What IS new is the cover (it has to say WHICH report this is, or a
# one-stream document reads as the whole picture) and the filename (a folder of
# downloads called `42-social-listening-full-2026-08-20.pdf` five times over is
# a folder nobody can use).

ITEM_META: dict[str, Any] = {
    "title": "#spidermanbrandnewday",
    "brand": "42 Abu Dhabi",
    "mode": "full",
    "scope": "viral",
    "scope_label": "Viral Social Trends",
    "item": {
        "kind": "trend",
        "id": 880,
        "stream": "viral",
        "stream_label": "Viral Social Trends",
        "label": "#spidermanbrandnewday",
        "slug": "spidermanbrandnewday",
        "url": "https://www.tiktok.com/tag/spidermanbrandnewday",
    },
    "generated_at": "2026-08-20T09:12:00+00:00",
    "window": {"days": 30, "from": "2026-07-21", "to": "2026-08-20"},
    "previous_window": {"from": "2026-06-21", "to": "2026-07-21"},
}

# An item report is ranked against ONE stream, so the other three counts are
# None and the cover must simply not draw them.
ITEM_BASIS: dict[str, Any] = {
    "scope": "viral",
    "scope_label": "Viral Social Trends",
    "competitor_handles": None,
    "competitor_posts": None,
    "news_items": None,
    "viral_trends": 80,
    "notes": ["This report is about ONE Viral Social Trends item: "
              "#spidermanbrandnewday."],
}


def _item_sections(full: bool) -> list[dict[str, Any]]:
    overview = _section(
        id="item_overview", title="The Item", kind="signals",
        estimated=True,
        basis="Posts and views are the numbers the source itself published.",
        body={"groups": [{
            "group": "Viral Social Trends",
            "items": [{
                "name": "#spidermanbrandnewday",
                "link": "https://www.tiktok.com/tag/spidermanbrandnewday",
                "extra_links": [{"label": "tiktok", "url": LONG_URL}],
                "metrics": [
                    {"label": "Posts", "value": "288,030"},
                    {"label": "Views", "value": "915,630,032"},
                    {"label": "Heat", "value": "0.39 of 1.00 (warm)"},
                    {"label": "Audience sentiment",
                     "value": "not measured - we store no comment text"},
                ],
                "sentiment": {"label": "Neutral", "estimated": True},
                "suggestion": "A transition reel: old life, then the 42 lab.",
            }],
        }]})
    what = _section(id="item_what_it_is", title="What It Is", kind="prose",
                    estimated=True,
                    basis="Assembled from the stored summary and a cached lookup.",
                    body={"paragraphs": ["A fan-made campaign for a film that "
                                         "does not exist.", "AI read of it: a "
                                         "fresh start."]})
    research = _section(
        id="item_research", title="Web Research Sources", kind="table",
        estimated=True, basis="The sources a grounded lookup returned.",
        body={"columns": ["Source", "Link"],
              "rows": [["wikipedia.org", "en.wikipedia.org/x"],
                       ["fandom.com", "fandom.com/y"]],
              "align": ["left", "left"]})
    basis = _section(
        id="item_basis", title="What This Stands On", kind="bullets",
        basis="Every number here is a stored column.",
        body={"items": ["This report is about ONE item.",
                        "NOT MEASURED - audience sentiment."]})
    if not full:
        return [overview, what, research, basis]
    return [
        overview, what, research,
        _section(id="item_ranking", title="Where It Sits In The Window",
                 kind="kpis", basis="This item against the 80 trends collected.",
                 body={"items": [
                     {"label": "Rank by heat", "value": "5th of 80",
                      "delta": None, "delta_dir": None,
                      "note": "this item 0.39, the window median 0.16"},
                     {"label": "Rank by evidence sightings", "value": "1st of 80",
                      "delta": None, "delta_dir": None, "note": None},
                 ]}),
        _section(id="item_timeline", title="Timeline", kind="table",
                 basis="Stored timestamps on this row, unchanged.",
                 body={"columns": ["Event", "When", "Whose clock this is"],
                       "rows": [["First seen by us", "20 Aug 2026",
                                 "the first collector run that listed it"]],
                       "align": ["left", "right", "left"]}),
        _section(id="item_ai_read", title="The AI Read", kind="table",
                 estimated=True, basis="Every line here was written by a model.",
                 body={"columns": ["What the AI said", "Value"],
                       "rows": [["Fit for 42", "80/100"],
                                ["UAE relevance", "85/100"]],
                       "align": ["left", "left"]}),
        _section(id="item_evidence", title="Every Sighting", kind="table",
                 basis="5 distinct sighting(s), one line each.",
                 body={"columns": ["Source", "What it saw", "Metric", "Region",
                                   "Published / recorded", "Link"],
                       "rows": [["tiktok", "#spidermanbrandnewday",
                                 "288,030 posts", "GB", "18 Aug / 20 Aug",
                                 "www.tiktok.com/tag/spidermanbrandnewday"]] * 4,
                       "align": ["left", "left", "right", "left", "right",
                                 "left"]}),
        _section(id="recommendations", title="What 42 Should Do With This",
                 kind="bullets", estimated=True,
                 basis="written by the report model from the assembled sections",
                 body={"items": ["Shoot one transition reel this week."]}),
        basis,
    ]


def _item_report(full: bool = True) -> dict[str, Any]:
    meta = dict(ITEM_META)
    meta["mode"] = "full" if full else "simple"
    return {"meta": meta, "basis": ITEM_BASIS,
            "sections": _item_sections(full)}


def _scoped_report(scope: str = "news", mode: str = "full") -> dict[str, Any]:
    meta = dict(META)
    meta.update(mode=mode, scope=scope, item=None,
                scope_label="Industry Signal",
                title=f"Social-Listening Report - Industry Signal")
    basis = dict(BASIS)
    basis.update(scope=scope, scope_label="Industry Signal",
                 competitor_handles=None, competitor_posts=None,
                 viral_trends=None)
    return {"meta": meta, "basis": basis, "sections": [
        _section(id="mention_volume", title="Mention Volume", kind="kpis",
                 basis="Counted from stored rows.",
                 body={"items": [{"label": "Industry news items", "value": "385",
                                  "delta": "+12", "delta_dir": "up",
                                  "note": "373 in the previous 30 days"}]}),
        _section(id="share_of_voice", title="Share of Voice", kind="table",
                 available=False,
                 unavailable_reason=("Share of Voice compares the competitor "
                                     "accounts we watch; this report covers "
                                     "Industry Signal only."),
                 basis="Not applicable at this scope.",
                 body={"columns": [], "rows": [], "align": []}),
    ]}


def test_an_item_report_renders_at_both_depths() -> None:
    simple = pdf.render_pdf(_item_report(full=False))
    full = pdf.render_pdf(_item_report(full=True))
    assert simple.startswith(b"%PDF-") and full.startswith(b"%PDF-")
    assert len(full) > len(simple)


@needs_reader
def test_a_simple_item_report_is_a_brief_and_a_full_one_is_a_document() -> None:
    """Cover plus body. The owner presses this from a dialog: a one-item brief
    that runs to eight pages is not a brief."""
    assert _pages(pdf.render_pdf(_item_report(full=False))) <= 3
    assert 2 <= _pages(pdf.render_pdf(_item_report(full=True))) <= 6


@needs_reader
def test_the_cover_names_the_item_and_its_stream() -> None:
    """A one-item document that looks like the whole report is the misreading
    this feature cannot afford."""
    flat = _flat(pdf.render_pdf(_item_report()))
    assert "#spidermanbrandnewday" in flat
    assert "one trend" in flat
    assert "Viral Social Trends" in flat
    # "Single trend #880" and a "WHAT THIS ITEM IS RANKED AGAINST" KPI block
    # stood on the cover sheet. The masthead names the item and its stream,
    # which is what stops a one-item document reading as the whole report; the
    # population it was ranked against is a section, like every other number.
    assert "The Item" in flat


@needs_reader
def test_an_item_cover_draws_only_the_population_it_was_ranked_against() -> None:
    """The other two streams are None, not 0 — so no card, not a zero card."""
    with _open(pdf.render_pdf(_item_report())) as doc:
        cover = doc[0].get_text()
    assert "VIRAL SOCIAL TRENDS" in cover and "80" in cover
    for absent in ("COMPETITOR POSTS", "COMPETITOR HANDLES",
                   "INDUSTRY NEWS ITEMS"):
        assert absent not in cover


@needs_reader
def test_a_scoped_cover_says_which_stream_it_covers() -> None:
    with _open(pdf.render_pdf(_scoped_report())) as doc:
        cover = doc[0].get_text()
    flat = re.sub(r"\s+", " ", cover)
    assert "Social-Listening Report - Industry Signal" in flat
    assert "42 Abu Dhabi · Industry Signal" in flat
    assert "INDUSTRY NEWS ITEMS" in cover and "385" in cover
    assert "COMPETITOR POSTS" not in cover
    assert "VIRAL SOCIAL TRENDS" not in cover


@needs_reader
def test_a_not_applicable_section_prints_its_reason_in_a_scoped_report() -> None:
    """The gap is the finding. A blank Share of Voice would read as "we
    measured it and it was empty"."""
    flat = _flat(pdf.render_pdf(_scoped_report()))
    assert "Share of Voice" in flat
    assert "Not enough data." in flat
    assert "this report covers Industry Signal only" in flat


def test_suggested_filename_encodes_the_scope() -> None:
    assert (pdf.suggested_filename(_scoped_report("news", "full"))
            == "42-social-listening-news-full-2026-08-20.pdf")
    report = _scoped_report("competitors", "full")
    report["meta"]["scope"] = "competitors"
    assert (pdf.suggested_filename(report)
            == "42-social-listening-competitors-full-2026-08-20.pdf")


def test_suggested_filename_for_scope_all_is_unchanged() -> None:
    report = _report([], scope="all", scope_label="All three streams", item=None)
    assert pdf.suggested_filename(report) == \
        "42-social-listening-simple-2026-08-20.pdf"


def test_suggested_filename_names_the_item_it_is_about() -> None:
    assert (pdf.suggested_filename(_item_report(full=True))
            == "42-report-trend-880-spidermanbrandnewday-full-2026-08-20.pdf")
    assert (pdf.suggested_filename(_item_report(full=False))
            == "42-report-trend-880-spidermanbrandnewday-simple-2026-08-20.pdf")


def test_an_item_filename_is_ascii_safe_however_bad_the_slug_is() -> None:
    report = _item_report()
    report["meta"]["item"] = dict(report["meta"]["item"],
                                  slug="../../etc/passwd 🔥 نانو",
                                  kind="tr end", id="8 8 0")
    name = pdf.suggested_filename(report)
    assert name.isascii() and name.endswith(".pdf")
    assert "/" not in name and "\\" not in name and ".." not in name
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name


def test_an_item_filename_falls_back_to_the_label_when_there_is_no_slug() -> None:
    report = _item_report()
    report["meta"]["item"] = dict(report["meta"]["item"], slug="")
    name = pdf.suggested_filename(report)
    assert name.startswith("42-report-trend-880-spidermanbrandnewday-")
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name


def test_every_scope_and_item_produces_a_different_filename() -> None:
    """Five downloads that overwrite each other in the browser's folder are one
    download."""
    names = {
        pdf.suggested_filename(_report([], scope="all", item=None)),
        pdf.suggested_filename(_scoped_report("news", "full")),
        pdf.suggested_filename(_scoped_report("viral", "full")),
        pdf.suggested_filename(_item_report(full=True)),
        pdf.suggested_filename(_item_report(full=False)),
    }
    assert len(names) == 5
