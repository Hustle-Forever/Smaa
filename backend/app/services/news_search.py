"""Industry Signal - one grounded search a morning, judged and rewritten.

WHAT THIS REPLACED, and why. The old collector polled four RSS feeds, and the
scoring prompt it fed was told to reject model releases, benchmarks, funding and
pricing BY CATEGORY. So the intake's only job was to hand the scorer material the
scorer was told to bury, which `test_the_intake_is_narrower_than_the_prompt_it_is
_scored_by` asserted out loud. Category is the wrong axis: "OpenAI released a
model that helps people code faster" and "OpenAI's model scores 4% higher on a
benchmark" are the same category and opposite answers. What matters is whether
there is a consequence somebody learning to code would care about.

THE SHAPE. Two model calls a day and no HTTP requests of our own:

    gemini_search(...)  -> prose: one block per candidate story  (grounded)
    gemini_text(...)    -> JSON: judged, scored and rewritten     (schema)

Two calls rather than one because Vertex refuses grounding and a response schema
together (`vertex_client._SEARCH_WITH_SCHEMA`). The grounded call returns prose
and we parse it, exactly as `general_trend_ideation.parse_research` does.

WHAT THE PROMPTS BELOW ARE, AND WHY THEY ARE SHAPED LIKE THIS. Every rule in
SEARCH_SYSTEM was measured against the live model on 2026-09-05, not guessed, and
three of them cost real result quality when they were phrased any other way:

  * "Today is <date>" plus "the last 48 hours" returned NOTHING AT ALL, with an
    empty grounding list: the model declined rather than searched. The window has
    to be soft ("published in the last 7 days") and the current date must not be
    asserted at it.
  * With no recency framing whatsoever it returns evergreen SEO listicles - "The
    9 best AI coding tools in 2026", a university explainer, a vendor roundup -
    dated anywhere in the past nine months. Hence the paragraph that spends four
    sentences saying what NEWS is and what it is not. That paragraph is the
    difference between a feed of things that happened and a feed of content
    marketing.
  * Telling it "never give a vertexaisearch redirect link, give the publisher's
    own" made it INVENT ONE: it produced
    `https://www.youtube.com/watch?v=VIDEO_ID_PLACEHOLDER`. So this module never
    forbids the redirect form. It asks for the address "exactly as you have it"
    and lets `verify_domains` below decide what is trustworthy. Forbidding a
    shape the model cannot always supply is an instruction to fabricate.

THE ONE VERIFICATION LEFT, and it costs nothing. We do not fetch pages any more.
What survives is a pure cross-check: `gemini_search` hands back the pages Google
actually retrieved, and their titles are bare domains. A story whose link points
somewhere that never appeared in that list did not come from the search. On the
measured run every one of six stories passed, so this is a floor rather than a
filter that eats good rows. It cannot catch a real domain carrying an invented
path, and that residual risk is accepted deliberately: a human reads every story
before anything is posted.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlsplit

from .vertex_client import gemini_search, gemini_text

log = logging.getLogger("archivist.news_search")

# The grounded call is the slow one: it does real web retrieval before it writes
# a word. 90s is generous against a p99 of about 40s and still bounded, because
# an unbounded await inside a poller is a hung morning nobody is watching.
SEARCH_TIMEOUT_SEC = 90.0
JUDGE_TIMEOUT_SEC = 120.0

# 2.5-pro spends hidden thinking tokens out of the SAME pool as the answer, and a
# starved call returns an EMPTY STRING rather than raising - which looks exactly
# like "found nothing". Both budgets are set well clear of the measured need.
SEARCH_MAX_TOKENS = 4000
JUDGE_MAX_TOKENS = 8000

# How many candidates the search is asked for. The judge culls from here, so this
# is deliberately above `stories_per_day`: asking for exactly ten and keeping the
# ones that pass would mean a strict judge starves the feed.
CANDIDATE_MULTIPLIER = 2
MAX_CANDIDATES = 20

# A story older than this never reaches the feed, whatever the model says. Kept
# equal to the retention window in `news_ingest`, so the intake and the shelf life
# are the same width and nothing is ingested only to be pruned on the next tick.
MAX_STORY_AGE_DAYS = 14

# The plain-language caps. Twelve words is not arbitrary: it is about the length
# at which a headline stops being a sentence somebody says out loud and starts
# being a summary they scan.
# The score below which a story is not worth a row.
#
# The judge drops what is WRONG for this audience (usable=false) but still
# scores plenty of merely dull stories 20 or 30, and those used to be written
# and then hidden behind a fit bar in the feed. A quarter of the table was
# stories nobody would ever be shown. Dropping them here means the table holds
# only what somebody might actually post, and the feed needs no bar at all.
MIN_RELEVANCE_TO_KEEP = 40

MAX_HEADLINE_WORDS = 12
MAX_HEADLINE_CHARS = 140
MAX_WHY_CHARS = 400
# Mirrors news_store.MAX_BRIEF_CHARS. Duplicated rather than imported because
# news_store imports nothing from here and a cycle is not worth one integer.
MAX_BRIEF_CHARS = 4000


# ---------------------------------------------------------------------------
# The jargon backstop
# ---------------------------------------------------------------------------
# THIS IS A NET, NOT THE MECHANISM. The model is asked to write plainly and then
# asked to mark its own work (`plain_enough`), and those two do the real job. This
# catches what they wave through, and it exists because "no jargon" is the whole
# point of the feature and a prompt is a hope while a regex is a guarantee.
#
# Scoped to what is genuinely a FORMAT rule - a shape you can see without
# understanding the sentence - rather than to judging meaning, which is the
# model's half of the split.

# "GPT-5.2", "v2.1", "Claude 4", "Gemini 3" - a product name welded to a number.
_VERSION_RE = re.compile(
    r"\bv\d+(\.\d+)*\b"
    r"|\b\d+\.\d+\b"
    r"|\b(gpt|claude|gemini|llama|grok|qwen|mistral|o)[\s-]?\d+",
    re.I,
)

# Four or more capitals in a row is an acronym nobody outside the field expands.
# "AI" is two, so it is safe by construction and needs no exception. Three is
# spared on purpose: "UAE", "MIT", "BBC", "NYC" are all readable.
_ACRONYM_RE = re.compile(r"\b[A-Z]{4,}\b")

# Words that are jargon whatever sentence they appear in. Deliberately short: a
# long blocklist starts rejecting ordinary English ("model", "training", "data")
# and the model's own judgement is better than a list at that boundary.
_JARGON_WORDS = (
    "benchmark", "parameters", "multimodal", "fine-tune", "fine-tuning",
    "inference", "context window", "quantization", "quantisation",
    "transformer", "open-weights", "open weights", "tokens per second",
    "latency", "throughput", "api", "sota", "arr", "valuation",
    "funding round", "series a", "series b", "seed round",
    # Three letters or fewer, so `_ACRONYM_RE` cannot see them. They are here
    # for the same reason it exists: nobody outside the field expands them.
    "llm", "llms", "rag", "gpu", "gpus", "sdk", "ide",
)
_JARGON_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _JARGON_WORDS) + r")\b", re.I
)


def jargon_reason(headline: str) -> Optional[str]:
    """Why this headline is not plain English, or None if it is fine.

    Pure, and it returns a READABLE reason rather than a bool, because the reason
    goes in the debug log next to the headline that tripped it. "too technical"
    would not tell you anything there; "has a version number" tells you the brief
    is pulling in release notes.
    """
    text = (headline or "").strip()
    if not text:
        return "empty headline"
    if len(text) > MAX_HEADLINE_CHARS:
        return f"too long ({len(text)} characters)"
    words = text.split()
    if len(words) > MAX_HEADLINE_WORDS:
        return f"too long ({len(words)} words)"
    if _VERSION_RE.search(text):
        return "has a version number in it"
    hit = _ACRONYM_RE.search(text)
    if hit:
        return f"uses the abbreviation {hit.group(0)}"
    hit = _JARGON_RE.search(text)
    if hit:
        return f"uses the technical word {hit.group(0).lower()!r}"
    return None


# ---------------------------------------------------------------------------
# Call A - the grounded search
# ---------------------------------------------------------------------------

# The two searches share every rule about what news is and how to report it.
# Only the opening differs: one is told to look across the whole web, the other
# to read a fixed list of websites and nothing else. Shared as one literal so
# the two lanes cannot drift on the definition of a story.
_RESEARCHER = (
    "You are a news researcher with a live web search, working for the social "
    "media team at 42 Abu Dhabi, a tuition-free coding school. You report what "
    "you FIND, not what you remember.\n\n"
)

_NEWS_RULES = (
    "WHAT COUNTS AS NEWS. Something that HAPPENED and was REPORTED BY A NEWS "
    "OUTLET in the last week. A 'best AI tools of the year' guide, a listicle, a "
    "tutorial, an explainer, a vendor landing page or a comparison roundup is NOT "
    "news, however relevant it looks. Leave those out. If the same page could "
    "have been published three months ago and still be true, it is not news.\n\n"

    "WHO IT IS FOR. Students, people thinking about learning to code, developers, "
    "parents, and anyone curious about technology and education. Nobody in that "
    "audience is a researcher.\n\n"

    "THE TEST, for every story: would someone learning to code, studying "
    "technology, or building software care about this or find it useful? Being "
    "about AI is not enough on its own.\n\n"

    "A new model, tool or product launch is a GOOD story when you can say what it "
    "lets people DO. It is a BAD story when the story is a number. Do not go "
    "looking for benchmark scores, funding rounds, research papers or pricing "
    "changes; include one only if it has an obvious, large, real-world "
    "consequence for students, developers, education, or how people use AI.\n\n"

    "ONE BLOCK PER STORY, in exactly this shape. Plain text, no markdown, no "
    "bullet points, no numbering, nothing before the first block or after the "
    "last:\n"
    "HEADLINE: <the outlet's own headline, copied exactly as they wrote it>\n"
    "OUTLET: <the name of the news outlet>\n"
    "WHEN: <YYYY-MM-DD, the day it was published>\n"
    "LINK: <the web address you found it at, exactly as you have it>\n"
    "WHAT HAPPENED: <2-3 plain sentences>\n\n"

    "NEVER INVENT a headline, a link, a date or an outlet. If you do not have "
    "one, leave that whole story out. Returning three real stories is better "
    "than eight with one guess in them. If you find nothing that passes the "
    "test, say so and return no blocks at all: a quiet week is a real answer and "
    "padding it is not."
)

# Lane one: the whole web.
SEARCH_SYSTEM = _RESEARCHER + _NEWS_RULES

# Lane two: the websites we always check, and nothing else.
#
# A SEPARATE SEARCH, not a sentence in the first one. The follow list used to
# put its websites into the open search's brief as "prefer these when they are
# as good", which is a hope: on a busy week the model never reached them, and
# the person who had added theverge.com saw nothing from The Verge and read the
# feature as broken. Asking for those sites on their own, one `site:` search
# each, is what turns a preference into a fixed list.
SITES_SYSTEM = _RESEARCHER + (
    "YOU ARE READING A FIXED LIST OF WEBSITES, and only those. The team checks "
    "them every time it looks for stories. Search each website on the list on "
    "its own, with a site: search, and report its newest stories from the last "
    "7 days that pass the test below. Never return a page from a website that "
    "is not on the list, and never pad a quiet website with a weak story.\n\n"

    "SOME OF THESE WEBSITES BELONG TO COMPANIES, such as openai.com or "
    "anthropic.com. On those, the company's own announcements from the last 7 "
    "days count as stories: report what they launched or changed and what it "
    "lets people do, and put the company's name as the outlet. The test below "
    "still applies in full. A benchmark table, a research paper, a pricing "
    "change or a hiring post is not a story, whoever published it.\n\n"
) + _NEWS_RULES

# How many stories the fixed-list search is asked for, in total and per site.
# Three per site is enough that a busy site still gives a choice, and small
# enough that one site cannot fill the whole feed on its own.
SITE_STORIES_PER_SITE = 3
MAX_SITE_CANDIDATES = 12


def build_search_prompt(*, brief: str, recent_headlines: Iterable[str],
                        limit: int) -> str:
    """Pure - the grounded call's user turn. Driven directly by the tests.

    `recent_headlines` is what we already have, so the model can skip it. It is
    advisory: the judge re-checks for duplicates with more context, and code
    catches an exact repeat by URL. Three overlapping guards, because the same
    story arriving every morning for a week is the most visible way this feature
    can look broken.
    """
    want = max(1, min(int(limit) * CANDIDATE_MULTIPLIER, MAX_CANDIDATES))
    parts = [f"WHAT TO LOOK FOR (from the team):\n{(brief or '').strip()}"]

    seen = [h.strip() for h in recent_headlines if str(h or "").strip()]
    if seen:
        parts.append(
            "ALREADY IN OUR FEED, do not send these again or another write-up of "
            "the same event:\n" + "\n".join(f"- {h}" for h in seen[:40])
        )

    parts.append(
        f"Search for AI news published in the last 7 days and give me up to "
        f"{want} stories that pass the test. Recent news only, not guides or "
        f"roundups."
    )
    return "\n\n".join(parts)


def build_sites_prompt(*, sites: Iterable[str], recent_headlines: Iterable[str],
                       limit: int) -> str:
    """Pure - the fixed-list call's user turn.

    Each site is written out with the exact `site:` search to run, because
    "look at theverge.com" is read as a suggestion and "search site:theverge.com"
    is read as an instruction. The skip-list is the same one the open search
    gets, for the same reason.
    """
    hosts = [str(s or "").strip().lower() for s in sites if str(s or "").strip()]
    want = max(1, min(int(limit), MAX_SITE_CANDIDATES))
    parts = [
        "THE WEBSITES WE ALWAYS CHECK, one search each:\n"
        + "\n".join(f"- {h}  (search: site:{h})" for h in hosts)
    ]

    seen = [h.strip() for h in recent_headlines if str(h or "").strip()]
    if seen:
        parts.append(
            "ALREADY IN OUR FEED, do not send these again or another write-up of "
            "the same event:\n" + "\n".join(f"- {h}" for h in seen[:40])
        )

    parts.append(
        f"Search every website above for AI news published in the last 7 days "
        f"and give me up to {want} stories in total that pass the test, newest "
        f"first, and no more than {SITE_STORIES_PER_SITE} from any one website. "
        f"Recent news only, not guides or roundups."
    )
    return "\n\n".join(parts)


def fixed_site_of(url: str, sites: Iterable[str]) -> Optional[str]:
    """Which of the websites we always check this link is on, or None.

    Pure. Subdomains count (`news.mit.edu` is on `mit.edu`) because that is
    how publishers actually file their articles. Used two ways: to throw away
    a fixed-list result that wandered off the list, and to mark a story the
    open search found on one of those sites as one of theirs anyway.
    """
    host = _host(url)
    if not host:
        return None
    for site in sites or []:
        s = str(site or "").strip().lower()
        if s and (host == s or host.endswith("." + s)):
            return s
    return None


def clean_host(raw: Any) -> str:
    """A bare host from whatever the judge wrote in `company_site`, or "".

    The model is asked for "openai.com" and mostly gives it, but "https://www.
    openai.com/" and "OpenAI.com" both appear, and a sentence appears when it
    is unsure and ignores the instruction to leave it empty. Only something
    shaped like a host survives, so nothing downstream ever fetches a sentence.
    """
    text = " ".join(str(raw or "").split()).lower()
    if not text or " " in text:
        return ""
    if "://" not in text:
        text = "https://" + text
    host = _host(text)
    return host if _HOSTISH_RE.match(host) else ""


_HOSTISH_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def choose_stories(stories: list[dict], *, limit: int,
                   sites: Iterable[str] = ()) -> list[dict]:
    """The stories that make the cut, best first. Pure.

    Relevance decides the order, as it always did. The one thing added is a
    reserved place for the websites we always check: each one's best story is
    kept inside the cap even when the open search found more interesting
    things, because a fixed list that never shows on a busy day is not a fixed
    list. A site with nothing good this week reserves nothing.

    AT MOST HALF THE CAP IS RESERVED. Five fixed websites against a cap of
    three would otherwise fill the feed between them and the open search,
    which is the other half of what was asked for, would never show. The
    reserved places go to the best-scoring of the sites' best stories.
    """
    cap = max(1, int(limit))
    ranked = sorted(
        stories,
        key=lambda s: (-int(s.get("relevance") or 0), str(s.get("headline") or "")),
    )
    if len(ranked) <= cap:
        return ranked

    wanted = [str(s or "").strip().lower() for s in (sites or []) if str(s or "").strip()]
    best_per_site: list[dict] = []
    for s in wanted:
        best = next((x for x in ranked if x.get("fixed_site") == s), None)
        if best is not None and not any(best is b for b in best_per_site):
            best_per_site.append(best)
    best_per_site.sort(key=lambda x: -int(x.get("relevance") or 0))
    budget = min(len(best_per_site), (cap + 1) // 2)
    kept_ids: set[int] = {id(x) for x in best_per_site[:budget]}

    for x in ranked:
        if len(kept_ids) >= cap:
            break
        kept_ids.add(id(x))
    return [x for x in ranked if id(x) in kept_ids]


# One block's five labels. Tolerant on purpose: the model drifts to "**HEADLINE:**"
# or "Headline:" under load, and throwing away a real story over a bold marker
# would be the parser deciding what counts as news.
_FIELD_RE = re.compile(
    r"^\s*[*_#\-\s]*"
    r"(HEADLINE|OUTLET|WHEN|LINK|WHAT\s+HAPPENED)"
    # The trailing `[*_\s]*` is not decoration: under load the model writes
    # `**HEADLINE:** Bill Gates ...` and without it the closing asterisks land
    # inside the headline and travel all the way to the card.
    r"[*_\s]*:[*_\s]*(.*)$",
    re.I,
)


def parse_candidates(text: Any) -> list[dict]:
    """Pure - the grounded prose split into candidate stories.

    Grounding forbids a response schema, so this is the parser that replaces one.
    A block is kept when it has BOTH a headline and a link, because those two are
    the only fields with no sane default: an outlet can fall back to the link's
    domain and a missing date is handled by the caller, but a story with no
    headline is not a story and one with no link cannot be checked or read.
    """
    raw = str(text or "").strip()
    if not raw:
        return []

    out: list[dict] = []
    cur: dict = {}
    field: Optional[str] = None

    def flush() -> None:
        if cur.get("headline") and cur.get("url"):
            out.append(dict(cur))
        cur.clear()

    for line in raw.splitlines():
        m = _FIELD_RE.match(line)
        if not m:
            # A continuation of the previous field. Only WHAT HAPPENED is
            # expected to wrap, but allowing any field to keeps a headline that
            # ran long from being silently truncated.
            if field and line.strip():
                cur[field] = f"{cur.get(field, '')} {line.strip()}".strip()
            continue
        label = " ".join(m.group(1).upper().split())
        value = m.group(2).strip()
        if label == "HEADLINE":
            flush()                     # a new headline starts a new block
            field = "headline"
        elif label == "OUTLET":
            field = "outlet"
        elif label == "WHEN":
            field = "published"
        elif label == "LINK":
            field = "url"
        else:
            field = "summary"
        cur[field] = value
    flush()

    for c in out:
        # One combined set, stripped repeatedly: the model wraps a link as
        # `<https://a.com/x>.` and two sequential strips stop at the first
        # character the current set does not cover.
        c["url"] = c.get("url", "").strip().strip("<>()[]\"'.,;")
        c["headline"] = " ".join(c.get("headline", "").split())
        c["outlet"] = " ".join(c.get("outlet", "").split())
        c["summary"] = " ".join(c.get("summary", "").split())
    return out


# ---------------------------------------------------------------------------
# The grounding cross-check
# ---------------------------------------------------------------------------

def _host(url: str) -> str:
    """The bare host of a url, lowercased, without `www.`. "" when unparseable."""
    try:
        host = urlsplit(str(url or "").strip()).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def grounded_hosts(sources: Iterable[dict]) -> set[str]:
    """The domains Google actually retrieved, out of `gemini_search`'s sources.

    Their `title` IS the bare domain ("edweek.org", "mit.edu") and their `uri` is
    always a `vertexaisearch.cloud.google.com/grounding-api-redirect/...` address,
    measured 2026-09-05 across four live calls. Both are read, because the title
    is the useful half and relying on a field that carries a domain today and a
    headline tomorrow is how this quietly stops working.
    """
    out: set[str] = set()
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip().lower()
        if title and " " not in title and "." in title:
            out.add(title[4:] if title.startswith("www.") else title)
        host = _host(s.get("uri") or "")
        if host:
            out.add(host)
    return out


# Path and query values that are a placeholder rather than an address.
#
# MEASURED, not imagined. A live run on 2026-09-05 put
# `https://www.youtube.com/watch?v=null` at the TOP of the feed: the domain was
# genuinely in the grounding list, so `is_grounded` passed it, and the model had
# simply not carried the video id through. The earlier probe produced
# `?v=VIDEO_ID_PLACEHOLDER` from the same gap.
#
# This is the other half of the residual risk `is_grounded` documents - a real
# domain wearing an invented path - and while it cannot close it in general, the
# obvious cases are free to catch and were the ones actually happening. Matched
# on WHOLE segments so a real slug like `/news/ai-test-results` is untouched.
_PLACEHOLDER_VALUES = {
    "null", "undefined", "none", "nan", "placeholder", "example", "sample",
    "xxx", "xxxx", "todo", "tbd", "id", "video_id", "article_id", "your_id",
    "insert_url_here", "url", "link",
}


def looks_like_a_placeholder(url: str) -> bool:
    """Is this an address shape rather than an address?

    Pure and free: no request, no timeout. Checks the path segments and the
    query VALUES, never the host, because a placeholder host would already have
    failed the grounding check above.
    """
    text = str(url or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if "placeholder" in lowered or "<" in text or ">" in text:
        return True
    try:
        parts = urlsplit(text)
    except ValueError:
        return True
    segments = [seg for seg in parts.path.split("/") if seg]
    values = [v for _, v in parse_qsl(parts.query, keep_blank_values=True)]
    for candidate in segments + values:
        if candidate.strip().lower() in _PLACEHOLDER_VALUES:
            return True
    # A query key with nothing behind it: `?v=` is not a video.
    return any(v.strip() == "" for v in values)


def is_grounded(url: str, hosts: set[str]) -> bool:
    """Did this link come from a page the search actually retrieved?

    The whole verification layer, and it costs nothing: no request, no timeout,
    no politeness budget. A story pointing somewhere that never appeared in the
    grounding list was not found by the search that claims to have found it.

    Subdomains count both ways, because the list holds `mit.edu` while the story
    links `news.mit.edu`, and a redirect link is trusted outright since it IS a
    grounding address.

    It cannot catch a real domain carrying an invented path, and that is stated
    rather than hidden: a person reads every story before anything is posted.
    """
    host = _host(url)
    if not host:
        return False
    if host.endswith("vertexaisearch.cloud.google.com"):
        return True
    return any(host == h or host.endswith("." + h) or h.endswith("." + host)
               for h in hosts)


# ---------------------------------------------------------------------------
# Call B - judge, score and rewrite
# ---------------------------------------------------------------------------

# One literal, read by the judge at ingest and by the catch-up scorer for
# stories found before the score existed, so the two can never disagree on
# what "viral" means.
_VIRAL_RULES = (
    "VIRAL POTENTIAL. `viral_potential` is 0-100 for how likely a post about "
    "this would get a lot of views and shares on Instagram with 42's audience: "
    "students, people thinking about learning to code, and parents. Score HIGH "
    "for a surprise, a strong feeling (fear, hope, outrage, pride, a laugh), "
    "something every student can relate to, a name everyone knows, something "
    "you can say in one line and show in one picture, and anything about jobs, "
    "money, school, exams or cheating. Score LOW for the corporate, the niche, "
    "the abstract, and anything that needs a paragraph to explain. Use the "
    "whole range, and never let it copy relevance_score: a useful story can be "
    "dull and a dull story can be useful. `viral_reason` is one short plain "
    "sentence on why it would or would not travel.\n\n"
)

JUDGE_SYSTEM = (
    "You advise the social media team at 42 Abu Dhabi, a tuition-free, "
    "peer-to-peer coding school. Students learn without teachers or lectures, "
    "admission runs through an intensive month called the Piscine, and no prior "
    "coding experience is required.\n\n"

    "You are given news stories somebody else found. For each one you decide "
    "whether 42 should post about it, and if so you write it in plain English.\n\n"

    "THE TEST: would someone learning to code, studying technology, or building "
    "software care about this or find it useful? Being about AI is not enough.\n"
    "- A new model, tool or product launch is a GOOD story when you can say what "
    "it lets people DO. It is a BAD story when the story is a number.\n"
    "  GOOD: 'OpenAI released a new AI model that helps people code and learn "
    "faster'\n"
    "  BAD:  'OpenAI's new model scores 4% higher on benchmark X'\n"
    "- Benchmark scores, funding rounds, research papers and pricing changes are "
    "NOT interesting on their own. Mark them unusable unless there is an "
    "obvious, large, real-world consequence for students, developers, education "
    "or how people use AI.\n"
    "  Unusable: 'AI model achieves 93% on a benchmark'. 'AI startup raises "
    "$500M'. 'Researchers publish a new transformer architecture'. 'OpenAI "
    "changes API pricing'.\n"
    "- A guide, listicle, tutorial or vendor roundup is not news. Mark it "
    "unusable.\n\n"

    "NOW WRITE IT FOR SOMEONE WITH NO TECHNICAL BACKGROUND. High level only.\n"
    "- plain_headline: at most 12 words, what happened, in ordinary words.\n"
    "- why_it_matters: one or two ordinary sentences on why anyone should care.\n"
    "- angle_for_42: one or two ordinary sentences on how 42 could post about "
    "this, meaning what the post would say or show. Write one for EVERY usable "
    "story and never leave it empty.\n"
    "Nothing else. No detail, no numbers, no mechanism. If a fact cannot be said "
    "in ordinary words, leave it out. Add no fact the story did not contain, and "
    "never present 42 as the source of the news.\n\n"
    "Never use in either field: version numbers, benchmark names, architecture "
    "words (transformer, parameters, multimodal, fine-tuning, inference, context "
    "window), business words (valuation, funding round, API pricing), or any "
    "abbreviation longer than three letters except 'AI'.\n"
    "  BAD:  'OpenAI ships GPT-5.2 with improved MMLU scores'\n"
    "  GOOD: 'The company behind ChatGPT made it much better at maths'\n"
    "  BAD:  'Anthropic releases an agentic SWE-bench SOTA coding model'\n"
    "  GOOD: 'A new AI can now write and fix code on its own'\n"
    "  BAD:  'Study finds LLM tutors improve learning outcomes by 14%'\n"
    "  GOOD: 'Students who learned with an AI tutor did better in tests'\n\n"
    "Then mark your own work honestly in `plain_enough`: would a 10-year-old "
    "understand your headline? If not, say false and it will be dropped.\n\n"

    "WHO THE STORY IS ABOUT. `company` is the one company or organisation the "
    "story is mainly about, by its everyday name: OpenAI, Microsoft, Anthropic, "
    "Google, Meta, Apple, Nvidia. A story about a product names the company "
    "that makes it: ChatGPT is OpenAI, Claude is Anthropic, Gemini is Google, "
    "Copilot is Microsoft. Leave it empty when the story is not mainly about "
    "one company, such as a school district, a study or a government. "
    "`company_site` is that company's main website address, like openai.com or "
    "anthropic.com, and empty if you are not sure.\n\n"

    + _VIRAL_RULES +

    "SCORING. `relevance_score` is 0-100 for how much this audience would care. "
    "Use the WHOLE range. Anchors:\n"
    "- 90-100: 42 could post this tomorrow and its students would care. "
    "Concrete, human, and about their own learning, work or future.\n"
    "- 0-20: nothing here for this audience - a version number, a benchmark "
    "table, a funding round, or a story with no real consequence.\n"
    "Do not cluster on 80 and 90. If two stories differ in usefulness, give them "
    "different numbers.\n\n"

    "Return one entry per story you were given, in the same order, echoing its "
    "`index`. Never drop one silently: an unusable story comes back with "
    "`usable: false` and a short `reject_reason` a non-technical person can read."
)

_JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer",
                              "description": "the STORY index given in the prompt"},
                    "usable": {"type": "boolean",
                               "description": "should 42 post about this at all"},
                    "reject_reason": {"type": "string",
                                      "description": "if not usable, why, in plain words"},
                    "plain_headline": {"type": "string",
                                       "description": "at most 12 ordinary words"},
                    "why_it_matters": {"type": "string",
                                       "description": "one or two ordinary sentences"},
                    "angle_for_42": {"type": "string",
                                     "description": "how 42 would post about it, "
                                                    "one or two plain sentences"},
                    "company": {"type": "string",
                                "description": "the one company the story is mainly "
                                               "about, by its everyday name, or empty"},
                    "company_site": {"type": "string",
                                     "description": "that company's main website, "
                                                    "like openai.com, or empty"},
                    "viral_potential": {"type": "integer",
                                        "description": "0-100, how likely a post "
                                                       "about this gets views and shares"},
                    "viral_reason": {"type": "string",
                                     "description": "one short plain sentence on why"},
                    "relevance_score": {"type": "integer", "description": "0-100"},
                    "plain_enough": {"type": "boolean",
                                     "description": "would a 10-year-old understand the headline"},
                    "duplicate_of": {"type": "string",
                                     "description": "an earlier headline this repeats, or empty"},
                    "topic": {"type": "string",
                              "description": "the transferable angle, max 8 words"},
                    "theme": {"type": "string",
                              "description": "a short reusable bucket, e.g. 'AI and jobs', "
                                             "'AI in school', 'new tools'"},
                    "suggested_format": {"type": "string",
                                         "enum": ["reel", "carousel", "static"]},
                    "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "usable", "plain_headline", "why_it_matters",
                             "angle_for_42", "relevance_score", "viral_potential",
                             "plain_enough"],
            },
        },
    },
    "required": ["stories"],
}


def build_judge_prompt(candidates: list[dict], *, brief: str,
                       recent_headlines: Iterable[str]) -> str:
    """Pure - the judging call's user turn.

    The candidate's own summary is all the model gets about the story: we no
    longer fetch the page. So the summary is quoted in full rather than trimmed,
    and the headline is given as the OUTLET'S OWN so the rewrite has something
    honest to rewrite FROM.
    """
    lines = [f"WHAT THE TEAM IS LOOKING FOR:\n{(brief or '').strip()}"]

    seen = [h.strip() for h in recent_headlines if str(h or "").strip()]
    if seen:
        lines.append(
            "ALREADY IN THE FEED. If a story below is the same event as one of "
            "these, set `duplicate_of` to the line it repeats:\n"
            + "\n".join(f"- {h}" for h in seen[:40])
        )

    for i, c in enumerate(candidates):
        block = [f"STORY {i}"]
        if c.get("outlet"):
            block.append(f"outlet: {c['outlet']}")
        if c.get("published"):
            block.append(f"published: {c['published']}")
        block.append(f"headline as published: {c.get('headline', '')}")
        if c.get("summary"):
            block.append(f"what happened: {c['summary']}")
        lines.append("\n".join(block))

    lines.append(
        f"Judge all {len(candidates)} stories. Return one entry each, echoing "
        f"the index."
    )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%d %b %Y",
                 "%B %d, %Y", "%b %d, %Y")


def parse_when(raw: Any) -> Optional[datetime]:
    """The model's `WHEN:` as an aware UTC datetime, or None.

    It is asked for `YYYY-MM-DD` and gives it, but a handful of spellings are
    accepted anyway: a date this module cannot read is a real story thrown away
    over punctuation.
    """
    text = " ".join(str(raw or "").split())[:40]
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


class SearchUnavailable(RuntimeError):
    """The search cannot run at all - credentials, billing or the budget cap.

    Separated from a transient failure the way `trend_scoring.ScoringUnavailable`
    is, so the caller records a reason a person can act on instead of retrying
    into the same wall every morning.
    """


def _unavailable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in (
        "billing", "permission_denied", "403", "api key", "credential",
        "not enabled", "unauthenticated", "401", "budget",
    ))


# ---------------------------------------------------------------------------
# The morning run
# ---------------------------------------------------------------------------

async def find_stories(*, brief: str, limit: int = 10,
                       recent_headlines: Optional[list[str]] = None,
                       sites: Optional[list[str]] = None) -> dict:
    """One morning's stories: search, judge, rewrite, filter.

    `sites` are the websites we always check. When there are any, a second
    grounded search reads them alone, at the same time as the open one, and
    both piles of candidates go through the same cross-check and the same
    judge. One lane failing does not lose the other: a fixed list that breaks
    the open search would be worse than no fixed list.

    Returns a report rather than raising, because the caller is an unattended
    poller and every outcome here is a row in a log somebody reads:

        {"status": ok|empty|ungrounded|search_failed|judge_failed,
         "stories": [...], "found": int, "kept": int,
         "drops": {readable reason: count}, "error": str | None}

    `drops` is keyed from a FIXED set of short sentences written here, never
    from anything the model wrote. Nothing about a rejected story reaches a
    screen: a story the judge threw out is one nobody should ever be shown, and
    printing the model's own sentence about it ("Do not post; this is about
    corporate investment strategy...") put a refusal in front of the owner that
    reads like a post. The counts are still written to `news_runs`, so a morning
    that drops everything is still diagnosable from the log or the database.
    """
    recent = list(recent_headlines or [])
    fixed = [str(s or "").strip().lower() for s in (sites or []) if str(s or "").strip()]
    drops: dict[str, int] = {}

    def drop(reason: str) -> None:
        drops[reason] = drops.get(reason, 0) + 1

    # --- 1. the grounded searches, side by side ---------------------------
    lanes: list[tuple[str, str, str]] = [
        ("web", build_search_prompt(brief=brief, recent_headlines=recent, limit=limit),
         SEARCH_SYSTEM),
    ]
    if fixed:
        lanes.append((
            "sites",
            build_sites_prompt(sites=fixed, recent_headlines=recent,
                               limit=len(fixed) * SITE_STORIES_PER_SITE),
            SITES_SYSTEM,
        ))

    results = await asyncio.gather(
        *(asyncio.wait_for(
            gemini_search(prompt, system=system, temperature=0.3,
                          max_tokens=SEARCH_MAX_TOKENS),
            timeout=SEARCH_TIMEOUT_SEC,
        ) for _, prompt, system in lanes),
        return_exceptions=True,
    )

    hosts: set[str] = set()
    answers: list[tuple[str, str]] = []   # (lane, prose)
    errors: list[str] = []
    for (lane, _, _), got in zip(lanes, results):
        if isinstance(got, BaseException):
            if _unavailable(got):
                raise SearchUnavailable(str(got)[:300]) from got
            log.warning("[news] %s search failed: %s", lane, str(got)[:300])
            errors.append(str(got)[:500])
            continue
        text, sources = got
        # An empty grounding list means the model answered from memory.
        # Measured: a prompt that pinned "today" and demanded the last 48 hours
        # produced exactly this, a confident refusal with no search behind it.
        # Trusting that reply would put remembered stories in a feed that
        # promises found ones.
        lane_hosts = grounded_hosts(sources)
        if not lane_hosts:
            log.warning("[news] %s search returned no grounding sources; "
                        "discarding", lane)
            continue
        hosts |= lane_hosts
        answers.append((lane, text))

    if not answers:
        if errors and len(errors) == len(lanes):
            return {"status": "search_failed", "stories": [], "found": 0, "kept": 0,
                    "drops": drops, "error": errors[0]}
        return {"status": "ungrounded", "stories": [], "found": 0, "kept": 0,
                "drops": drops, "error": errors[0] if errors else None}

    # The fixed-list lane is read FIRST so that a story both lanes found is
    # counted once, as one of theirs. Duplicates across lanes are the normal
    # case, not an edge one: the open search reads the same web.
    candidates: list[dict] = []
    seen_urls: set[str] = set()
    for lane, text in sorted(answers, key=lambda a: a[0] != "sites"):
        for c in parse_candidates(text):
            key = c["url"].lower().rstrip("/")
            if key in seen_urls:
                continue
            seen_urls.add(key)
            c["lane"] = lane
            c["fixed_site"] = fixed_site_of(c["url"], fixed) or ""
            candidates.append(c)
    found = len(candidates)
    if not candidates:
        return {"status": "empty", "stories": [], "found": 0, "kept": 0,
                "drops": drops, "error": None}

    # --- 2. the free cross-check ------------------------------------------
    checked = []
    for c in candidates:
        if looks_like_a_placeholder(c["url"]):
            drop("the link was not a real address")
        elif c["lane"] == "sites" and not c["fixed_site"]:
            # Told to read the list and nothing else, and wandered off it.
            drop("not from one of the websites we always check")
        elif is_grounded(c["url"], hosts):
            checked.append(c)
        else:
            drop("the link did not come from the search")
    if not checked:
        return {"status": "empty", "stories": [], "found": found, "kept": 0,
                "drops": drops, "error": None}

    # --- 3. judge and rewrite ---------------------------------------------
    try:
        verdicts = await asyncio.wait_for(
            gemini_text(build_judge_prompt(checked, brief=brief,
                                           recent_headlines=recent),
                        system=JUDGE_SYSTEM, json_schema=_JUDGE_SCHEMA,
                        temperature=0.3, max_tokens=JUDGE_MAX_TOKENS),
            timeout=JUDGE_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        if _unavailable(exc):
            raise SearchUnavailable(str(exc)[:300]) from exc
        log.warning("[news] judging failed: %s", str(exc)[:300])
        return {"status": "judge_failed", "stories": [], "found": found, "kept": 0,
                "drops": drops, "error": str(exc)[:500]}

    # `_parse_response` hands back this sentinel instead of raising when the
    # reply will not parse - most often a truncation, because 2.5-pro spends
    # thinking tokens from the same budget as the answer.
    if not isinstance(verdicts, dict) or verdicts.get("_raw") is not None:
        log.warning("[news] judging returned an unparseable reply")
        return {"status": "judge_failed", "stories": [], "found": found, "kept": 0,
                "drops": drops, "error": "the reply could not be read"}

    by_index: dict[int, dict] = {}
    for v in (verdicts.get("stories") or []):
        if isinstance(v, dict) and isinstance(v.get("index"), int):
            by_index.setdefault(v["index"], v)

    # --- 4. the code filters ----------------------------------------------
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(days=MAX_STORY_AGE_DAYS)
    stories: list[dict] = []

    for i, c in enumerate(checked):
        v = by_index.get(i)
        if v is None:
            drop("the AI did not answer for this one")
            continue
        if not v.get("usable"):
            # The judge is still ASKED for a reason: made to justify a refusal it
            # refuses more carefully. But its answer goes to the debug log only.
            log.debug("[news] dropped %r: %s", c.get("headline", "")[:80],
                      " ".join(str(v.get("reject_reason") or "").split())[:200])
            drop("not right for our audience")
            continue
        if str(v.get("duplicate_of") or "").strip():
            drop("repeats a story we already have")
            continue

        headline = " ".join(str(v.get("plain_headline") or "").split())
        if not v.get("plain_enough"):
            drop("still too technical")
            continue
        why = jargon_reason(headline)
        if why:
            log.debug("[news] dropped %r: %s", headline[:80], why)
            drop("still too technical")
            continue

        relevance = max(0, min(int(v.get("relevance_score") or 0), 100))
        if relevance < MIN_RELEVANCE_TO_KEEP:
            drop("not interesting enough to post")
            continue

        published = parse_when(c.get("published"))
        estimated = published is None
        if published is None:
            published = now
        elif published > now:
            # A future date is a publisher bug or a timezone one. Trusting it
            # pins the story to the top of a recency sort indefinitely.
            published = now
        elif published < oldest:
            drop("older than a fortnight")
            continue

        why = " ".join(str(v.get("why_it_matters") or "").split())[:MAX_WHY_CHARS]
        # Every card carries "how 42 could post about this". The judge is told
        # to write one for every usable story, and when it still does not, the
        # reason the story matters is the next best sentence to stand there.
        angle = " ".join(str(v.get("angle_for_42") or "").split())[:1000] or why
        stories.append({
            "headline": headline,
            "original_headline": c.get("headline", "")[:500],
            "outlet": (c.get("outlet") or _host(c["url"]) or "Industry Signal")[:120],
            "url": c["url"],
            "published": published,
            "date_is_estimated": estimated,
            "why_it_matters": why,
            "angle": angle,
            "company": " ".join(str(v.get("company") or "").split())[:120],
            "company_site": clean_host(v.get("company_site")),
            "fixed_site": c.get("fixed_site") or "",
            "relevance": relevance,
            "viral": clamp_score(v.get("viral_potential")),
            "viral_reason": " ".join(str(v.get("viral_reason") or "").split())[:300],
            "topic": " ".join(str(v.get("topic") or "").split())[:200] or headline,
            "theme": " ".join(str(v.get("theme") or "").split())[:120],
            "suggested_format": v.get("suggested_format") or None,
            "effort": v.get("effort") or None,
            "tags": [str(t)[:40] for t in (v.get("tags") or [])][:12],
        })

    stories = choose_stories(stories, limit=limit, sites=fixed)

    return {"status": "ok" if stories else "empty", "stories": stories,
            "found": found, "kept": len(stories), "drops": drops, "error": None}


# ---------------------------------------------------------------------------
# Viral potential for the stories found before the score existed
# ---------------------------------------------------------------------------
# Every story found since carries the score from ingest, written by the same
# judge that wrote everything else on the card. The ones from before are scored
# here, a batch at a time, from the housekeeping tick (`news_virality`), so the
# feed's order means the same thing for every card. The same rules literal, so
# the two paths cannot drift on what "viral" means.

VIRALITY_SYSTEM = (
    "You advise the social media team at 42 Abu Dhabi, a tuition-free coding "
    "school, on which stories to post first. You are given stories the team "
    "already kept, each as a plain headline and one line on why it matters.\n\n"
    + _VIRAL_RULES +
    "Return one entry per story, in the same order, echoing its `index`. Never "
    "drop one."
)

_VIRAL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "viral_potential": {"type": "integer", "description": "0-100"},
                    "viral_reason": {"type": "string",
                                     "description": "one short plain sentence"},
                },
                "required": ["index", "viral_potential"],
            },
        },
    },
    "required": ["scores"],
}

# The same budget as the judge, for the same reason: the model spends hidden
# thinking tokens out of the answer's pool, and a starved call returns a cut
# reply that will not parse. Measured: 4000 was not enough for twenty stories.
VIRALITY_MAX_TOKENS = JUDGE_MAX_TOKENS


def clamp_score(raw: Any) -> int:
    """0-100 from whatever the model wrote in a score field. Unreadable is 0."""
    try:
        return max(0, min(int(raw or 0), 100))
    except (TypeError, ValueError):
        return 0


def build_virality_prompt(rows: list[dict]) -> str:
    """Pure - the catch-up call's user turn. `rows` carry `headline` and `why`."""
    lines: list[str] = []
    for i, r in enumerate(rows):
        block = [f"STORY {i}", f"headline: {str(r.get('headline') or '').strip()}"]
        why = str(r.get("why") or "").strip()
        if why:
            block.append(f"why it matters: {why}")
        lines.append("\n".join(block))
    lines.append(f"Score all {len(rows)} stories. Return one entry each, echoing "
                 f"the index.")
    return "\n\n".join(lines)


