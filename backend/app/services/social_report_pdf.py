"""SMAA Social Intelligence report → PDF.

The owner asked for "a really well formatted pdf" twice, so this file is a
typesetter, not a dump. It takes the report dict produced by
``services/social_report.py`` (the CONTRACT shape: ``meta`` / ``basis`` /
``sections[]``) and lays it out with reportlab: a cover that states the sample
sizes before it states anything else, a running header, a real "Page N of M"
footer, and one renderer per section ``kind``.

WHAT THIS FILE MUST NEVER DO
----------------------------
Invent anything. It renders what the dict says and nothing else. A section with
``available=False`` prints its title and its ``unavailable_reason`` in muted
type — that is a correct, finished result, not a hole to fill. Every section
prints its own ``basis`` line, and an AI-ASSESSED chip whenever ``estimated``
is true, so no page ever presents a model's judgement as a measurement.

FONT / UNICODE STRATEGY (the decision that drove the type scale)
---------------------------------------------------------------
Measured on this box, 2026-08-20:

  * ``python:3.12-slim`` (the deploy image) installs ffmpeg / libgl1 /
    libglib / libpq5 and nothing else — **there is no system font package**, so
    no Noto, no DejaVu, no Arial. Anything we reference by filename would work
    here and break in production.
  * The only TTF reportlab bundles is Bitstream Vera. ``fontTools`` says its
    cmap holds **256 glyphs** — Latin-1 only. No Arabic, no Cyrillic, no Greek,
    no emoji. Registering it buys us nothing over the core fonts.
  * The base-14 core fonts do NOT raise on unencodable text, they *silently
    corrupt* it: ``"X\\u0623\\u0637Y\\U0001F525Z"`` drawn in Helvetica extracts
    back as ``"XIIYIZ"`` — every foreign character becomes a capital I. That is
    the tofu failure, and it is silent, so sanitising is mandatory rather than
    optional.
  * Helvetica's digits are all exactly 556/1000 em (measured for 0-9, both
    regular and bold): the core fonts are tabular by construction, so
    right-aligned numeric columns align without a monospace face. Courier is
    therefore used only where a monospace face *means* something — machine
    stamps in the footer, the generated-at line, the data-basis section.

So: **core fonts only, plus a sanitiser** (:func:`sanitize_text`). Identical
bytes in dev and in the container, nothing to install, no embedded font. The
cost is that non-Latin-1 text has to be degraded, and it is degraded in the
order a reader would prefer:

  1. typographic look-alikes are mapped (nbsp → space, U+2011 → "-", → arrows);
  2. Arabic is transliterated letter-by-letter in logical order
     ("نانو" → "nanw") — we cannot shape or bidi-reorder Arabic without
     HarfBuzz, and unshaped right-to-left glyphs printed left-to-right would be
     worse than a romanisation;
  3. emoji and other pictographs are dropped (no font we can ship has them);
  4. anything left is NFKD-decomposed and stripped of combining marks
     ("ő" → "o"), which keeps Latin-Extended readable;
  5. whatever still cannot be encoded is removed. Never a black box, never an
     "I".

Real corpus check: ``general_trends.title`` contains em dashes, "DELÍRIO",
"Ela ké Leitada" and "نانو — TUL8TE" — the first three are Latin-1 and survive
untouched; only the Arabic is transliterated.

Determinism: the document is built with ``invariant=1`` so the PDF's internal
timestamp and /ID are fixed and the same input renders to the same bytes (the
tests assert this). The *human* timestamp on the cover is ``meta.generated_at``
from the data, which is the one that matters.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional, Sequence
from xml.sax.saxutils import escape as _xml_escape

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger("archivist.social_report_pdf")

# --------------------------------------------------------------------------
# Brand palette — the app is white with near-black text and one teal accent.
# --------------------------------------------------------------------------
INK = HexColor("#0F1011")
MUTED = HexColor("#71717A")
BORDER = HexColor("#E5E5E7")
RULE = HexColor("#D4D4D8")
PAPER = HexColor("#FFFFFF")
SURFACE = HexColor("#F2F2F4")
ACCENT = HexColor("#06B6A2")
ACCENT_DARK = HexColor("#048876")
SUCCESS = HexColor("#10B981")
WARNING = HexColor("#F59E0B")
DANGER = HexColor("#EF4444")
ZEBRA = HexColor("#FAFAFB")  # only used past ~12 rows, where it earns its keep

# Chart series colours. Teal, then near-black, then greys — NOT teal followed
# by dark teal: rendered at 110dpi the two teals were indistinguishable in a
# line chart, which is a lie about a comparison. No rainbow either; a second
# series is a comparison, not a decoration.
SERIES_COLORS: tuple[Color, ...] = (ACCENT, INK, MUTED, ACCENT_DARK, WARNING)

# --------------------------------------------------------------------------
# Page geometry (A4 — the UAE/EU standard) and the type scale, in points.
# --------------------------------------------------------------------------
PAGE_SIZE = A4
MARGIN_X = 54.0
MARGIN_TOP = 66.0
MARGIN_BOTTOM = 56.0
CONTENT_WIDTH = PAGE_SIZE[0] - 2 * MARGIN_X  # 487.3pt
FRAME_HEIGHT = PAGE_SIZE[1] - MARGIN_BOTTOM - MARGIN_TOP

# Room a section title block needs before it may be left at the foot of a page.
# Used only as a floor: the real head height is measured, because these basis
# lines run to five lines and a fixed guess strands the heading.
_HEAD_RESERVE = 96.0

# On top of the head, enough body to prove the section started here. Used for
# prose and bullets; a chart reserves its whole height and a table reserves its
# real first two rows, both of which are measured rather than assumed.
_BODY_RESERVE = 46.0

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"
FONT_MONO = "Courier"

# Cap on how much of a table a single column may claim before we make it wrap.
_MAX_COL_FRACTION = 0.45
_MIN_COL_WIDTH = 40.0
_ZEBRA_FROM_ROWS = 12


# ==========================================================================
# Text sanitising — see the module docstring for why this is not optional.
# ==========================================================================

# Look-alikes that cp1252 cannot encode but a reader expects to see *something*
# for. (Chars cp1252 *does* have — em dash, curly quotes, bullet, ellipsis, ×,
# ° — are deliberately absent: they pass through untouched.)
_LOOKALIKES: dict[str, str] = {
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "―": "-",  # horizontal bar
    "−": "-",  # minus sign
    " ": " ",  # nbsp
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    "　": " ",
    "​": "",  # zero-width space
    "‌": "",
    "‍": "",  # ZWJ (emoji sequences)
    "﻿": "",
    " ": " ",
    " ": " ",
    "→": "->",
    "←": "<-",
    "➔": "->",
    "⇒": "=>",
    "≤": "<=",
    "≥": ">=",
    "≠": "!=",
    "≈": "~",
    "⁄": "/",
    "▲": "+",  # up triangle, used as a delta marker upstream
    "▼": "-",  # down triangle
    "–": "–",  # en dash IS cp1252 (0x96) — listed to document intent
}

# Arabic → Latin, logical order. Deliberately plain (no macrons, no digraph
# ambiguity beyond the usual th/kh/sh/gh) because this is a fallback for a
# handful of trend titles, not a scholarly transliteration.
_ARABIC: dict[str, str] = {
    "ء": "'",
    "آ": "aa",
    "أ": "a",
    "ؤ": "'",
    "إ": "i",
    "ئ": "'",
    "ا": "a",
    "ب": "b",
    "ة": "a",
    "ت": "t",
    "ث": "th",
    "ج": "j",
    "ح": "h",
    "خ": "kh",
    "د": "d",
    "ذ": "dh",
    "ر": "r",
    "ز": "z",
    "س": "s",
    "ش": "sh",
    "ص": "s",
    "ض": "d",
    "ط": "t",
    "ظ": "z",
    "ع": "'",
    "غ": "gh",
    "ـ": "",  # tatweel: pure decoration
    "ف": "f",
    "ق": "q",
    "ك": "k",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "ه": "h",
    "و": "w",
    "ى": "a",
    "ي": "y",
    "٠": "0",
    "١": "1",
    "٢": "2",
    "٣": "3",
    "٤": "4",
    "٥": "5",
    "٦": "6",
    "٧": "7",
    "٨": "8",
    "٩": "9",
    "،": ",",
    "؛": ";",
    "؟": "?",
    "٪": "%",
    "٫": ".",
    "٬": ",",
    "ٱ": "a",
    "پ": "p",
    "چ": "ch",
    "ڤ": "v",
    "گ": "g",
    "ی": "y",
}

# Ranges we drop outright: nothing we can ship draws them.
_PICTOGRAPH_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # emoji, mahjong, playing cards, symbols & pictographs
    (0x1F1E6, 0x1F1FF),  # regional indicators (flags)
    (0x2190, 0x21FF),  # arrows not caught by _LOOKALIKES
    (0x2300, 0x23FF),  # misc technical (⏰ ⌚ ▶)
    (0x2460, 0x24FF),  # enclosed alphanumerics
    (0x25A0, 0x25FF),  # geometric shapes
    (0x2600, 0x27BF),  # misc symbols + dingbats
    (0x2B00, 0x2BFF),  # arrows / stars
    (0xFE00, 0xFE0F),  # variation selectors
    (0xE000, 0xF8FF),  # private use (brand glyphs)
)

_MULTISPACE = re.compile(r"[ \t]{2,}")


@lru_cache(maxsize=4096)
def _encodable(ch: str) -> bool:
    """True when a core (WinAnsi) font can actually draw ``ch``."""
    try:
        ch.encode("cp1252")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False
    return True


def _is_pictograph(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _PICTOGRAPH_RANGES)


def sanitize_text(value: Any, default: str = "") -> str:
    """Make ``value`` safe to draw with a core font — never tofu, never an "I".

    Accepts anything (the contract says strings, reality sometimes says
    ``None`` or an int) and always returns a printable str. ``default`` is
    returned when the input is missing *or* when sanitising empties it — an
    emoji-only trend name is the real case.
    """
    if value is None:
        return default
    if not isinstance(value, str):
        value = str(value)
    text = unicodedata.normalize("NFC", value)

    out: list[str] = []
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            out.append(" ")
            continue
        mapped = _LOOKALIKES.get(ch)
        if mapped is not None:
            out.append(mapped)
            continue
        cp = ord(ch)
        if cp < 0x20 or cp == 0x7F:  # control characters
            out.append(" ")
            continue
        if _encodable(ch):
            out.append(ch)
            continue
        if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0xFB50 <= cp <= 0xFEFF:
            out.append(_ARABIC.get(ch, ""))  # unknown Arabic-block char: drop
            continue
        if _is_pictograph(cp):
            out.append(" ")
            continue
        # Last resort: strip the accents and keep the letter ("ő" -> "o").
        decomposed = unicodedata.normalize("NFKD", ch)
        kept = "".join(
            c for c in decomposed if not unicodedata.combining(c) and _encodable(c)
        )
        out.append(kept)

    cleaned = _MULTISPACE.sub(" ", "".join(out)).strip()
    return cleaned or default


def _esc(value: Any, default: str = "") -> str:
    """Sanitised **and** XML-escaped, ready for a Paragraph."""
    return _xml_escape(sanitize_text(value, default))


def _attr(value: Any) -> str:
    """Escape a URL for use inside an XML attribute."""
    return _xml_escape(sanitize_text(value), {'"': "&quot;", "'": "&apos;"})


# ==========================================================================
# Small formatting helpers
# ==========================================================================

_DATE_ONLY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    match = _DATE_ONLY.match(value.strip())
    if match:
        try:
            return datetime(*(int(g) for g in match.groups()), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _fmt_date(value: Any, fallback: str = "-") -> str:
    """``2026-08-20T09:12:00Z`` → ``20 Aug 2026``. Unparseable text is kept."""
    parsed = _parse_dt(value)
    if parsed is None:
        return sanitize_text(value, fallback)
    return parsed.strftime("%d %b %Y")


def _fmt_datetime(value: Any, fallback: str = "-") -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return sanitize_text(value, fallback)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # 12-hour, to match every clock in the UI (frontend `CLOCK_12H`). %I pads
    # the hour and there is no portable strftime flag that does not, so the
    # zero is stripped by hand: "09:05 AM" is a 24-hour habit wearing a
    # meridiem.
    stamp = parsed.astimezone(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")
    return stamp.replace(", 0", ", ", 1) if ", 0" in stamp else stamp


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return sanitize_text(value, "-")


def _num(value: Any) -> float:
    """Coerce a chart value. Non-numeric is 0.0 — a chart cannot draw "n/a"."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return 0.0
    return out


