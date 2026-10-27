"""The narrative pass over an assembled social-listening report.

``social_report.build_report`` produces every section from the database. Two of
them cannot be produced that way — the opening "What matters now" and the
closing recommended actions are *writing*, not counting — so the data pass
emits them as empty slots and this module fills them in ONE model call.

WHY THIS MODULE EXISTS AT ALL, AND WHY IT IS SHAPED LIKE THIS
-------------------------------------------------------------
A social intelligence report is read as *measurement*. The single worst failure
this feature can have is a confident summary containing a number nobody
counted — "engagement is up 34% across the sector" reads exactly like the real
KPIs printed two pages above it, and somebody will act on it.

So the model never sees a database row. It is handed the ALREADY-ASSEMBLED
numbers — the section bodies, their one-line basis statements, the sample
sizes — and is told, in the prompt, that every figure it cites must already
appear in that digest. It has nothing to re-interpret and no raw data to
re-aggregate, which is the only structural way to stop the fabrication rather
than asking politely.

The measured reality this is written against (checked 2026-08-20, last 30 days):
competitor posts 20, **distinct competitor handles 2**, news items 385, viral
trends 95. Two accounts is not "the competitive landscape", so the prompt
forbids that claim outright at small sample sizes.

DEGRADING IS A FEATURE. A narrative failure must never fail the download: if
the model is unreachable the two sections come back ``available: False`` with a
plain reason and the rest of the report — which is the measured part, the part
that matters — renders exactly as it would have.

The audience rule for anything the model proposes 42 should POST is imported,
not restated: ``PLAIN_LANGUAGE_RULE`` is one object by design (there is a test
asserting identity) so that fixing the rule fixes every prompt at once.
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any, Optional

from ..config import config
from . import trend_scoring
from .trend_ideation import PLAIN_LANGUAGE_RULE
from .vertex_client import VertexBudgetExceededError, gemini_text

log = logging.getLogger("archivist.social_report_narrative")


# ---------------------------------------------------------------------------
# The two slots
# ---------------------------------------------------------------------------

EXEC_SUMMARY_ID = "executive_summary"
RECOMMENDATIONS_ID = "recommendations"

# The data pass owns the section ids; this pass has to find its two slots in a
# report it did not build. Matching on the id ALONE would break the whole
# narrative over a rename, so each slot also matches on its title. Both lists
# are deliberately narrow — nothing here may accidentally claim "Top Content"
# or "Reputation / Risk".
_EXEC_IDS = {EXEC_SUMMARY_ID, "exec_summary", "summary", "executive"}
_EXEC_TITLES = ("executive summary",)
_REC_IDS = {RECOMMENDATIONS_ID, "recommendation", "next_steps", "what_to_do"}
_REC_TITLES = ("recommendation", "what 42 should do", "next steps")

MIN_FINDINGS = 3
MAX_FINDINGS = 5
MAX_RECOMMENDATIONS = 6

# An item report writes ONE short section from a digest of a handful of
# numbers, on the fast model. 6000 tokens there buys nothing but latency, and
# latency is the whole point of the per-item download.
ITEM_MAX_TOKENS = 3000

# Clamps on what comes back. A finding is one sentence, not an essay; a
# recommendation that does not fit in a line is not concrete enough to act on.
_MAX_FINDING_CHARS = 400
_MAX_ACTION_CHARS = 400

# How much of the assembled report reaches the prompt. Wide sections are
# truncated rather than dropped:
# the model needs to know a table HAS 40 rows even if it only reads 12 of them.
_MAX_TABLE_ROWS = 12
_MAX_SIGNAL_ITEMS = 8
_MAX_BULLETS = 12
_MAX_CHART_POINTS = 24
_MAX_TEXT_CHARS = 600

NARRATIVE_BASIS = (
    "written by the report model from the assembled sections in this report — "
    "no new numbers"
)

# Why the model was skipped or gave up. Plain language, because these render
# in place of the section.
NOTHING_MEASURED = (
    "no measured activity in this window — there is nothing to summarise"
)
UNREADABLE = (
    "the report model returned an answer this report could not read — "
    "regenerate the report to try again"
)
_GENERIC_FAILURE = (
    "the report model could not be reached, so this section was not written. "
    "Everything else in this report is measured and unaffected. "
)


# ---------------------------------------------------------------------------
# The rules the prompt is built from
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a social-media intelligence analyst writing the opening and closing "
    "sections of a report for the marketing team of 42 Abu Dhabi, a tuition-free "
    "coding school in Abu Dhabi where students learn by building projects with "
    "each other instead of in lectures.\n"
    "You are bound by the data you are given and nothing else. You are not a "
    "forecaster and not a copywriter. Your job is to say what the numbers show, "
    "how thin they are, and what to do on Monday."
)

# THE guard against a confident fabricated summary. Asserted by a test, because
# the day someone softens this paragraph is the day the report starts lying in
# a font that looks like measurement.
NO_INVENTED_NUMBERS_RULE = (
    "NEVER INVENT A NUMBER.\n"
    "Every figure you cite must appear in the data above. Do not compute a new "
    "percentage, do not average anything, do not estimate, do not round a number "
    "into a different number, and do not describe a rise or a fall you cannot "
    "see in the values printed above. If you want to make a point you have no "
    "number for, make it qualitatively or leave it out.\n"
    "\"Engagement is concentrated in a handful of posts\" is fine if the table "
    "shows that. \"Engagement is up 34%\" is forbidden unless 34% is printed "
    "above, exactly.\n"
    "\n"
    "A NUMBER BELONGS TO THE LINE IT IS PRINTED ON.\n"
    "Do not carry a figure out of one section into a sentence about another, and "
    "do not build a new claim by combining two figures that were printed "
    "separately. If a section's `basis` line already states the count for that "
    "section (\"20 of 95 items carry a flag\"), that is the count — a row count "
    "of the table printed under it is not, and neither is a number that happens "
    "to appear in a different section."
)

_SAMPLE_SIZE_RULE = (
    "SAY HOW THIN THE DATA IS. THIS IS A SMALL SAMPLE AND THE READER MUST KNOW.\n"
    "- State the sample size in ONE of the findings, using the counts in "
    "MEASURED BASIS. NOT the first one: the reader sees the counts as figures "
    "at the top of the page before reaching a word of this, and the opening "
    "line is the one slot that has to say what to do about this week.\n"
    "- With fewer than five watched competitor accounts you may NOT write about "
    "\"the competitive landscape\", \"the market\", \"the sector\", \"the industry\" "
    "or \"share of voice\" as if it had been measured. Name the handful of "
    "accounts actually watched instead, and say it is a handful.\n"
    "- A section marked NOT AVAILABLE has nothing behind it. You may say that it "
    "is missing and what it would take to fill it. You may not fill it in.\n"
    "- Anything marked AI-ESTIMATED is a model's judgement, not a measurement. "
    "If you cite it, say so in the same sentence.\n"
    "- Tone/sentiment in this report is AI-assessed from post text. Nothing here "
    "measures how the audience felt — no comment text is stored. Never write "
    "\"audience sentiment\".\n"
    "- Report tone the way it is printed: as a COUNT OF ITEMS ASSESSED. Never as "
    "a percentage or a share, not even when the percentage is arithmetically "
    "obvious — \"40 of 40 assessed items read as neutral\" is right, \"100% "
    "neutral\" is wrong, and \"68.3% positive\" is the failure this whole report "
    "is built to avoid.\n"
    "- Instagram does not report shares. Never mention shares."
)

_TASK_RULE = (
    "WRITE TWO THINGS.\n"
    "\n"
    f"1) EXECUTIVE SUMMARY — {MIN_FINDINGS} to {MAX_FINDINGS} findings.\n"
    "   What changed, what is growing, what needs attention. One sentence each, "
    "two at most. Every finding must be traceable: `section` is the exact TITLE "
    "of the section above that the finding came from. Lead with the finding that "
    "would change what the team does this week. Do not repeat the same number in "
    "two findings. Keep competitor observations and industry signals separate: "
    "do not use an industry story as proof that a competitor post worked, or the "
    "other way round.\n"
    "\n"
    f"2) RECOMMENDATIONS — 3 to {MAX_RECOMMENDATIONS} things 42 Abu Dhabi should "
    "actually do next.\n"
    "   `action` is the thing to do, specific enough to start today. `why` is the "
    "evidence from above that says to do it.\n"
    "   This is a campus full of students with phones, a building people can film "
    "in, and admissions deadlines. A usable action looks like \"film X at Y\", "
    "\"reply to the comments on Z\", \"post the A format before B closes\", \"stop "
    "doing C\", or \"start watching more competitor accounts, because two is not "
    "enough to measure anything\". An unusable action looks like \"increase brand "
    "awareness\", \"leverage engagement\" or \"optimise the content strategy\".\n"
    "   Do not recommend paid media, agencies, tools or dashboards. Recommend "
    "things this team can do themselves.\n"
    "   A content opportunity is not automatically a proven content gap. This "
    "report does not track 42 Abu Dhabi's own feed, so call it something to test, "
    "not something 42 is missing."
)


# --- the item report ------------------------------------------------------
# A report about ONE post or ONE trend has a handful of numbers instead of
# fourteen sections, which makes padding MORE tempting, not less. Same
# non-negotiables (no invented number, tone is not audience sentiment, no
# shares), different job: no findings, one short set of actions about this item.
_ITEM_EVIDENCE_RULE = (
    "THIS IS A REPORT ABOUT ONE ITEM. THE EVIDENCE IS THIN AND THE READER MUST "
    "KNOW IT.\n"
    "- You are looking at everything that is stored about this item. There is "
    "no more.\n"
    "- A section marked NOT AVAILABLE has nothing behind it. Say it is missing "
    "if it matters. Never fill it in.\n"
    "- Anything marked AI-ESTIMATED is a model's judgement, including the fit "
    "score, the theme and the suggested angle. If you lean on one, say in the "
    "same sentence that it is a judgement.\n"
    "- A rank (\"3rd of 20\") is measured and you may cite it exactly as "
    "printed. Do not turn it into a percentile, a share or a trend.\n"
    "- Tone here is AI-assessed from the publisher's own text. Nothing measures "
    "how an audience felt - no comment text is stored. Never write \"audience "
    "sentiment\".\n"
    "- Instagram does not report shares. Never mention shares.\n"
    "- One item is not evidence of a pattern. Do not write about \"the market\", "
    "\"the sector\", \"the trend across competitors\" or what \"audiences want\"."
)

_ITEM_TASK_RULE = (
    "WRITE ONE THING.\n"
    "\n"
    "RECOMMENDATIONS - 2 to 4 things 42 Abu Dhabi should do about THIS ITEM, "
    "and nothing else.\n"
    "  `action` is the thing to do, specific enough to start today. `why` is "
    "the evidence from above that says to do it - name the section or the "
    "figure.\n"
    "  At least one action must be about whether to make something from this "
    "item, and say plainly if the answer is no: \"skip it\" is a valid "
    "recommendation and a better one than a vague yes.\n"
    "  This is a campus full of students with phones, a building people can "
    "film in, and admissions deadlines. A usable action looks like \"film X at "
    "Y this week\", \"reply to the comments on Z\", \"post the A format before B "
    "closes\", or \"do not touch this one, because C\". An unusable action looks "
    "like \"increase brand awareness\", \"leverage engagement\" or \"monitor the "
    "trend\".\n"
    "  Do not recommend paid media, agencies, tools or dashboards.\n"
    "\n"
    "Return `executive_summary` as an EMPTY ARRAY. This report has no executive "
    "summary section and anything you write there is thrown away."
)


# --- plain English, because a report nobody can read is not a report -------
# The standing house rule for every 42-studio screen: a non-technical reader,
# zero jargon. It was missing here, and the gap showed in production. A real
# summary line read "Competitor content volume was extremely low, ... a 94%
# decrease ..., making any competitive analysis indicative at best", and a real
# recommendation cited "the `share_of_voice` and `data_basis` sections" by their
# internal ids. Both are accurate. Neither is readable by the person this report
# is for, and accuracy is wasted on a sentence nobody finishes.
#
# This rule governs THE FINDINGS AND THE ACTIONS THEMSELVES, which is what makes
# it different from `_audience_rule()` below: that one binds any post copy the
# model proposes and leaves the analysis free to sound like analysis.
_PLAIN_REPORT_RULE = (
    "WRITE IT THE WAY YOU WOULD SAY IT OUT LOUD. THIS IS NOT OPTIONAL.\n"
    "The person reading this has never worked in marketing and has no interest "
    "in learning the vocabulary. Use the words they already own.\n"
    "- ONE SENTENCE per finding and per action. Short. Never two.\n"
    "- BANNED WORDS, in a finding and in an action alike: share of voice, "
    "engagement rate, impressions, reach, content volume, competitive "
    "landscape, benchmark, KPI, metric, data set, sample size, indicative, "
    "leverage, optimise, drive, surface, cadence, vertical, strategy.\n"
    "- NEVER name a section of this report inside a sentence, and never quote a "
    "section id such as `share_of_voice`. Say the thing itself, not where it is "
    "filed.\n"
    "- Numbers are welcome. The words around them are where the jargon hides.\n"
    "  RIGHT: \"Only 2 posts came in from the 2 accounts we watch, down from "
    "31.\"\n"
    "  WRONG: \"Competitor content volume was extremely low, a 94% decrease "
    "from the prior period, making any competitive analysis indicative at "
    "best.\"\n"
    "- `why` is ONE short sentence of plain evidence. If the only honest "
    "version of it needs those banned words, leave `why` empty instead."
)


def _audience_rule() -> str:
    """The plain-language rule, framed for a report rather than for a caption.

    Imported, never restated — see the module docstring."""
    return (
        "IF YOU PROPOSE A POST, A HOOK OR A LINE OF COPY, IT MUST OBEY THIS:\n"
        + PLAIN_LANGUAGE_RULE
    )


# ---------------------------------------------------------------------------
# The digest — the ONLY thing the model is allowed to reason from
# ---------------------------------------------------------------------------

def _text(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    """Collapse whitespace and clamp. Section prose arrives from the data pass
    already clean; this is belt-and-braces so one long paragraph cannot push the
    rest of the report out of the prompt."""
    out = " ".join(str(value or "").split())
    return out[:limit]


def _fmt_kpis(body: dict) -> list[str]:
    lines = []
    for item in (body.get("items") or [])[:_MAX_BULLETS]:
        if not isinstance(item, dict):
            continue
        line = f"  KPI  {_text(item.get('label'), 120)} = {_text(item.get('value'), 120)}"
        delta = item.get("delta")
        if delta not in (None, ""):
            direction = _text(item.get("delta_dir"), 8) or "flat"
            line += f"  (change vs previous window: {_text(delta, 60)}, {direction})"
        note = item.get("note")
        if note:
            line += f"  [{_text(note, 160)}]"
        lines.append(line)
    return lines


def _fmt_table(body: dict) -> list[str]:
    cols = [_text(c, 60) for c in (body.get("columns") or [])]
    rows = body.get("rows") or []
    lines = [f"  TABLE columns: {' | '.join(cols)}"]
    for row in rows[:_MAX_TABLE_ROWS]:
        cells = [_text(c, 80) for c in (row if isinstance(row, (list, tuple)) else [row])]
        lines.append("    " + " | ".join(cells))
    if len(rows) > _MAX_TABLE_ROWS:
        # The count matters even when the rows are cut: "40 rows" is a fact,
        # "12 rows" would be a fact invented by reading a truncated table.
        #
        # The qualifier is not decoration. On the first live run the model read
        # "16 rows in total" off a truncated Reputation / Risk table and wrote
        # "an AI model flagged 16 of 95 viral trends" — while that section's own
        # basis line said TWENTY of 95. Both numbers were really in the digest;
        # the fabrication was in combining them. So every row count now says out
        # loud what it counts.
        # No number here on purpose. The previous version printed the row
        # total, and the model lifted it: a Reputation / Risk table of 15
        # flagged trends plus 7 spike days became "the system has flagged 22
        # trends". A count the model must not use should not be in front of
        # it — the section's own basis line carries the real counts.
        lines.append("    ... some rows are not shown. Do NOT count the rows of "
                     "this table or state how many there are; this section's "
                     "basis line above is what the section actually measured.")
    return lines


def _fmt_chart(body: dict) -> list[str]:
    labels = [_text(x, 40) for x in (body.get("labels") or [])][:_MAX_CHART_POINTS]
    lines = [f"  CHART ({_text(body.get('chart'), 20) or 'bar'}) "
             f"y = {_text(body.get('y_label'), 60)}",
             f"    x: {', '.join(labels)}"]
    for series in (body.get("series") or [])[:4]:
        if not isinstance(series, dict):
            continue
        values = [str(v) for v in (series.get("values") or [])][:_MAX_CHART_POINTS]
        lines.append(f"    {_text(series.get('name'), 60)}: {', '.join(values)}")
    return lines


def _fmt_signals(body: dict) -> list[str]:
    lines: list[str] = []
    for group in (body.get("groups") or []):
        if not isinstance(group, dict):
            continue
        items = [i for i in (group.get("items") or []) if isinstance(i, dict)]
        lines.append(f"  GROUP {_text(group.get('group'), 80)} "
                     f"({len(items)} item(s))")
        for item in items[:_MAX_SIGNAL_ITEMS]:
            metrics = ", ".join(
                f"{_text(m.get('label'), 40)}: {_text(m.get('value'), 40)}"
                for m in (item.get("metrics") or []) if isinstance(m, dict)
            )
            line = f"    - {_text(item.get('name'), 140)}"
            if metrics:
                line += f"  [{metrics}]"
            tone = item.get("sentiment")
            if isinstance(tone, dict) and tone.get("label"):
                line += f"  (tone, AI-assessed: {_text(tone.get('label'), 40)})"
            lines.append(line)
        if len(items) > _MAX_SIGNAL_ITEMS:
            lines.append(f"    ... {len(items)} in total, {_MAX_SIGNAL_ITEMS} shown "
                         f"(a count of the items listed in this group only)")
    return lines


def _fmt_body(kind: str, body: Any) -> list[str]:
    """One section's body as flat text. Unknown kinds are skipped rather than
    guessed at — a future body shape must not become a hallucinated number."""
    if not isinstance(body, dict):
        return []
    if kind == "prose":
        return [f"  {_text(p)}" for p in (body.get("paragraphs") or [])[:_MAX_BULLETS]]
    if kind == "bullets":
        return [f"  - {_text(i)}" for i in (body.get("items") or [])[:_MAX_BULLETS]]
    if kind == "kpis":
        return _fmt_kpis(body)
    if kind == "table":
        return _fmt_table(body)
    if kind == "chart":
        return _fmt_chart(body)
    if kind == "signals":
        return _fmt_signals(body)
    return []


def summarise_report(report: dict) -> str:
    """The assembled report as the digest the model reasons from.

    Numbers only, plus each section's own basis line. No database rows, no
    captions, no permalinks — nothing the model could re-aggregate into a
    figure that was never counted."""
    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    basis = report.get("basis") if isinstance(report.get("basis"), dict) else {}
    window = meta.get("window") if isinstance(meta.get("window"), dict) else {}

    item = meta.get("item") if isinstance(meta.get("item"), dict) else None
    scope = _text(meta.get("scope"), 40).lower()
    scope_label = _text(meta.get("scope_label"), 80)

    lines = [
        f"REPORT: {_text(meta.get('title'), 120) or 'Social-listening report'}",
        f"BRAND: {_text(meta.get('brand'), 80) or '42 Abu Dhabi'}",
        f"WINDOW: last {window.get('days')} day(s), "
        f"{_text(window.get('from'), 40)} to {_text(window.get('to'), 40)}",
    ]
    # The scope is stated to the model for the same reason it is stated on the
    # cover: a one-stream number described as the whole picture is a
    # fabrication even when every digit is right.
    if scope and scope != "all":
        lines.append(f"SCOPE: {scope_label or scope} ONLY. The other signal "
                     "streams are not in this report; do not write about them.")
    if item is not None:
        lines.append(
            f"THIS REPORT IS ABOUT ONE ITEM: {_text(item.get('label'), 160)} "
            f"({_text(item.get('stream_label'), 80) or scope_label}, "
            f"{_text(item.get('kind'), 20)} #{_text(item.get('id'), 20)}). "
            "The counts below are the population it is RANKED AGAINST, not "
            "measurements of the item.")
    lines += [
        "",
        "SAMPLE SIZES (this is the whole sample — there is no more data than "
        "this). This block is a header, NOT a section: never cite it in "
        "`section`.",
    ]
    # A stream that is out of scope carries None, not 0. Printing "None" (or
    # worse, "0") would invite a claim that we looked and found nothing.
    for key, label in (("competitor_handles", "competitor accounts watched:"),
                       ("competitor_posts", "competitor posts collected: "),
                       ("news_items", "industry news items:        "),
                       ("viral_trends", "viral social trends tracked:")):
        if basis.get(key) is not None:
            lines.append(f"  {label} {basis.get(key)}")
    for note in (basis.get("notes") or [])[:_MAX_BULLETS]:
        lines.append(f"  note: {_text(note, 300)}")

    lines += ["", "SECTIONS:"]
    for section in _sections(report):
        if _slot_of(section) is not None:
            continue        # the two slots this pass is about to write
        title = _text(section.get("title"), 120)
        sid = _text(section.get("id"), 60)
        if not section.get("available"):
            lines.append(
                f"[{sid}] {title} — NOT AVAILABLE: "
                f"{_text(section.get('unavailable_reason'), 300) or 'no data'}"
            )
            continue
        tag = "AI-ESTIMATED" if section.get("estimated") else "MEASURED"
        lines.append(f"[{sid}] {title}  ({tag})")
        lines.append(f"  basis: {_text(section.get('basis'), 300)}")
        lines += _fmt_body(str(section.get("kind") or ""), section.get("body"))
    return "\n".join(lines)


def is_item_report(report: Any) -> bool:
    """True for a report about ONE post or ONE trend.

    `meta.item` is the marker the data pass sets; nothing else in the contract
    distinguishes the two, and guessing from the section ids would break the
    first time a section is renamed."""
    meta = report.get("meta") if isinstance(report, dict) else None
    return isinstance(meta, dict) and isinstance(meta.get("item"), dict)


def build_narrative_prompt(report: dict) -> str:
    """The one prompt. Digest first so that "the data above" in every rule below
    it means exactly one thing.

    Two shapes, one set of non-negotiables: an item report has no executive
    summary and no window-wide claims to make, so it gets its own task and its
    own thin-evidence rule — but `NO_INVENTED_NUMBERS_RULE` is the same object
    in both, because the failure it guards against is the same failure."""
    item = is_item_report(report)
    intro = (
        "Here is everything this system stores about ONE item from the trends "
        "feed, already measured or already labelled as a model's judgement. You "
        "are writing the closing recommendation about it."
        if item else
        "Here is the finished, measured body of a social-listening report. "
        "Everything in it has already been counted or already been labelled as a "
        "model's judgement. You are writing its first and last sections."
    )
    return "\n\n".join([
        intro,
        "===== DATA =====",
        summarise_report(report),
        "===== END OF DATA =====",
        NO_INVENTED_NUMBERS_RULE,
        _ITEM_EVIDENCE_RULE if item else _SAMPLE_SIZE_RULE,
        _ITEM_TASK_RULE if item else _TASK_RULE,
        # Last of the three content rules and deliberately after the task: it is
        # about HOW every line above is worded, so it reads as the final pass.
        _PLAIN_REPORT_RULE,
        _audience_rule(),
        "Return JSON only.",
    ])


_SCHEMA = {
    "type": "object",
    # Every field is `required`: Gemini silently drops properties that are not,
    # and a finding with no section is a finding nobody can check.
    "properties": {
        "executive_summary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": {"type": "string"},
                    "section": {"type": "string"},
                },
                "required": ["finding", "section"],
            },
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["action", "why"],
            },
        },
    },
    "required": ["executive_summary", "recommendations"],
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _citable(report: Any) -> dict[str, str]:
    """{normalised title or id -> the title as printed} for every real section.

    A "(see: X)" that names nothing in the document is worse than no citation:
    it looks like provenance and cannot be followed. On the first live run the
    model cited "MEASURED BASIS", which is a heading in the digest and not a
    section of the report."""
    out: dict[str, str] = {}
    for section in _sections(report if isinstance(report, dict) else {}):
        title = _text(section.get("title"), 120)
        if not title:
            continue
        out[_norm_cite(title)] = title
        sid = _text(section.get("id"), 60)
        if sid:
            out.setdefault(_norm_cite(sid), title)
    return out


def _norm_cite(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def parse_narrative(
    raw: Any, citable: Optional[dict[str, str]] = None,
) -> tuple[list[str], list[str]]:
    """(findings, recommendations) as render-ready bullet strings.

    Tolerant on purpose: a half-usable answer is worth printing, and either half
    may come back empty — the caller marks only the empty half unavailable.

    `citable` is the map from `_citable()`. When it is supplied, a citation that
    does not name a real section is DROPPED and the finding is kept: an
    unfollowable pointer is removed, a fact is not."""
    if not isinstance(raw, dict) or raw.get("_raw") is not None:
        return [], []

    findings: list[str] = []
    for item in (raw.get("executive_summary") or [])[:MAX_FINDINGS]:
        if isinstance(item, dict):
            text = _text(item.get("finding"), _MAX_FINDING_CHARS)
            where = _text(item.get("section"), 120)
        else:
            text, where = _text(item, _MAX_FINDING_CHARS), ""
        if not text:
            continue
        if where and citable is not None:
            resolved = citable.get(_norm_cite(where))
            if resolved is None:
                log.info("[social-report] dropped citation to a non-section: %r",
                         where)
            where = resolved or ""
        findings.append(f"{text} (see: {where})" if where else text)

    actions: list[str] = []
    for item in (raw.get("recommendations") or [])[:MAX_RECOMMENDATIONS]:
        if isinstance(item, dict):
            action = _text(item.get("action"), _MAX_ACTION_CHARS)
            why = _text(item.get("why"), _MAX_ACTION_CHARS)
        else:
            action, why = _text(item, _MAX_ACTION_CHARS), ""
        if not action:
            continue
        # ONE STRING, TWO HALVES, AND THE JOIN HAS TO BE UNAMBIGUOUS.
        # `body.items` is a list of strings by contract, so the action and its
        # evidence travel glued together and every reader has to split them
        # back apart. The glue used to be " \u2014 ", and it was ambiguous: a
        # real action read "Use the growing sound 'Garee2a Awy (Live)
        # \u2014 TUL8TE' for a reel", whose own title carries that character, so
        # the first split point landed mid-title and the screen printed half a
        # sentence. A newline cannot collide: `_text` above runs the model's
        # answer through `" ".join(...split())`, so neither half can contain
        # one. The PDF is unaffected (`sanitize_text` draws it as a space).
        actions.append(f"{action}\n{why}" if why else action)

    return findings, actions


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

def _sections(report: dict) -> list[dict]:
    raw = report.get("sections")
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _slot_of(section: dict) -> Optional[str]:
    """Which narrative slot this section is, or None. Id first, title second."""
    sid = str(section.get("id") or "").strip().lower()
    title = str(section.get("title") or "").strip().lower()
    if sid in _EXEC_IDS or any(t in title for t in _EXEC_TITLES):
        return EXEC_SUMMARY_ID
    if sid in _REC_IDS or any(t in title for t in _REC_TITLES):
        return RECOMMENDATIONS_ID
    return None


def _slots(report: dict) -> dict[str, dict]:
    """The two sections this pass owns, keyed by slot. First match wins so a
    report that somehow carries two Recommendations sections writes the first."""
    found: dict[str, dict] = {}
    for section in _sections(report):
        slot = _slot_of(section)
        if slot is not None and slot not in found:
            found[slot] = section
    return found


def _fill(section: dict, items: list[str]) -> None:
    section["available"] = True
    section["unavailable_reason"] = None
    section["estimated"] = True          # a model wrote this; label it as such
    section["basis"] = NARRATIVE_BASIS
    section["kind"] = "bullets"
    section["body"] = {"items": items}


def _blank(section: dict, reason: str) -> None:
    section["available"] = False
    section["unavailable_reason"] = reason
    section["estimated"] = True
    section["basis"] = NARRATIVE_BASIS
    section["kind"] = "bullets"
    section["body"] = {"items": []}


def _already_written(section: dict) -> bool:
    """Has this pass already filled this slot? ``basis`` is the marker — the
    data pass has no reason to ever write that exact line."""
    body = section.get("body")
    return bool(
        section.get("available")
        and section.get("basis") == NARRATIVE_BASIS
        and isinstance(body, dict) and (body.get("items") or [])
    )


def _explain(exc: BaseException) -> str:
    """Why the narrative is missing, in words the owner can act on."""
    if isinstance(exc, VertexBudgetExceededError):
        return (_GENERIC_FAILURE + "The Vertex spend cap was reached — raise "
                "VEO_BUDGET_USD or wait for the meter to reset.")
    if trend_scoring._is_unavailable(exc):
        return _GENERIC_FAILURE + trend_scoring._explain_unavailable(exc)
    return _GENERIC_FAILURE + f"({str(exc)[:160]})"


def _has_measured_anything(report: dict) -> bool:
    """Is there any measured volume behind this report at all?

    Defensive: a report with no ``basis`` block is treated as having data, so a
    shape change upstream costs a wasted model call rather than a silently
    blank summary."""
    if is_item_report(report):
        # An item report's counts are the POPULATION it is ranked against; the
        # item itself is measured whether or not that population is empty, so
        # there is always something to write about.
        return True
    basis = report.get("basis")
    if not isinstance(basis, dict):
        return True
    keys = ("competitor_posts", "news_items", "viral_trends", "competitor_handles")
    total = 0
    for key in keys:
        try:
            total += int(basis.get(key) or 0)
        except (TypeError, ValueError):
            return True
    return total > 0


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

async def write_narrative(report: dict) -> dict:
    """Fill the Executive Summary and Recommendations slots. ONE model call.

    Returns a mutated COPY — the caller's dict is never touched, so a retry
    starts from the same clean report. NEVER raises: every failure path ends
    with the two sections marked unavailable and a reason a human can read,
    because a missing summary must not cost the owner the measured report they
    actually asked to download."""
    out = copy.deepcopy(report) if isinstance(report, dict) else {}
    slots = _slots(out)
    if not slots:
        # "simple" mode has no narrative slots at all — nothing to do, and that
        # is not an error.
        return out

    if all(_already_written(s) for s in slots.values()):
        # Someone already ran this pass over this report. Re-running it would
        # spend a second model call to overwrite words that are already there.
        return out

    if not _has_measured_anything(out):
        # Nothing was counted, so there is nothing to summarise — and a model
        # asked to summarise nothing is a model inventing something.
        for section in slots.values():
            _blank(section, NOTHING_MEASURED)
        return out

    item = is_item_report(out)

    prompt = build_narrative_prompt(out)
    try:
        # 6000, not the 2000 default: 2.5-pro spends hidden thinking tokens out
        # of the SAME output budget, and a starved structured call comes back as
        # an empty string rather than as an error. Temperature is low — this is
        # the reporting step, not the ideas step.
        #
        # AN ITEM REPORT USES THE FAST MODEL WITH THINKING OFF. It is opened
        # from a dialog with the owner waiting on it, and its digest is a
        # handful of numbers rather than fourteen sections. Measured end-to-end
        # on the live stack, one full item report: 31-57s on the pro model,
        # 13-18s on flash with thinking on, 5-11s on flash with thinking off.
        # The answers at the last setting were checked line by line against the
        # report they came from — still specific ("create a carousel for the
        # next application deadline"), still citing only figures printed above
        # them ("1st of 19 by engagement", "Fit for 42 100/100").
        #
        # The all-streams report keeps the pro model and its thinking budget:
        # there the reasoning spans a whole document, and nobody is watching a
        # dialog while it runs.
        raw = await gemini_text(
            prompt, system=_SYSTEM, json_schema=_SCHEMA,
            temperature=0.3,
            max_tokens=ITEM_MAX_TOKENS if item else 6000,
            model=((config.vertex_gemini_fast_model or None) if item else None),
            thinking_budget=(0 if item else None),
        )
    except Exception as exc:      # noqa: BLE001 — a narrative NEVER fails a download
        log.warning("[social-report] narrative call failed: %s", exc, exc_info=True)
        for section in slots.values():
            _blank(section, _explain(exc))
        return out

    findings, actions = parse_narrative(raw, _citable(out))
    for slot, items in ((EXEC_SUMMARY_ID, findings), (RECOMMENDATIONS_ID, actions)):
        section = slots.get(slot)
        if section is None:
            continue
        # Either half may come back empty; only the empty half is marked
        # unavailable, because half a narrative still beats none.
        if items:
            _fill(section, items)
        else:
            _blank(section, UNREADABLE)
    if not findings and not actions:
        log.warning("[social-report] narrative returned nothing usable")
    return out