def parse_virality(reply: Any, count: int) -> dict[int, tuple[int, str]]:
    """Pure - `{index: (score, reason)}` from the model's reply.

    An index outside the batch, a repeat, or an entry that is not a dict is
    skipped rather than guessed at: a story left unscored is scored on the
    next tick, and a story scored from the wrong entry is wrong for good.
    """
    out: dict[int, tuple[int, str]] = {}
    if not isinstance(reply, dict):
        return out
    for v in reply.get("scores") or []:
        if not isinstance(v, dict) or not isinstance(v.get("index"), int):
            continue
        i = v["index"]
        if not (0 <= i < count) or i in out:
            continue
        reason = " ".join(str(v.get("viral_reason") or "").split())[:300]
        out[i] = (clamp_score(v.get("viral_potential")), reason)
    return out


async def rate_virality(rows: list[dict]) -> dict[int, tuple[int, str]]:
    """Score a batch of kept stories. Empty on any failure, never raises for a
    transient one; `SearchUnavailable` for credentials or billing, as the
    morning run does, so the caller stops retrying into the same wall."""
    if not rows:
        return {}
    try:
        reply = await asyncio.wait_for(
            gemini_text(build_virality_prompt(rows), system=VIRALITY_SYSTEM,
                        json_schema=_VIRAL_SCHEMA, temperature=0.3,
                        max_tokens=VIRALITY_MAX_TOKENS),
            timeout=JUDGE_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001 - a housekeeping stage never raises
        if _unavailable(exc):
            raise SearchUnavailable(str(exc)[:300]) from exc
        log.warning("[news] viral scoring failed: %s", str(exc)[:300])
        return {}
    if not isinstance(reply, dict) or reply.get("_raw") is not None:
        log.warning("[news] viral scoring returned an unparseable reply")
        return {}
    return parse_virality(reply, len(rows))


# ---------------------------------------------------------------------------
# Writing the brief
# ---------------------------------------------------------------------------
# The brief is the only thing on the Configure screen, and it decides everything
# the feature finds. Somebody who has never written a search brief will type
# three words and get three words' worth of news, so this turns a rough note into
# something that actually works - and then hands it BACK rather than saving it,
# because a button that silently rewrites the one field controlling the whole
# feature would be the worst kind of helpful.

_BRIEF_SYSTEM = (
    "You help the social media team at 42 Abu Dhabi, a tuition-free coding "
    "school, describe the AI news they want to find each morning.\n\n"
    "Their audience is students, people thinking about learning to code, "
    "developers, parents, and anyone curious about technology and education. "
    "The test every story has to pass is: would someone learning to code, "
    "studying technology, or building software care about this or find it "
    "useful?\n\n"
    "A good brief names the KINDS OF CHANGE worth hearing about - what people "
    "can now do, learn, earn or worry about - rather than listing companies or "
    "technologies. A brief that names companies returns press releases.\n\n"
    "Write 3 alternatives, each 2-4 sentences of plain English, each taking a "
    "genuinely different angle on what the team said. No jargon, no bullet "
    "points, no headings: this text is pasted straight into a search. Give each "
    "one a short label of 2-4 words and one sentence saying how it differs."
)

_BRIEF_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "2-4 words"},
                    "difference": {"type": "string",
                                   "description": "one sentence on how it differs"},
                    "brief": {"type": "string",
                              "description": "2-4 plain sentences, pasted into the search"},
                },
                "required": ["label", "brief"],
            },
        },
    },
    "required": ["suggestions"],
}