def _shorten_url(url: str, limit: int = 54) -> str:
    """Display text for a link. The full URL stays live in the annotation."""
    text = sanitize_text(url)
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    text = text.rstrip("/")
    if len(text) <= limit:
        return text
    head = text.split("/", 1)[0]
    tail = text[-(max(limit - len(head) - 5, 8)) :]
    return f"{head}/…{tail}"  # U+2026 ellipsis is cp1252 0x85


def _dget(obj: Any, key: str, default: Any = None) -> Any:
    """dict.get that survives a None (or a list, or anything else) upstream."""
    if isinstance(obj, dict):
        value = obj.get(key, default)
        return default if value is None else value
    return default


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


# ==========================================================================
# Styles
# ==========================================================================


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "body",
        fontName=FONT,
        fontSize=9.5,
        leading=14.0,
        textColor=INK,
        spaceAfter=0,
        splitLongWords=1,  # a 200-char token wraps instead of overflowing
    )
    out: dict[str, ParagraphStyle] = {"body": base}

    out["cover_title"] = ParagraphStyle(
        "cover_title", parent=base, fontName=FONT_BOLD, fontSize=30, leading=34,
        spaceAfter=6,
    )
    out["cover_sub"] = ParagraphStyle(
        "cover_sub", parent=base, fontSize=12.5, leading=17, textColor=MUTED,
    )
    out["cover_stamp"] = ParagraphStyle(
        "cover_stamp", parent=base, fontName=FONT_MONO, fontSize=8, leading=12,
        textColor=MUTED,
    )
    out["eyebrow"] = ParagraphStyle(
        "eyebrow", parent=base, fontName=FONT_BOLD, fontSize=7.2, leading=10,
        textColor=ACCENT_DARK, spaceAfter=3,
    )
    out["h1"] = ParagraphStyle(
        "h1", parent=base, fontName=FONT_BOLD, fontSize=15, leading=19,
        spaceAfter=2,
    )
    out["h2"] = ParagraphStyle(
        "h2", parent=base, fontName=FONT_BOLD, fontSize=10.5, leading=14,
        spaceBefore=8, spaceAfter=3,
    )
    out["basis"] = ParagraphStyle(
        "basis", parent=base, fontSize=7.6, leading=11, textColor=MUTED,
        spaceAfter=8,
    )
    out["prose"] = ParagraphStyle("prose", parent=base, spaceAfter=7)
    out["bullet"] = ParagraphStyle(
        "bullet", parent=base, leftIndent=13, bulletIndent=2, spaceAfter=5,
    )
    out["muted"] = ParagraphStyle("muted", parent=base, textColor=MUTED)
    out["small"] = ParagraphStyle(
        "small", parent=base, fontSize=7.6, leading=10.5, textColor=MUTED,
    )
    out["chart_caption_mid"] = ParagraphStyle(
        "chart_caption_mid", parent=base, fontName=FONT_MONO, fontSize=6.6,
        leading=9, textColor=MUTED, alignment=TA_CENTER,
    )
    out["chart_caption_end"] = ParagraphStyle(
        "chart_caption_end", parent=base, fontName=FONT_MONO, fontSize=6.6,
        leading=9, textColor=MUTED, alignment=TA_RIGHT,
    )
    out["mono_small"] = ParagraphStyle(
        "mono_small", parent=base, fontName=FONT_MONO, fontSize=7.6, leading=11,
        textColor=MUTED,
    )
    out["unavailable"] = ParagraphStyle(
        "unavailable", parent=base, fontSize=9.5, leading=14, textColor=MUTED,
        leftIndent=10, spaceAfter=6,
    )

    # Table cells (one per alignment so numeric columns really do right-align).
    cell = ParagraphStyle(
        "cell", parent=base, fontSize=8.6, leading=12, splitLongWords=1,
    )
    out["cell_left"] = cell
    out["cell_right"] = ParagraphStyle("cell_right", parent=cell, alignment=TA_RIGHT)
    out["cell_center"] = ParagraphStyle("cell_center", parent=cell, alignment=TA_CENTER)
    head = ParagraphStyle(
        "head_left", parent=cell, fontName=FONT_BOLD, fontSize=7.6, leading=10.5,
        textColor=INK,
    )
    out["head_left"] = head
    out["head_right"] = ParagraphStyle("head_right", parent=head, alignment=TA_RIGHT)
    out["head_center"] = ParagraphStyle("head_center", parent=head, alignment=TA_CENTER)

    # KPI cards.
    out["kpi_label"] = ParagraphStyle(
        "kpi_label", parent=base, fontSize=7.0, leading=9.5, textColor=MUTED,
    )
    out["kpi_value"] = ParagraphStyle(
        "kpi_value", parent=base, fontName=FONT_BOLD, fontSize=20, leading=23,
        spaceBefore=3,
    )
    out["kpi_delta"] = ParagraphStyle(
        "kpi_delta", parent=base, fontName=FONT_BOLD, fontSize=8.4, leading=12,
        spaceBefore=2,
    )
    out["kpi_note"] = ParagraphStyle(
        "kpi_note", parent=base, fontSize=7.0, leading=9.5, textColor=MUTED,
        spaceBefore=2,
    )

    # Signals.
    out["signal_group"] = ParagraphStyle(
        "signal_group", parent=base, fontName=FONT_BOLD, fontSize=8.0, leading=11,
        textColor=ACCENT_DARK, spaceBefore=4, spaceAfter=2,
    )
    out["signal_name"] = ParagraphStyle(
        "signal_name", parent=base, fontName=FONT_BOLD, fontSize=11, leading=14,
    )
    out["signal_links"] = ParagraphStyle(
        "signal_links", parent=base, fontSize=8.0, leading=11.5, spaceBefore=1,
    )
    out["signal_metrics"] = ParagraphStyle(
        "signal_metrics", parent=base, fontSize=8.4, leading=12.5, spaceBefore=3,
    )
    out["suggestion"] = ParagraphStyle(
        "suggestion", parent=base, fontSize=8.6, leading=12.5,
    )
    return out


S = _styles()


def _hex(color: Color) -> str:
    """``Color`` -> ``#rrggbb``. ``hexval()`` yields ``0xrrggbb``, which the
    paragraph parser rejects."""
    return "#" + color.hexval()[2:]


def _chip(text: str, fill: Color = ACCENT, ink: str = "#FFFFFF", size: float = 6.4) -> str:
    """Inline pill. reportlab draws ``backcolor`` tight to the text, so the
    padding is literal spaces — crude, but it is the only way to get a chip
    inside a Paragraph without a nested table."""
    return (
        f'<font face="{FONT_BOLD}" size="{size}" color="{ink}" '
        f'backcolor="{_hex(fill)}">&#160;{_xml_escape(text)}&#160;</font>'
    )


AI_CHIP = _chip("AI-ASSESSED")


def _link(text: str, url: Any) -> str:
    """Inline clickable link with shortened display text."""
    clean = sanitize_text(url)
    if not clean:
        return _esc(text)
    label = _xml_escape(sanitize_text(text) or _shorten_url(clean))
    return f'<a href="{_attr(clean)}" color="{_hex(ACCENT_DARK)}">{label}</a>'


# ==========================================================================
# Section renderers, one per `kind`
# ==========================================================================


def _prose(body: Any) -> list[Any]:
    paragraphs = [p for p in _as_list(_dget(body, "paragraphs", [])) if sanitize_text(p)]
    if not paragraphs:
        return [Paragraph(_esc("No narrative was produced for this section."), S["muted"])]
    return [Paragraph(_esc(p), S["prose"]) for p in paragraphs]