async def suggest_briefs(notes: str) -> dict:
    """Rough note in, two or three complete briefs out. Never saves anything."""
    text = " ".join(str(notes or "").split())
    if not text:
        raise ValueError("Tell us roughly what you want and we will write it up.")

    prompt = (
        f"The team said:\n{text[:2000]}\n\n"
        "Write 3 briefs they could use."
    )
    try:
        raw = await asyncio.wait_for(
            gemini_text(prompt, system=_BRIEF_SYSTEM, json_schema=_BRIEF_SCHEMA,
                        temperature=0.6, max_tokens=2000),
            timeout=JUDGE_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        if _unavailable(exc):
            raise SearchUnavailable(str(exc)[:300]) from exc
        raise

    if not isinstance(raw, dict) or raw.get("_raw") is not None:
        raise RuntimeError("the reply could not be read")

    out = []
    for s in (raw.get("suggestions") or []):
        if not isinstance(s, dict):
            continue
        brief = " ".join(str(s.get("brief") or "").split())[:MAX_BRIEF_CHARS]
        if not brief:
            continue
        out.append({
            "label": " ".join(str(s.get("label") or "").split())[:60] or "Suggestion",
            "difference": " ".join(str(s.get("difference") or "").split())[:200],
            "brief": brief,
        })
    return {"suggestions": out[:3]}