def _bullets(body: Any) -> list[Any]:
    """One bullet per item, and a second line where the item carries evidence.

    `social_report_narrative` writes a recommendation as the action, a newline,
    then the evidence for it. That newline is the whole separator: `sanitize_text`
    flattens it to a space, so printing the string whole runs the two halves
    together into one sentence that reads as a single thought. Splitting here
    gives the action its own bullet and puts the evidence under it in the muted
    style, which is what the screen already does.
    """
    items = [i for i in _as_list(_dget(body, "items", [])) if sanitize_text(i)]
    if not items:
        return [Paragraph(_esc("No items."), S["muted"])]
    # Split the RAW string. `sanitize_text` maps a newline to a space, so
    # sanitising first destroys the separator before it can be found. The
    # evidence half after it is DROPPED, here and on screen, from the same
    # split: it doubled the length of every list to restate what the sections
    # above had already measured, and printing it in one renderer and not the
    # other was the loudest way the file and the page disagreed.
    return [
        Paragraph(_esc(str(item).partition("\n")[0]), S["bullet"], bulletText="•")
        for item in items
    ]


def _delta_color(direction: Any) -> Color:
    return {"up": SUCCESS, "down": DANGER, "flat": MUTED}.get(
        str(direction).lower(), MUTED
    )


def _kpi_card(item: Any, width: float) -> Table:
    """One KPI card: eyebrow label, big value, coloured delta, small note."""
    label = sanitize_text(_dget(item, "label", ""), "-").upper()
    value = sanitize_text(_dget(item, "value", ""), "-")
    delta = sanitize_text(_dget(item, "delta", ""))
    note = sanitize_text(_dget(item, "note", ""))
    direction = _dget(item, "delta_dir", "")

    flow: list[Any] = [
        Paragraph(_xml_escape(label), S["kpi_label"]),
        Paragraph(_xml_escape(value), S["kpi_value"]),
    ]
    if delta:
        colour = _hex(_delta_color(direction))
        # The bullet is a colour anchor; the sign in the text carries direction
        # (no core font has a triangle glyph, and we ship no other font).
        flow.append(
            Paragraph(
                f'<font color="{colour}">• {_xml_escape(delta)}</font>',
                S["kpi_delta"],
            )
        )
    if note:
        flow.append(Paragraph(_xml_escape(note), S["kpi_note"]))

    card = Table([[flow]], colWidths=[width])
    card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PAPER),
                ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
                ("ROUNDEDCORNERS", [4, 4, 4, 4]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 11),
                ("RIGHTPADDING", (0, 0), (-1, -1), 11),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
            ]
        )
    )
    return card


def _kpis(body: Any, width: float = CONTENT_WIDTH) -> list[Any]:
    items = _as_list(_dget(body, "items", []))
    if not items:
        return [Paragraph(_esc("No KPIs."), S["muted"])]

    per_row = 3 if len(items) >= 3 else max(len(items), 1)
    gap = 10.0
    card_w = (width - gap * (per_row - 1)) / per_row

    rows: list[list[Any]] = []
    for start in range(0, len(items), per_row):
        chunk = items[start : start + per_row]
        cells: list[Any] = []
        for idx, item in enumerate(chunk):
            if idx:
                cells.append("")
            cells.append(_kpi_card(item, card_w))
        while len(cells) < per_row * 2 - 1:
            cells.extend(["", ""])
        rows.append(cells)

    widths: list[float] = []
    for idx in range(per_row * 2 - 1):
        widths.append(gap if idx % 2 else card_w)

    grid = Table(rows, colWidths=widths, hAlign="LEFT")
    grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), gap),
            ]
        )
    )
    return [grid]


def _widest_word(text: str, font: str, size: float) -> float:
    """The longest run with no break opportunity in it.

    A column narrower than this forces reportlab to split a word across two
    lines. That is not tight typesetting, it is unreadable: on a live report a
    squeezed "Type (AI)" column rendered "culture" as "cultur/e" and "sound" as
    "soun/d", which a reader sees as corrupted data rather than as a layout
    compromise."""
    widest = 0.0
    for word in sanitize_text(text).split():
        widest = max(widest, stringWidth(word, font, size))
    return widest


def _apply_floors(
    widths: list[float], floors: list[float], total: float
) -> list[float]:
    """Raise every column to its floor, paying for it out of the columns that
    have room to spare (proportionally to how much spare each one has).

    If the floors alone do not fit, they are scaled together — a table that
    must break words is still better than one that runs off the page."""
    if sum(floors) >= total:
        factor = total / sum(floors) if sum(floors) else 1.0
        return [f * factor for f in floors]
    deficit = sum(max(0.0, floors[i] - widths[i]) for i in range(len(widths)))
    if deficit <= 0.0:
        return widths
    spare = [max(0.0, widths[i] - floors[i]) for i in range(len(widths))]
    pool = sum(spare)
    out = []
    for i, w in enumerate(widths):
        if w < floors[i]:
            out.append(floors[i])
        elif pool > 0.0:
            out.append(w - deficit * (spare[i] / pool))
        else:
            out.append(w)
    return out


def _measure(flow: Sequence[Any], width: float) -> float:
    """Real rendered height of a run of flowables.

    reportlab's wrap() is how a flowable reports its own size, and it is safe to
    call before the document build — the value is recomputed at layout time."""
    total = 0.0
    for item in flow:
        try:
            total += float(item.wrap(width, FRAME_HEIGHT)[1])
        except Exception:      # noqa: BLE001 — measuring must never break a build
            continue
    return total


def _table_head_height(table: Table, width: float) -> float:
    """Height of a table's header row plus its first row of data.

    A table may split across pages, but splitting it before its first data row
    leaves the heading, the basis line and nothing else at the foot of a page.
    The header row of these tables wraps to two lines often enough that a fixed
    guess is wrong, so the real row heights are read back after wrap()."""
    try:
        table.wrap(width, FRAME_HEIGHT)
        heights = [float(h) for h in (table._rowHeights or []) if h]
    except Exception:          # noqa: BLE001 — never fail a build to measure it
        return _BODY_RESERVE
    return sum(heights[:2]) if heights else _BODY_RESERVE


def _indivisible_height(flow: Sequence[Any], width: float) -> float:
    """How much of the body must land on the same page as the heading.

    A Drawing (a chart) cannot be split at all, so all of it counts. A Table
    can be split, but not before its first data row."""
    total = 0.0
    for item in flow:
        if isinstance(item, Drawing):
            try:
                total += float(item.height)
            except Exception:  # noqa: BLE001 — a bad drawing must not stop the page
                break
        elif isinstance(item, Table):
            total += _table_head_height(item, width)
            break
        else:
            break
    return total


def _column_widths(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], total: float
) -> list[float]:
    """Natural widths, capped so one long cell cannot eat the table, then
    scaled to fill exactly ``total`` — but never below the width of a column's
    longest single word."""
    count = len(columns)
    if count == 0:
        return []
    padding = 16.0
    natural: list[float] = []
    floors: list[float] = []
    for idx in range(count):
        widest = stringWidth(sanitize_text(columns[idx]), FONT_BOLD, 7.6)
        floor = _widest_word(columns[idx], FONT_BOLD, 7.6)
        for row in rows:
            if idx < len(row):
                widest = max(
                    widest, stringWidth(sanitize_text(row[idx]), FONT, 8.6)
                )
                floor = max(floor, _widest_word(str(row[idx]), FONT, 8.6))
        natural.append(min(widest + padding, total * _MAX_COL_FRACTION))
        # The floor is a word plus the cell's own padding, and it may not
        # itself exceed the per-column cap or one column could still eat
        # the table.
        floors.append(min(max(floor + padding, _MIN_COL_WIDTH),
                          total * _MAX_COL_FRACTION))
    natural = [max(w, _MIN_COL_WIDTH) for w in natural]

    # Never exceed the frame; if the caps made us too wide, scale everything.
    if sum(natural) > total:
        factor = total / sum(natural)
        natural = [w * factor for w in natural]
    else:
        slack = total - sum(natural)
        share = sum(natural)
        natural = [w + slack * (w / share) for w in natural]
    # Scaling above is uniform, so it can push a narrow column under its
    # longest word. Put that back before the table is built.
    natural = _apply_floors(natural, floors, total)
    # Absorb float drift into the last column so the table ends flush.
    natural[-1] += total - sum(natural)
    return natural


def _table(body: Any, width: float = CONTENT_WIDTH) -> list[Any]:
    columns = [sanitize_text(c) for c in _as_list(_dget(body, "columns", []))]
    raw_rows = [_as_list(r) for r in _as_list(_dget(body, "rows", []))]
    align = [str(a).lower() for a in _as_list(_dget(body, "align", []))]
    if not columns or not raw_rows:
        return [Paragraph(_esc("No rows."), S["muted"])]

    # Normalise ragged rows rather than letting reportlab raise.
    rows = [
        [sanitize_text(cell) for cell in (row + [""] * (len(columns) - len(row)))][
            : len(columns)
        ]
        for row in raw_rows
    ]
    aligns = [
        align[i] if i < len(align) and align[i] in ("left", "right", "center") else "left"
        for i in range(len(columns))
    ]

    header = [
        Paragraph(_xml_escape(columns[i]), S[f"head_{aligns[i]}"])
        for i in range(len(columns))
    ]
    data: list[list[Any]] = [header]
    for row in rows:
        data.append(
            [
                Paragraph(_xml_escape(row[i]), S[f"cell_{aligns[i]}"])
                for i in range(len(columns))
            ]
        )

    widths = _column_widths(columns, rows, width)
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    commands: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), SURFACE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, RULE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.35, BORDER),
        ("LINEBELOW", (0, -1), (-1, -1), 0.75, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]
    if len(rows) >= _ZEBRA_FROM_ROWS:
        # Long tables only: past ~12 rows the eye starts losing the row.
        for idx in range(1, len(rows) + 1):
            if idx % 2 == 0:
                commands.append(("BACKGROUND", (0, idx), (-1, idx), ZEBRA))
    table.setStyle(TableStyle(commands))
    return [table, Spacer(1, 4)]


def _chart(body: Any, width: float = CONTENT_WIDTH) -> list[Any]:
    """One bar per bucket, and the SAME bars the screen draws.

    This was a 200pt reportlab chart with a value axis, a grid and a legend,
    one line per stream, while the screen drew a 96px strip of stacked totals
    under a single caption. Same numbers, two different pictures, and a reader
    comparing the file with the page it came from had no way to know they were
    the same report.

    The strip won. A reader wants to see WHEN the week was busy, which is one
    shape, and the three streams are counted separately three sections later
    anyway. The series are summed exactly as the screen sums them.
    """
    labels = [sanitize_text(x) for x in _as_list(_dget(body, "labels", []))]
    series = [s for s in _as_list(_dget(body, "series", [])) if isinstance(s, dict)]
    if not labels or not series:
        return [Paragraph(_esc("No chart data."), S["muted"])]

    totals = [0.0] * len(labels)
    for entry in series:
        for i, raw in enumerate(_as_list(_dget(entry, "values", []))):
            if i < len(totals):
                totals[i] += max(0.0, _num(raw))
    peak = max(totals) if totals else 0.0
    if peak <= 0:
        return [Paragraph(_esc("No chart data."), S["muted"])]

    plot = 74.0
    gap = 2.0
    slot = (width + gap) / len(totals)
    bar_w = max(1.0, min(slot - gap, 24.0))
    drawing = Drawing(width, plot)
    fill = Color(ACCENT.red, ACCENT.green, ACCENT.blue, 0.78)
    for i, value in enumerate(totals):
        if value <= 0:
            continue    # a true zero keeps its zero height
        # The floor is 3% of the plot, not zero: one item in a 30-day window is
        # a fraction of a point and would read as "nothing happened" on a day
        # something did.
        h = max((value / peak) * plot, plot * 0.03)
        drawing.add(Rect(i * slot, 0, bar_w, h, fillColor=fill, strokeColor=None))

    caption = Table(
        [[Paragraph(_esc(labels[0]), S["mono_small"]),
          Paragraph(f'{_esc("busiest:")} <font face="{FONT_BOLD}" color="{_hex(INK)}">'
                    f'{_fmt_int(peak)}</font>', S["chart_caption_mid"]),
          Paragraph(_esc(labels[-1]), S["chart_caption_end"])]],
        colWidths=[width / 3.0] * 3,
    )
    caption.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.4, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [drawing, Spacer(1, 4), caption]


def _nowrap(text: str, limit: float = CONTENT_WIDTH * 0.4) -> str:
    """Escape ``text`` and glue it with non-breaking spaces so a metric value
    wraps as one unit — real data gave "Shares not available (Instagram / API)"
    with the parenthesis split across two lines. Anything wider than ``limit``
    keeps ordinary spaces: an unbreakable run that long would be worse.
    """
    escaped = _xml_escape(text)
    if stringWidth(text, FONT, 8.4) > limit:
        return escaped
    return escaped.replace(" ", "&#160;")


def _suggestion_box(text: str, width: float) -> Table:
    """Tinted box with a teal spine — the "what 42 should do next" line."""
    para = Paragraph(
        f'<font face="{FONT_BOLD}">Suggestion&#160;&#160;</font>{_xml_escape(text)}',
        S["suggestion"],
    )
    box = Table([["", para]], colWidths=[2.5, width - 2.5], hAlign="LEFT")
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), ACCENT),
                ("BACKGROUND", (1, 0), (1, 0), SURFACE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 0),
                ("TOPPADDING", (0, 0), (0, 0), 0),
                ("BOTTOMPADDING", (0, 0), (0, 0), 0),
                ("LEFTPADDING", (1, 0), (1, 0), 9),
                ("RIGHTPADDING", (1, 0), (1, 0), 9),
                ("TOPPADDING", (1, 0), (1, 0), 7),
                ("BOTTOMPADDING", (1, 0), (1, 0), 7),
            ]
        )
    )
    return box


def _signal_item(item: Any, width: float) -> list[Any]:
    flow: list[Any] = []
    name = sanitize_text(_dget(item, "name", ""), "(untitled)")
    flow.append(Paragraph(_xml_escape(name), S["signal_name"]))

    links: list[str] = []
    primary = sanitize_text(_dget(item, "link", ""))
    if primary:
        links.append(_link(_shorten_url(primary), primary))
    for extra in _as_list(_dget(item, "extra_links", [])):
        url = sanitize_text(_dget(extra, "url", ""))
        if not url:
            continue
        links.append(_link(sanitize_text(_dget(extra, "label", ""), "Link"), url))
    if links:
        sep = f'<font color="{_hex(RULE)}"> · </font>'
        flow.append(Paragraph(sep.join(links), S["signal_links"]))

    parts: list[str] = []
    for metric in _as_list(_dget(item, "metrics", [])):
        label = sanitize_text(_dget(metric, "label", ""))
        value = sanitize_text(_dget(metric, "value", ""), "-")
        if not label and not value:
            continue
        parts.append(
            f'<font color="{_hex(MUTED)}" size="7.6">{_nowrap(label)}</font>'
            f'&#160;<font face="{FONT_BOLD}">{_nowrap(value)}</font>'
        )
    sentiment = _dget(item, "sentiment")
    if isinstance(sentiment, dict):
        label = sanitize_text(_dget(sentiment, "label", ""), "-")
        chip = AI_CHIP if _dget(sentiment, "estimated", False) else ""
        parts.append(
            f'<font color="{_hex(MUTED)}" size="7.6">Tone</font>&#160;'
            f'<font face="{FONT_BOLD}">{_nowrap(label)}</font>'
            + (f" {chip}" if chip else "")  # breakable: the chip may wrap
        )
    if parts:
        # The middot is glued to the pair it follows (nbsp) and followed by a
        # REAL space: that space is the only break opportunity in the line. With
        # nbsp on both sides the whole metrics run became one unbreakable token
        # and reportlab chopped it mid-word ("Tone Po / sitive").
        sep = f'<font color="{_hex(RULE)}">&#160;&#160;·</font>&#160; '
        flow.append(Paragraph(sep.join(parts), S["signal_metrics"]))

    # A `suggestion` box stood here. The screen has never drawn one, so every
    # signal carried a paragraph in the file that was not on the page, and the
    # ideas worth acting on are collected and ranked in their own sections
    # rather than repeated under each item.
    flow.append(Spacer(1, 12))
    # Deliberately NOT wrapped in KeepTogether here: the caller does that. A
    # KeepTogether inside a KeepTogether wraps to height 0xffffff, which the
    # outer one reads as "taller than the page" and answers with a FrameBreak —
    # measured cost, one blank page per nesting.
    return flow


def _signals(body: Any, width: float = CONTENT_WIDTH) -> list[Any]:
    groups = [g for g in _as_list(_dget(body, "groups", [])) if isinstance(g, dict)]
    if not groups:
        return [Paragraph(_esc("No signals."), S["muted"])]

    flow: list[Any] = []
    for index, group in enumerate(groups):
        title = sanitize_text(_dget(group, "group", ""), "(ungrouped)")
        items = _as_list(_dget(group, "items", []))
        header = [
            Spacer(1, 10) if index else Spacer(1, 0),
            Paragraph(_xml_escape(title.upper()), S["signal_group"]),
            _hrule(ACCENT, 1.0, width),
            Spacer(1, 7),
        ]
        first = _signal_item(items[0], width) if items else [
            Paragraph(_esc("No items in this group."), S["muted"]), Spacer(1, 10)
        ]
        # A group heading must never be the last thing on a page, so it travels
        # with its first item; every other item is kept whole on its own.
        flow.append(KeepTogether(header + first))
        for item in items[1:]:
            flow.append(KeepTogether(_signal_item(item, width)))
    return flow


def _hrule(color: Color, thickness: float, width: float) -> Table:
    """A rule that behaves in a KeepTogether (HRFlowable measures oddly there)."""
    rule = Table([[""]], colWidths=[width], rowHeights=[thickness])
    rule.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), color),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return rule


_RENDERERS = {
    "prose": lambda body, width: _prose(body),
    "bullets": lambda body, width: _bullets(body),
    "kpis": _kpis,
    "table": _table,
    "chart": _chart,
    "signals": _signals,
}


def _basis_appendix(sections: list[Any], width: float) -> list[Any]:
    """Every section's basis line, once, at the end.

    This is the other half of the decision documented in `_section`: the
    methodology comes OUT of the body and lands here, so the report reads as
    findings and still answers "where did that number come from" for anyone who
    asks. A section with no basis contributes no line rather than an empty one.

    It is deliberately the last thing in the document, and it FLOWS rather than
    starting its own page: an appendix that claims a fresh sheet reads as a
    section, and on a one-section brief it would add a page of footnotes to a
    two-page document. CondPageBreak only breaks when there is no room left."""
    rows: list[Any] = []
    for index, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            continue
        basis = sanitize_text(_dget(section, "basis", ""))
        if not basis:
            continue
        title = sanitize_text(_dget(section, "title", ""), "(untitled section)")
        rows.append(
            Paragraph(
                f'<font face="{FONT_BOLD}">{index:02d} {_xml_escape(title)}</font><br/>'
                f"{_xml_escape(basis)}",
                S["basis"],
            )
        )
        rows.append(Spacer(1, 5))
    if not rows:
        return []

    head: list[Any] = [
        Paragraph(_xml_escape("Where these numbers come from"), S["h1"]),
        _hrule(RULE, 0.6, width),
        Spacer(1, 6),
        Paragraph(
            _esc(
                "One line per section, naming exactly what it was counted from "
                "and over which window, so anything above can be checked rather "
                "than believed."
            ),
            S["muted"],
        ),
        Spacer(1, 8),
    ]
    return [CondPageBreak(140), Spacer(1, 10), *head, *rows]


def _section(section: Any, number: int, total: int, width: float) -> list[Any]:
    """Title + body for one section, with widow control.

    THE BASIS LINE IS NOT PRINTED HERE ANY MORE. Every section carries one, and
    each is a paragraph of methodology ("Counted from stored rows, 21 Aug -
    20 Sep 2026: competitor posts by publish time, news items by publish time
    falling back to collection time when a feed gives none. Compared against
    ...") — roughly 250 characters, 31 of them, close to 8,000 characters of
    prose wrapped around the numbers a reader actually came for. Under every
    single heading it read as a second, denser section nobody asked for.

    The audit trail is NOT lost, which is the point: `_basis_appendix` prints
    every one of them at the end, as one compact list, so a number can still be
    checked rather than believed. One appendix, not thirty-one footnotes."""
    if not isinstance(section, dict):
        return []

    title = sanitize_text(_dget(section, "title", ""), "(untitled section)")
    basis = sanitize_text(_dget(section, "basis", ""))
    estimated = bool(_dget(section, "estimated", False))
    available = bool(_dget(section, "available", True))
    kind = str(_dget(section, "kind", "prose")).lower()

    head: list[Any] = []
    if total > 1:
        head.append(Paragraph(f"{number:02d}", S["eyebrow"]))
    head.append(Paragraph(_xml_escape(title), S["h1"]))
    head.append(_hrule(RULE, 0.6, width))
    head.append(Spacer(1, 4))

    # The AI chip stays: "a model wrote this" changes how the line below should
    # be read, and it is three words, not a paragraph.
    if estimated:
        head.append(Paragraph(AI_CHIP, S["basis"]))
    else:
        head.append(Spacer(1, 6))

    if not available:
        reason = sanitize_text(
            _dget(section, "unavailable_reason", ""),
            "No reason was recorded.",
        )
        head.append(
            Paragraph(
                f'<font face="{FONT_BOLD}">Not enough data.</font> {_xml_escape(reason)}',
                S["unavailable"],
            )
        )
        return [CondPageBreak(96), KeepTogether(head), Spacer(1, 14)]

    renderer = _RENDERERS.get(kind)
    if renderer is None:
        log.warning("social_report_pdf: unknown section kind %r", kind)
        body_flow: list[Any] = [
            Paragraph(
                _esc(f"Unsupported section kind '{kind}' — nothing rendered."),
                S["muted"],
            )
        ]
    else:
        try:
            body_flow = renderer(_dget(section, "body", {}), width)
        except Exception:  # a malformed body must not lose the whole report
            log.exception("social_report_pdf: section %r failed to render", title)
            body_flow = [
                Paragraph(_esc("This section could not be rendered."), S["muted"])
            ]

    # CondPageBreak guarantees room for the head block, so a title can never be
    # the last thing on a page; the head itself is glued.
    #
    # A chart is ONE indivisible Drawing, so reserving only the head's height
    # is not enough: the head fits, the chart does not, and the reader gets a
    # heading and a basis line stranded above 250pt of white space with the
    # chart overleaf. Reserve the chart's real height too. The reserve is
    # capped below the frame height because a CondPageBreak taller than the
    # page can never be satisfied and would break every page forever.
    reserve = max(_measure(head, width), _HEAD_RESERVE) + (
        _indivisible_height(body_flow, width) or _BODY_RESERVE
    )
    return [CondPageBreak(min(reserve, FRAME_HEIGHT - 4.0)),
            KeepTogether(head), *body_flow, Spacer(1, 14)]


# ==========================================================================
# Cover
# ==========================================================================

_MODE_LABEL = {
    "simple": "Signals digest",
    "full": "Competitor & industry briefing",
}

_BASIS_LABELS = (
    ("competitor_handles", "Competitor handles"),
    ("competitor_posts", "Competitor posts"),
    ("news_items", "Industry news items"),
    ("viral_trends", "Viral social trends"),
)


def _masthead(report: dict, width: float) -> list[Any]:
    """The top of the document, and the SAME top the screen draws.

    This was a full cover page: a 30pt title on an otherwise empty sheet, a
    stamp block of window dates, a "WHAT THIS REPORT STANDS ON" KPI row, the
    standing caveats, then a page break. The screen showed none of it, so the
    file and the page it was downloaded from opened differently, and a reader
    had no way to tell which one was the report.

    They are one document now. The counts that were on the cover are the first
    SECTION ("AT A GLANCE"), which both sides render from the same envelope,
    and the caveats are the last one. What is left here is a masthead: a rule,
    the title, who it is for, and which days it covers.
    """
    meta = _dget(report, "meta", {})
    window = _dget(meta, "window", {})

    title = sanitize_text(_dget(meta, "title", ""), "SMAA Social Intelligence")
    brand = sanitize_text(_dget(meta, "brand", ""), "42 Abu Dhabi")
    scope_label = sanitize_text(_dget(meta, "scope_label", ""))
    item = _dget(meta, "item", None)
    item = item if isinstance(item, dict) else None

    # The accent bar. The one piece of pure decoration here, and it earns its
    # place the same way it does on screen: it makes the block read as the top
    # of a document rather than as the first row of a list.
    bar = Table([[""]], colWidths=[28], rowHeights=[3.5])
    bar.hAlign = "LEFT"      # a Table centres itself in the frame by default
    bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))

    flow: list[Any] = [bar, Spacer(1, 9),
                       Paragraph(_xml_escape(title), S["cover_title"])]

    bits = [_xml_escape(brand)]
    if scope_label:
        bits.append(_xml_escape(scope_label))
    if item is not None:
        kind = sanitize_text(_dget(item, "kind", ""), "item")
        name = sanitize_text(_dget(item, "name", ""))
        bits.append(_xml_escape(" ".join(x for x in (f"one {kind}", name) if x)))
    flow.append(Spacer(1, 4))
    flow.append(Paragraph("&#160;·&#160;".join(bits), S["eyebrow"]))

    frm = _fmt_date(_dget(window, "from", ""), "")
    to = _fmt_date(_dget(window, "to", ""), "")
    window_txt = " – ".join(x for x in (frm, to) if x) or "window not recorded"
    flow.append(Spacer(1, 7))
    flow.append(Paragraph(_xml_escape(window_txt), S["mono_small"]))

    flow.append(Spacer(1, 10))
    flow.append(_hrule(INK, 1.0, width))
    flow.append(Spacer(1, 14))
    return flow


# ==========================================================================
# Page furniture
# ==========================================================================


class _NumberedCanvas(_canvas.Canvas):
    """Two-pass page furniture: pages are buffered so "Page N of M" has a real
    M, and so the header/footer can be drawn *after* the flow is known."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._header_left: str = kwargs.pop("header_left", "")
        self._header_right: str = kwargs.pop("header_right", "")
        self._footer_left: str = kwargs.pop("footer_left", "")
        kwargs.setdefault("invariant", 1)
        super().__init__(*args, **kwargs)
        self._saved_states: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802 (reportlab API)
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_furniture(total)
            super().showPage()
        self._saved_states = []
        super().save()

    def _draw_furniture(self, total: int) -> None:
        page = getattr(self, "_pageNumber", 1)
        width, height = PAGE_SIZE

        # Page one carries no header furniture. It used to be a cover sheet
        # with a teal spine and a "42 <title>" wordmark; the masthead now
        # stands there instead, the same masthead the screen draws, and a
        # spine plus a bar plus a title twice over is three of one thing.
        self.setStrokeColor(BORDER)
        self.setLineWidth(0.4)
        if page > 1:
            self.setFont(FONT, 7.4)
            self.setFillColor(MUTED)
            self.drawString(MARGIN_X, height - 40, self._header_left)
            if self._header_right:
                self.drawRightString(width - MARGIN_X, height - 40,
                                     self._header_right)
            self.line(MARGIN_X, height - 48, width - MARGIN_X, height - 48)

        self.line(MARGIN_X, 42, width - MARGIN_X, 42)
        self.setFont(FONT_MONO, 7.0)
        self.setFillColor(MUTED)
        self.drawString(MARGIN_X, 31, self._footer_left)
        self.drawRightString(width - MARGIN_X, 31, f"Page {page} of {total}")


class _ReportDoc(BaseDocTemplate):
    """Two page templates: a bare cover, then the running body."""

    def __init__(self, target: Any, title: str, author: str) -> None:
        super().__init__(
            target,
            pagesize=PAGE_SIZE,
            leftMargin=MARGIN_X,
            rightMargin=MARGIN_X,
            topMargin=MARGIN_TOP,
            bottomMargin=MARGIN_BOTTOM,
            title=title,
            author=author,
            subject="Social intelligence briefing",
            creator="42 Studio",
            invariant=1,
            allowSplitting=1,
        )
        frame_kwargs = dict(
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0
        )
        cover_frame = Frame(
            MARGIN_X,
            MARGIN_BOTTOM,
            CONTENT_WIDTH,
            PAGE_SIZE[1] - MARGIN_BOTTOM - MARGIN_TOP,
            id="cover",
            **frame_kwargs,
        )
        body_frame = Frame(
            MARGIN_X,
            MARGIN_BOTTOM,
            CONTENT_WIDTH,
            PAGE_SIZE[1] - MARGIN_BOTTOM - MARGIN_TOP,
            id="body",
            **frame_kwargs,
        )
        self.addPageTemplates(
            [
                PageTemplate(id="cover", frames=[cover_frame], pagesize=PAGE_SIZE),
                PageTemplate(id="body", frames=[body_frame], pagesize=PAGE_SIZE),
            ]
        )


# ==========================================================================
# Public API
# ==========================================================================


def render_pdf(report: dict) -> bytes:
    """Render a CONTRACT-shaped report dict to PDF bytes.

    Tolerant by design: a missing key, a ``None`` where a string belongs or an
    unknown section ``kind`` degrades that one piece and still produces a
    document. It never invents a value to fill a gap.
    """
    if not isinstance(report, dict):
        report = {}
    meta = _dget(report, "meta", {})
    window = _dget(meta, "window", {})

    title = sanitize_text(_dget(meta, "title", ""), "SMAA Social Intelligence Report")
    brand = sanitize_text(_dget(meta, "brand", ""), "42 Abu Dhabi")
    frm = _fmt_date(_dget(window, "from", ""), "")
    to = _fmt_date(_dget(window, "to", ""), "")
    window_txt = " – ".join(x for x in (frm, to) if x)

    sections = [s for s in _as_list(_dget(report, "sections", [])) if isinstance(s, dict)]

    # Page one is the masthead AND the first sections: no cover sheet, and no
    # page break behind it. `NextPageTemplate` only decides what page TWO looks
    # like (the running header), so it costs nothing here.
    story: list[Any] = [NextPageTemplate("body")]
    story.extend(_masthead(report, CONTENT_WIDTH))
    if sections:
        for index, section in enumerate(sections, start=1):
            story.extend(_section(section, index, len(sections), CONTENT_WIDTH))
    else:
        story.append(Paragraph("No sections", S["h1"]))
        story.append(_hrule(RULE, 0.6, CONTENT_WIDTH))
        story.append(Spacer(1, 6))
        story.append(
            Paragraph(
                _esc(
                    "This report was generated with no sections. Nothing has been "
                    "inferred to fill the gap."
                ),
                S["muted"],
            )
        )

    buffer = io.BytesIO()
    doc = _ReportDoc(buffer, title=title, author=brand)

    footer_left = sanitize_text(f"{brand} · social intelligence")
    header_left = title
    header_right = window_txt

    def _maker(*args: Any, **kwargs: Any) -> _NumberedCanvas:
        kwargs["header_left"] = header_left
        kwargs["header_right"] = header_right
        kwargs["footer_left"] = footer_left
        return _NumberedCanvas(*args, **kwargs)

    doc.build(story, canvasmaker=_maker)
    return buffer.getvalue()


_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


def _name_part(value: Any, limit: int = 24) -> str:
    """One hyphen-joined ASCII fragment of a filename, or ''.

    The router sanitises the header on the way out, but a name that only
    survives BECAUSE of that is a name nobody can predict from the report — so
    the restriction is applied here, at the source."""
    out = _FILENAME_UNSAFE.sub("-", sanitize_text(value)).strip("-").lower()
    return out[:limit].strip("-")


def suggested_filename(report: dict) -> str:
    """A safe ASCII download name that says which report this is.

        42-social-listening-full-2026-08-20.pdf                 (all streams)
        42-social-listening-competitors-full-2026-08-20.pdf     (one stream)
        42-report-trend-880-spidermanbrandnewday-full-...pdf    (one item)

    Only ``[A-Za-z0-9._-]`` ever appears: the name lands inside a quoted
    Content-Disposition header, and a filename is not a place to discover that
    a trend title contained a quotation mark.
    """
    meta = _dget(report, "meta", {}) if isinstance(report, dict) else {}
    mode = _name_part(_dget(meta, "mode", "")) or "report"

    stamp: Optional[str] = None
    for candidate in (
        _dget(meta, "generated_at", ""),
        _dget(_dget(meta, "window", {}), "to", ""),
    ):
        parsed = _parse_dt(candidate)
        if parsed is not None:
            stamp = parsed.strftime("%Y-%m-%d")
            break
    if stamp is None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    item = _dget(meta, "item", None)
    if isinstance(item, dict) and _dget(item, "id", None) is not None:
        kind = _name_part(_dget(item, "kind", ""), 12) or "item"
        ident = _name_part(_dget(item, "id", ""), 12)
        slug = _name_part(_dget(item, "slug", ""), 28) or _name_part(
            _dget(item, "label", ""), 28)
        parts = [p for p in ("42-report", kind, ident, slug, mode, stamp) if p]
        return "-".join(parts) + ".pdf"

    scope = _name_part(_dget(meta, "scope", ""), 16)
    if scope and scope != "all":
        return f"42-social-listening-{scope}-{mode}-{stamp}.pdf"
    return f"42-social-listening-{mode}-{stamp}.pdf"
