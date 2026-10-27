"""General Trends → Producer: three ideas first, then a ready-to-use brief.

The "Make this" button on a General trend card, in two steps:

  ``brainstorm_ideas`` — ONE model call that returns THREE genuinely different
  ways 42 could do this trend. The owner picks one, or types what they actually
  wanted and gets another three. Nobody wants a single take handed to them as
  though it were the only one; the previous flow jumped straight to a filled-in
  brief and there was no way to say "no, more like this".

  ``generate_prefill`` — the picked idea (or, with no idea, the trend itself)
  written in the destination tool's exact shape.

Everything downstream is shared on purpose: the per-tool briefs
(``trend_ideation._TOOL_BRIEFS``), the response schema (``schema_for``), the
envelope parser (``parse_prefill``) and the error taxonomy
(``IdeationUnavailable`` / ``ValueError`` / ``LookupError`` / ``RuntimeError``),
so the frontend receives the identical ``TrendPrefill`` with ``post_id`` = the
trend id and ``permalink`` = the trend's url.

So is the BRAINSTORM step itself. It was written here, for trends, but nothing
about "give me three different ideas, then I will pick one" is trend-specific —
a competitor's post and a news story want exactly the same step. It therefore
lives in ``trend_ideation`` now (the module both paths already share) and this
one keeps only what is genuinely about a TREND: the school-meets-the-subject
rules, the trend/evidence description, the research lookup, and the paragraphs
of the ideas brief that name the trend — chiefly the one telling the model the
name is a label. The clamps, the schema, the tool menu, the picked-idea block,
the parser, the brainstorm system prompt and the rest of the ideas brief are
imported. The module-level names below
are thin aliases so nothing that reads them by name has to care where they moved.

  ``research_trend`` — ONE Google-grounded lookup of what the trend actually
  refers to, run BEFORE any idea is written and cached so a second "Make this"
  is free. The owner pressed "Make this" on ``#spidermanbrandnewday`` and got
  three ideas about "a fresh start / a brand new day": the model had read the
  hashtag literally when it is the title of the 2026 film *Spider-Man: Brand
  New Day*. Every idea was built on a misreading, and the scorer's own stored
  summary ("a famous Spider-Man comic storyline") was itself a guess from
  memory. The fix is not a better prompt about hashtags — it is looking it up.

Both calls carry ``PLAIN_LANGUAGE_RULE``. The owner caught a generated post
opening "I just saw the announcement that the Norminette..." — the Norminette is
42's internal code linter, so that sentence is meaningless to every single
person the post is aimed at. The rule is one rule, stated once, shared by every
prompt in this module. ``NAME_IS_A_LABEL_RULE`` and ``CONNECT_TO_42_RULE`` are
carried the same way, for the same reason.

``gemini_text`` and ``gemini_search`` are imported BY NAME — the tests
monkeypatch ``gti.gemini_text`` / ``gti.gemini_search``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import config
from . import general_trends_store as store
from . import trend_ideation
from . import trend_scoring
from .general_trends_pipeline import evidence_line
from .trend_ideation import IdeationUnavailable, TOOLS, parse_prefill, schema_for
from .vertex_client import VertexBudgetExceededError, gemini_search, gemini_text

log = logging.getLogger("archivist.general_trend_ideation")

EVIDENCE_LINES = 6

# One research answer's clamps, and how much of it reaches a prompt.
MAX_RESEARCH_WHAT = 900
MAX_RESEARCH_WHY = 600
MAX_RESEARCH_SOURCES = 6

# Where a persisted lookup lives on the trend row: `metadata.research`. Nothing
# in this module WRITES there — see `_RESEARCH_CACHE` — but the read path honours
# it, so the day the store grows a metadata setter the cache becomes durable
# with no change here.
RESEARCH_METADATA_KEY = "research"

# The grounded call is a nice-to-have on a user-facing click, so it is bounded.
# `gemini_search` is @_retry()'d (4 attempts, exponential backoff), which on a
# broken Vertex setup can add a minute to "Make this" before it degrades — this
# is the ceiling on that.
RESEARCH_TIMEOUT_SEC = 60

# How many lookups the in-process cache holds before evicting the oldest.
RESEARCH_CACHE_MAX = 500

# --------------------------------------------------------------------------- #
# The shared brainstorm primitives, re-exported under the names this module has
# always used. ``trend_ideation`` owns them — one set of clamps, one schema
# builder, one parser — because the competitor and news paths brainstorm too.
# Kept as names here (rather than call sites reaching across) because the router,
# the prompts below and the tests all read them by name.
# --------------------------------------------------------------------------- #
IDEAS_PER_ROUND = trend_ideation.IDEAS_PER_ROUND
IDEA_FORMATS = trend_ideation.IDEA_FORMATS
MAX_IDEA_TITLE = trend_ideation.MAX_IDEA_TITLE
MAX_IDEA_SHOT = trend_ideation.MAX_IDEA_SHOT
MAX_IDEA_WHY = trend_ideation.MAX_IDEA_WHY
MAX_FEEDBACK = trend_ideation.MAX_FEEDBACK
MAX_EXCLUDE = trend_ideation.MAX_EXCLUDE

# The picked idea rendered as an order, and the reply clamped into ideas. Both
# take the general path's full tool set: a trend can be made any of the eight
# ways (a news story cannot — see ``trend_ideation.NEWS_TOOLS``).
_idea_block = trend_ideation.idea_block
parse_ideas = trend_ideation.parse_ideas

# The plain-language rule is ONE rule and lives in ONE place: ``trend_ideation``
# is the module both ideation paths already share (TOOLS, schema_for,
# parse_prefill), so it owns the string and every prompt here imports it rather
# than restating it. Re-exported because this module's own tests and prompts
# read it by name.
PLAIN_LANGUAGE_RULE = trend_ideation.PLAIN_LANGUAGE_RULE

# THE BUG THIS FILE EXISTS TO PREVENT, written as a rule. `#spidermanbrandnewday`
# produced "Leaving my old life behind", "My 'brand new day' is every day" and
# "My first day with zero experience" — three ideas about a fresh start, from a
# hashtag that names a film. The model had treated a TITLE as a DESCRIPTION.
NAME_IS_A_LABEL_RULE = (
    "NEVER READ THE NAME LITERALLY. A trend's name — a hashtag, a sound title, a "
    "meme label — is a LABEL FOR A THING, not a description of it. "
    "\"#spidermanbrandnewday\" is the title of a Spider-Man film: it is NOT about "
    "\"a brand new day\", and an idea about fresh starts, new beginnings or first "
    "days is a WRONG ANSWER. \"#rushready\" is not about being in a rush. Build "
    "every idea around WHAT THE THING ACTUALLY IS — which the \"WHAT THIS ACTUALLY "
    "IS\" block tells you when it is present. If it says a film, the idea has to "
    "involve THAT FILM: its characters, its story, the argument its fans are "
    "having. If it says a game, a song, a person or a news event, the same. The "
    "words inside the title are not the subject."
)

# The other half of what the owner asked for: not "a post about Spider-Man" and
# not "a post about 42", but the collision of the two.
CONNECT_TO_42_RULE = (
    "PUT THE SUBJECT INSIDE 42. Every idea has to be the actual subject MEETING "
    "this school — Spider-Man at 42, not a generic post about new beginnings; the "
    "game's characters in the lab at 3am, not a post about gaming. Name the "
    "subject on screen. An idea that would still make sense with the subject "
    "deleted has failed.\n"
    "And never pretend 42 is something it is not. It is a coding school: not a "
    "games studio, not a film studio, not a sports brand, not a sponsor and not a "
    "partner of whatever the trend is about. The joke is what happens when THAT "
    "subject walks into THIS school."
)

# The school, described the way the rule demands. The old wording here said
# "peer-to-peer" and "an intensive month called the Piscine" — i.e. it handed
# the model two of the exact words the rule bans and then asked it not to use
# them. Same facts, words a stranger already owns. It moved down to
# ``trend_ideation`` when the brainstorm did, so the post and news prompts get
# the same description of the school rather than a fourth variant of it.
_SCHOOL = trend_ideation.SCHOOL

_SYSTEM = (
    "You turn an internet trend — a meme, a format, a sound, a challenge, a "
    "moment — into a brief that 42 Abu Dhabi's social team can execute.\n\n"
    + _SCHOOL + "\n\n"
    + PLAIN_LANGUAGE_RULE + "\n\n"
    + NAME_IS_A_LABEL_RULE + "\n\n"
    + CONNECT_TO_42_RULE + "\n\n"
    "Rules:\n"
    "- Write 42's OWN take on the trend: keep what makes the trend recognisable "
    "(the format, the beat, the joke structure) and put 42's students, campus "
    "and free-to-study, learn-from-each-other culture inside it.\n"
    "- Concrete beats clever: name the setting, the people and the action, the "
    "way a marketer would type it into a brief box.\n"
    "- Never mock a real person, a tragedy or a community. If the trend is "
    "sensitive, ride the FORMAT, not the subject.\n"
    "- No hashtags, no emoji, no camera jargon unless the tool asks for it.\n"
    "- Fill ONLY the fields the requested tool asks for. Leave the rest out."
)

# Assembled by the shared builder: the school, the plain-language rule and the
# shootable/never-mock bullets are the SAME strings the post and news brainstorm
# sends, and only the opening sentence and the two trend-specific rules are
# supplied here. They used to be a second copy that happened to match.
_BRAINSTORM_SYSTEM = trend_ideation.brainstorm_system(
    opening=(
        "You are the ideas person in the room when 42 Abu Dhabi's social team sits "
        "down with an internet trend and asks \"so what would OUR version be?\". You "
        "answer with three DIFFERENT ideas — not one idea worded three ways."
    ),
    rules=(NAME_IS_A_LABEL_RULE, CONNECT_TO_42_RULE),
    subject="trend",
)

# The brainstorm response schema for the general path's tool set — a trend may
# be made any of the eight ways. ``ideas_schema`` memoises per tool set, so this
# is the same object every call sends (the tests assert that identity, and a
# fresh dict per call would make `is` quietly false).
_IDEAS_SCHEMA: dict = trend_ideation.ideas_schema(TOOLS)


def _tool_menu() -> str:
    """Every tool, one line each. ``trend_ideation.tool_menu`` does the
    rendering — it reads the FIRST line of each tool's own brief, so the menu
    cannot drift from what the brief actually asks for.

    Nothing in this module calls it now that the ideas brief is assembled by the
    shared builder, which renders the menu itself. It stays as this module's
    name for that menu — the tests read it by name to pin the two as the same
    list."""
    return trend_ideation.tool_menu(TOOLS)


# Same shared builder as the post and news brief. Only the paragraphs that are
# genuinely about a TREND are written here — the ask, the name-is-a-label
# warning, and the two bullets whose wording names the trend itself; the spread,
# the shot-not-concept paragraph, the stranger test, the tool menu and `format`
# come from ``trend_ideation.ideas_brief``. Tuning any of those now moves both
# feeds at once, which is the point: it is one step, not two.
_IDEAS_BRIEF = trend_ideation.ideas_brief(
    ask=(f"GIVE THE TEAM {IDEAS_PER_ROUND} IDEAS FOR HOW 42 ABU DHABI COULD DO "
         "THIS TREND."),
    framing=(
        "Before you write anything, be sure you know what this trend IS. The name "
        "is a LABEL, not a description. If a \"WHAT THIS ACTUALLY IS\" block "
        "appears above, that is the subject and it beats everything else on this "
        "page — all three ideas must put THAT subject inside 42, and each one must "
        "name it on screen. An idea that only reuses the words in the name, with "
        "the subject nowhere in it, is a wrong answer and will be thrown away.",
    ),
    subject="trend",
    straight=("restage the trend literally, on campus, with 42 students. The "
              "format stays instantly recognisable; only the people and the "
              "place change."),
    flipped=("subvert it. Invert the punchline, play the other side of the joke, "
             "or let the trend be wrong about 42 and have 42 answer back."),
    tools=TOOLS,
)


# =========================================================================== #
# Research — look the trend up before recommending anything
# =========================================================================== #

_RESEARCH_SYSTEM = (
    "You are a researcher with a live web search. You report what you FIND, not "
    "what you remember. Your answer decides what a marketing team makes, so a "
    "confident guess is worse than an honest \"not clear\".\n\n"
    "How to read a trend name:\n"
    "- A hashtag or a sound title is a LABEL for a thing. It is almost never a "
    "description of it. Search for the label, then report the THING.\n"
    "- Say plainly which kind of thing it is: a film, a TV show, a game, a song "
    "or sound, a real person, a sports moment, a news event, a product, or a "
    "meme/challenge format people are recreating.\n"
    "- Name it. Titles, characters, people, dates, the studio or the team. If it "
    "is a film, say the film's full name and when it comes out or came out.\n"
    "- Prefer what is happening RIGHT NOW over background history. If the same "
    "name has an older meaning (an old comic run, a 1990s song), say which one "
    "people are actually posting about today and say the other is not it.\n"
    "- If your search does not tell you what it is, say \"not clear from the "
    "search\" and say what you did find. Do not invent.\n\n"
    "Answer in EXACTLY these two labelled blocks, plain prose, no bullet points, "
    "no markdown, nothing else:\n"
    "WHAT IT IS: <3-5 sentences naming the thing and what kind of thing it is>\n"
    "WHY PEOPLE POST: <2-4 sentences on who is posting under it and what they "
    "are actually doing — reacting, arguing, recreating a specific shot, using "
    "the sound, joking about one specific moment>"
)


def build_research_prompt(*, trend: dict, sightings: Optional[list[dict]]) -> str:
    """Pure — the one grounded lookup. The tests drive this directly.

    Feeds the model the RAW material (the name exactly as collected, the
    unedited evidence lines, the links) rather than a paraphrase: a paraphrase
    is where the original misreading was introduced, so handing it over would
    just launder the guess."""
    name = " ".join(str(trend.get("title") or "").split())[:160]
    parts: list[str] = [
        "Our collector found this trend on the internet and we are about to "
        "build a social post around it. Before anyone writes anything, work out "
        "WHAT IT ACTUALLY IS. Search for it.",
        "THE NAME, EXACTLY AS WE COLLECTED IT:\n  " + (name or "(no name)"),
    ]

    ours: list[str] = []
    summary = " ".join(str(trend.get("summary") or "").split())[:400]
    if summary:
        ours.append(f"our note: {summary}")
    if trend.get("topic_summary"):
        ours.append("our earlier guess at what it is: "
                    + " ".join(str(trend["topic_summary"]).split())[:400])
    if ours:
        parts.append(
            "WHAT WE THINK IT IS — TREAT THIS AS UNRELIABLE. It was written from "
            "the name alone by a model with no search, and it is exactly what we "
            "are asking you to check:\n" + "\n".join("  " + o for o in ours)
        )

    ev = [s for s in (sightings or []) if isinstance(s, dict)][:EVIDENCE_LINES]
    if ev:
        lines = []
        for s in ev:
            line = "  - " + evidence_line(s)
            url = str(s.get("url") or "").strip()
            if url:
                line += f"  {url[:200]}"
            lines.append(line)
        parts.append(
            "WHERE WE SAW IT — raw lines from the sources, unedited, including "
            "the real posts and the real accounts behind them:\n" + "\n".join(lines)
        )

    if trend.get("url"):
        parts.append(f"THE LINK WE STORED:\n  {trend['url']}")
    sources = [str(s) for s in (trend.get("sources") or []) if s]
    if sources:
        parts.append("SOURCES THAT LISTED IT: " + ", ".join(sources))

    parts.append(
        "Search now and answer the two blocks. Be specific enough that someone "
        "who has never seen this name knows exactly what it is and could name it "
        "out loud."
    )
    return "\n\n".join(parts)


_WHAT_RE = re.compile(r"\**\s*WHAT\s+IT\s+IS\s*\**\s*:?\s*\**", re.I)
_WHY_RE = re.compile(r"\**\s*WHY\s+PEOPLE\s+POST\s*\**\s*:?\s*\**", re.I)


# Leading markdown only: bullets, and a `#` that is a HEADING (followed by a
# space). A bare `#` is never stripped — the subject of half these lookups is a
# hashtag, and eating its `#` would be a small, permanent lie in the UI.
_LEADING_MD = re.compile(r"^(?:[*\-•]\s*|#{1,6}\s+)+")


def _clean_prose(text: str, limit: int) -> str:
    """Collapse whitespace and strip the markdown the model sprinkles in even
    when told not to. One line of prose, clamped."""
    out = " ".join(str(text or "").split())
    out = out.replace("**", "").replace("__", "")
    return _LEADING_MD.sub("", out).strip()[:limit]


def empty_research() -> dict:
    """What a failed, skipped or unusable lookup returns. Same SHAPE as a real
    one so no caller has to branch on None — ``what_it_is`` being empty is the
    single "we did not find out" signal."""
    return {"what_it_is": "", "why_people_post": "", "sources": [],
            "researched_at": "", "cached": False}


def parse_research(text: Any, sources: Any = None) -> dict:
    """Pure — split the grounded prose into the two blocks it was asked for.

    Grounding forbids a response schema (see ``vertex_client._SEARCH_WITH_SCHEMA``),
    so this is the parser that replaces one. Deliberately forgiving: an answer
    that skipped the labels is still worth keeping, so the whole reply becomes
    ``what_it_is`` rather than being thrown away."""
    raw = str(text or "").strip()
    if not raw:
        return empty_research()

    what, why = raw, ""
    m_what = _WHAT_RE.search(raw)
    body = raw[m_what.end():] if m_what else raw
    m_why = _WHY_RE.search(body)
    if m_why:
        what, why = body[:m_why.start()], body[m_why.end():]
    else:
        what = body

    clean_sources: list[dict] = []
    seen: set[str] = set()
    for s in (sources or []):
        if not isinstance(s, dict):
            continue
        uri = str(s.get("uri") or "").strip()[:500]
        title = " ".join(str(s.get("title") or "").split())[:120]
        key = uri or title
        if not key or key in seen:
            continue
        seen.add(key)
        clean_sources.append({"title": title or uri, "uri": uri})
        if len(clean_sources) >= MAX_RESEARCH_SOURCES:
            break

    return {
        "what_it_is": _clean_prose(what, MAX_RESEARCH_WHAT),
        "why_people_post": _clean_prose(why, MAX_RESEARCH_WHY),
        "sources": clean_sources,
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }


def has_research(research: Any) -> bool:
    """One definition of "we actually found something", used by the prompt
    builders and the cache alike."""
    return bool(isinstance(research, dict) and research.get("what_it_is"))


# The cache. It SHOULD live on `general_trends.metadata.research` — pressing
# "Make this" on the same trend next week would then still be free — but this
# module does not own `general_trends_store` and that file has no metadata
# setter it can borrow: `save_score` merges metadata, but it also overwrites
# topic / topic_summary / fit_score / scored_at from the same dict, so calling
# it with a research payload would blank a trend's whole AI verdict. So: an
# in-process cache with a TTL, and a READ path that already prefers a persisted
# `metadata.research` if one ever appears. See the report — the store needs a
# `set_metadata(trend_id, patch)`.
_RESEARCH_CACHE: dict[int, tuple[float, dict]] = {}


def _ttl_sec() -> float:
    """0 (or less) means an entry never expires inside a process — the
    cost-safe reading of "how long is a lookup good for"."""
    hours = int(config.general_trends_research_ttl_hours or 0)
    return float(hours * 3600) if hours > 0 else 0.0


def _fresh(stamped_at: float) -> bool:
    ttl = _ttl_sec()
    return ttl <= 0 or (time.monotonic() - stamped_at) < ttl


def _persisted_research(trend: Optional[dict]) -> Optional[dict]:
    """A lookup saved on the row itself, if anything ever writes one. Read-only
    and fully defensive — `metadata` is free-form jsonb."""
    meta = (trend or {}).get("metadata")
    if not isinstance(meta, dict):
        return None
    saved = meta.get(RESEARCH_METADATA_KEY)
    if not has_research(saved):
        return None
    stamped = str(saved.get("researched_at") or "")
    ttl = _ttl_sec()
    if ttl > 0 and stamped:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(stamped)).total_seconds()
        except ValueError:
            age = 0.0
        if age > ttl:
            return None
    return parse_research(
        f"WHAT IT IS: {saved.get('what_it_is')}\n"
        f"WHY PEOPLE POST: {saved.get('why_people_post') or ''}",
        saved.get("sources"),
    ) | {"researched_at": stamped or datetime.now(timezone.utc).isoformat()}


def _memory_research(trend_id: int) -> Optional[dict]:
    hit = _RESEARCH_CACHE.get(int(trend_id))
    if hit is None:
        return None
    stamped_at, payload = hit
    if _fresh(stamped_at):
        return dict(payload)
    _RESEARCH_CACHE.pop(int(trend_id), None)
    return None


def cached_research(trend_id: int, trend: Optional[dict] = None) -> Optional[dict]:
    """The lookup we already have for this trend, or None. Memory first (this
    process paid for it), then the row."""
    return _memory_research(trend_id) or _persisted_research(trend)


def _remember_research(trend_id: int, research: dict) -> None:
    """Bounded: with no TTL configured this dict would otherwise grow for the
    life of the process, one entry per trend anyone ever clicked."""
    _RESEARCH_CACHE[int(trend_id)] = (time.monotonic(), dict(research))
    while len(_RESEARCH_CACHE) > RESEARCH_CACHE_MAX:
        _RESEARCH_CACHE.pop(next(iter(_RESEARCH_CACHE)))   # oldest insertion first


async def _persist_research(trend_id: int, research: dict) -> None:
    """Write the lookup to `general_trends.metadata.research` so it survives a
    restart and is not paid for once per worker process.

    Best-effort by design: the answer is already in memory and already in the
    prompt by the time this runs, so a failed write costs a future cache hit and
    nothing else. It must never turn a successful "Make this" into an error."""
    if not has_research(research):
        return          # never persist a failed lookup — it would cache the failure
    setter = getattr(store, "set_metadata", None)
    if setter is None:  # a store (or a test double) that predates the setter
        return
    try:
        await setter(int(trend_id), {RESEARCH_METADATA_KEY: research})
    except Exception:   # noqa: BLE001 — a cache miss is not worth an exception
        log.info("[general-trends] could not persist research for trend #%s", trend_id,
                 exc_info=True)


async def research_trend(*, trend_id: int, force: bool = False,
                         trend: Optional[dict] = None,
                         sightings: Optional[list[dict]] = None) -> dict:
    """What this trend ACTUALLY is, looked up with Google Search grounding.

    Returns ``{what_it_is, why_people_post, sources: [{title, uri}],
    researched_at, cached}``. ``what_it_is == ""`` means "we did not find out" —
    the ONLY failure signal, because this must never break "Make this": a search
    outage, a timeout, a budget cap or an unparseable reply all degrade to an
    empty dict and the ideas are then written without it.

    ``force=True`` re-runs even when a fresh answer is cached. ``trend`` /
    ``sightings`` are the already-loaded row and evidence — passing them keeps
    ``brainstorm_ideas`` to a single database read.

    Raises ``ValueError`` on a junk id and ``LookupError`` when the trend is
    gone, matching the rest of this module; nothing else escapes.
    """
    tid = int(trend_id)
    if not config.general_trends_research:
        return empty_research()

    # Memory first — a hit here needs no database read at all. The row's own
    # copy can only be consulted once the row is loaded, so it is checked after.
    if not force:
        hit = _memory_research(tid)
        if hit is not None:
            return dict(hit) | {"cached": True}

    if trend is None:
        trend = await store.get_trend(tid)
        if not trend:
            raise LookupError("trend not found")
        if sightings is None:
            try:
                sightings = (await store.sightings_for(
                    [tid], per_trend=EVIDENCE_LINES)).get(tid, [])
            except Exception:  # noqa: BLE001 — evidence is nice-to-have
                sightings = []

    if not force:
        hit = _persisted_research(trend)
        if hit is not None:
            _remember_research(tid, hit)      # don't re-read the row next time
            return dict(hit) | {"cached": True}

    prompt = build_research_prompt(trend=trend, sightings=sightings)
    try:
        # No json_schema, by law (grounding + structured output are mutually
        # exclusive). 4000 tokens because 2.5-pro spends hidden thinking tokens
        # from the same pool and a starved call returns an EMPTY STRING rather
        # than raising — which would look exactly like "found nothing".
        text, srcs = await asyncio.wait_for(
            gemini_search(prompt, system=_RESEARCH_SYSTEM, temperature=0.2,
                          max_tokens=4000),
            timeout=RESEARCH_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001 — a lookup NEVER fails the click
        log.warning("[general-trends] research lookup failed (trend %s): %s",
                    tid, str(exc)[:200])
        return empty_research()

    out = parse_research(text, srcs)
    if not has_research(out):
        log.info("[general-trends] research returned nothing usable (trend %s)", tid)
        return empty_research()
    _remember_research(tid, out)
    await _persist_research(tid, out)
    log.info("[general-trends] researched trend %s: %s (%d sources)",
             tid, out["what_it_is"][:120], len(out["sources"]))
    return out


def research_block(research: Any) -> str:
    """The lookup, rendered as the authority on the subject. Empty string when
    there is nothing to say, so the prompt is unchanged on a failed lookup."""
    if not has_research(research):
        return ""
    bits = [f"what it is: {research['what_it_is']}"]
    why = str(research.get("why_people_post") or "").strip()
    if why:
        bits.append(f"why people post it: {why}")
    for src in (research.get("sources") or [])[:MAX_RESEARCH_SOURCES]:
        if isinstance(src, dict):
            title = str(src.get("title") or "").strip()
            uri = str(src.get("uri") or "").strip()
            if title or uri:
                bits.append("source: " + (f"{title} — {uri}" if title and uri
                                          else title or uri))
    return (
        "WHAT THIS ACTUALLY IS (looked up just now, with a live web search):\n"
        + "\n".join("  " + b for b in bits)
        + "\n\nThis block is the AUTHORITY on the subject. It was searched for "
          "seconds ago; the name above is only a LABEL and everything else we "
          "stored about this trend was written from that label by a model with "
          "no search, so it may be a guess. Where they disagree, THIS BLOCK "
          "WINS. Build the ideas around the thing described here."
    )


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value or "")[:10]


def _trend_parts(trend: dict, sightings: Optional[list[dict]],
                 research: Any = None) -> list[str]:
    """The description of one trend — the SAME blocks for the brief call and the
    brainstorm call, so the two never disagree about what the trend is.

    ``research`` is ``research_trend``'s answer. It is placed AFTER the evidence
    and BEFORE the scorer's notes on purpose: the notes are what the model wrote
    from the name alone, and the block immediately above them says so."""
    parts: list[str] = []
    bits = [f"name: {' '.join(str(trend.get('title') or '').split())[:160]}"]
    sources = [str(s) for s in (trend.get("sources") or []) if s]
    if sources:
        bits.append("seen on: " + ", ".join(sources))
    regions = [str(r) for r in (trend.get("regions") or []) if r]
    if regions:
        bits.append("regions: " + ", ".join(regions))
    first, last = _date(trend.get("first_seen_at")), _date(trend.get("last_seen_at"))
    if first or last:
        bits.append(f"seen: {first or '?'} → {last or '?'}")
    if trend.get("sighting_count"):
        bits.append(f"sightings: {trend['sighting_count']}")
    if trend.get("velocity") is not None:
        try:
            bits.append(f"heat: {round(float(trend['velocity']) * 100)}/100")
        except (TypeError, ValueError):
            pass
    summary = " ".join(str(trend.get("summary") or "").split())[:600]
    bits.append(f"summary: {summary or '(none)'}")
    if trend.get("url"):
        bits.append(f"link: {trend['url']}")
    parts.append("THE TREND:\n" + "\n".join("  " + b for b in bits))

    ev = [s for s in (sightings or []) if isinstance(s, dict)][:EVIDENCE_LINES]
    if ev:
        parts.append("WHERE WE SAW IT:\n" + "\n".join("  - " + evidence_line(s) for s in ev))

    block = research_block(research)
    if block:
        parts.append(block)

    if trend.get("scored_at"):
        scored = []
        if trend.get("topic_summary"):
            scored.append(f"what it is: {trend['topic_summary']}")
        if trend.get("topic"):
            scored.append(f"angle: {trend['topic']}")
        if trend.get("theme"):
            scored.append(f"theme: {trend['theme']}")
        if trend.get("trend_kind"):
            scored.append(f"kind: {trend['trend_kind']}")
        if trend.get("sensitivity"):
            scored.append(f"sensitivity: {trend['sensitivity']}")
        if trend.get("fit_score") is not None:
            line = f"fit for 42: {trend['fit_score']}/100"
            if trend.get("fit_reason"):
                line += f" — {trend['fit_reason']}"
            scored.append(line)
        if trend.get("uae_relevance") is not None:
            scored.append(f"UAE relevance: {trend['uae_relevance']}/100")
        if trend.get("suggested_angle"):
            scored.append(f"how 42 could ride it: {trend['suggested_angle']}")
        if trend.get("suggested_format"):
            scored.append(f"suggested format: {trend['suggested_format']}")
        if trend.get("effort"):
            scored.append(f"effort: {trend['effort']}")
        if trend.get("tags"):
            scored.append("tags: " + ", ".join(str(t) for t in trend["tags"][:8]))
        if scored:
            head = "WHAT THE SCORER ALREADY WORKED OUT:"
            if block:
                # Named as a guess only when there is something better to
                # compare it against; with no research it is all we have.
                head = ("WHAT THE SCORER ALREADY WORKED OUT — written from the "
                        "name with NO search, so treat every line as a guess "
                        "that the block above may have just disproved:")
            parts.append(head + "\n" + "\n".join("  " + s for s in scored))
    else:
        parts.append("This trend has not been scored yet — work from the name, the "
                     "summary and the evidence above.")
    return parts


def build_prompt(
    *, trend: dict, sightings: Optional[list[dict]], tool: str, brand_note: str = "",
    idea: Optional[dict] = None, research: Any = None,
) -> str:
    """Pure — the tests drive this directly. ``idea`` is one entry from
    ``brainstorm_ideas``; with none, the prompt is exactly what it always was.
    ``research`` is ``research_trend``'s answer, and an empty one adds nothing."""
    parts: list[str] = []
    if brand_note:
        parts.append("WHAT 42 SOUNDS AND LOOKS LIKE:\n" + brand_note.strip())
    parts.extend(_trend_parts(trend, sightings, research))
    if isinstance(idea, dict):
        parts.append(_idea_block(idea))
    parts.append(trend_ideation._TOOL_BRIEFS[tool])
    parts.append(
        ("Write the picked idea as this tool's brief now."
         if isinstance(idea, dict) else "Write 42's version of this trend now.")
        + " Fill only the fields this tool asks for."
    )
    return "\n\n".join(parts)


def build_ideas_prompt(
    *, trend: dict, sightings: Optional[list[dict]], feedback: str = "",
    exclude: Optional[list[str]] = None, brand_note: str = "",
    research: Any = None,
) -> str:
    """Pure — the brainstorm prompt. The tests drive this directly."""
    parts: list[str] = []
    if brand_note:
        parts.append("WHAT 42 SOUNDS AND LOOKS LIKE:\n" + brand_note.strip())
    parts.extend(_trend_parts(trend, sightings, research))
    parts.append(_IDEAS_BRIEF)
    # What the team turned down, then what they asked for instead — the shared
    # block, so a re-roll here and a re-roll on a post read the same way.
    said = trend_ideation.brainstorm_block(feedback=feedback, exclude=exclude)
    if said:
        parts.append(said)
    parts.append(f"Write the {IDEAS_PER_ROUND} ideas now.")
    return "\n\n".join(parts)


def prefill_post_for(trend: dict) -> dict:
    """The ``post`` shim handed to ``trend_ideation.parse_prefill`` so the
    envelope's fallbacks and ids come from the trend row."""
    return {
        "id": int(trend["id"]),
        "topic": trend.get("topic"),
        "caption": trend.get("summary"),
        "suggested_angle": trend.get("suggested_angle"),
        "topic_summary": trend.get("topic_summary"),
        "permalink": trend.get("url"),
    }


async def _load(trend_id: int) -> tuple[dict, list[dict], str]:
    """The trend, its evidence and the brand note. Raises LookupError when the
    trend is gone; evidence and brand note degrade to empty, never raise."""
    trend = await store.get_trend(int(trend_id))
    if not trend:
        raise LookupError("trend not found")
    try:
        sightings = (await store.sightings_for([int(trend["id"])],
                                               per_trend=EVIDENCE_LINES)).get(int(trend["id"]), [])
    except Exception:  # noqa: BLE001 — evidence is nice-to-have
        sightings = []
    brand_note = await trend_scoring._brand_note()   # "" on any failure, never raises
    return trend, sightings, brand_note


def _classify(exc: Exception, what: str, trend_id: int) -> Exception:
    """The shared failure taxonomy for both model calls. Returns the exception
    to raise: IdeationUnavailable (a config problem the user can act on, → 422)
    or RuntimeError (transient, → 502)."""
    if isinstance(exc, VertexBudgetExceededError):
        # Raised from inside @_retry, so 4 attempts are already gone. It is a
        # spend cap, not a blip — say so instead of "try again".
        return IdeationUnavailable(
            "the Vertex spend cap was reached — raise VEO_BUDGET_USD or wait for "
            "the meter to reset, then try again"
        )
    if trend_scoring._is_unavailable(exc):
        return IdeationUnavailable(trend_scoring._explain_unavailable(exc))
    log.exception("[general-trends] %s call failed (trend %s)", what, trend_id)
    return RuntimeError(f"couldn't reach the ideation model: {str(exc)[:200]}")


def _usable(raw: Any) -> dict:
    """gemini_text NEVER raises on unparseable JSON — it returns the {"_raw": …}
    sentinel. Both halves of this guard are load-bearing."""
    if not isinstance(raw, dict) or raw.get("_raw") is not None:
        raise RuntimeError("the ideation model returned an unreadable answer — try again")
    return raw


async def brainstorm_ideas(*, trend_id: int, feedback: str = "",
                           exclude: Optional[list[str]] = None) -> dict:
    """THREE different ways 42 could do this trend, in one model call.

    ``feedback`` is what the owner typed when none of the last three landed;
    ``exclude`` is the titles they have already seen. Same error taxonomy as
    ``generate_prefill``: LookupError (gone) / ValueError (bad id) /
    IdeationUnavailable (config) / RuntimeError (transient).

    LOOKS FIRST, then generates. The lookup is cached, so a re-roll (and the
    ``/ideas`` call afterwards) costs nothing more, and it is never fatal — a
    failed search just means the ideas are written the way they always were.
    Its answer is returned as ``research`` so the dialog can show what it found
    instead of asking the owner to trust that it looked."""
    trend, sightings, brand_note = await _load(int(trend_id))
    research = await research_trend(trend_id=int(trend["id"]), trend=trend,
                                    sightings=sightings)
    prompt = build_ideas_prompt(trend=trend, sightings=sightings, feedback=feedback,
                                exclude=exclude, brand_note=brand_note,
                                research=research)
    try:
        # temperature 0.9, not the 0.7 of the brief call: this is the DIVERGENT
        # step and three ideas that agree with each other is the failure mode.
        # 6000 max_tokens for the same reason as everywhere else here — 2.5-pro
        # spends hidden thinking tokens out of the same output pool, and a
        # starved call returns an empty string rather than an error.
        raw = await gemini_text(
            prompt, system=_BRAINSTORM_SYSTEM, json_schema=_IDEAS_SCHEMA,
            temperature=0.9, max_tokens=6000,
        )
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc, "brainstorm", trend_id) from exc

    ideas = parse_ideas(_usable(raw), exclude=exclude)
    if not ideas:
        # One or two survivors are fine — the UI shows what it got. Zero means
        # there is nothing to put on screen, so say "try again" rather than
        # hand the owner an empty picker.
        raise RuntimeError("the ideation model returned no usable ideas — try again")
    return {
        "ideas": ideas,
        "trend": {"id": int(trend["id"]),
                  "title": " ".join(str(trend.get("title") or "").split())[:160]},
        "research": research,
    }


async def generate_prefill(*, trend_id: int, tool: str,
                           idea: Optional[dict] = None) -> dict:
    """Returns the prefill envelope. Raises LookupError when the trend is gone,
    ValueError for an unknown tool, IdeationUnavailable for a config problem and
    RuntimeError for anything else the router should surface as a 502.

    ``idea`` is the one the team picked in the brainstorm step; the brief then
    EXECUTES that idea instead of inventing another. With no idea the behaviour
    is exactly what it was before the picker existed."""
    if tool not in TOOLS:
        raise ValueError(f"tool must be one of {', '.join(TOOLS)}")
    trend, sightings, brand_note = await _load(int(trend_id))
    # Same lookup, and in the normal flow it is a cache hit that brainstorm
    # already paid for. It matters here too: the brief re-reads the trend's
    # NAME, so without it a Spider-Man idea can still be written up as a post
    # about "a brand new day".
    research = await research_trend(trend_id=int(trend["id"]), trend=trend,
                                    sightings=sightings)
    prompt = build_prompt(trend=trend, sightings=sightings, tool=tool,
                          brand_note=brand_note,
                          idea=idea if isinstance(idea, dict) else None,
                          research=research)
    try:
        # Same budget reasoning as trend_ideation: 2.5-pro spends thinking
        # tokens out of the same output pool; 6000 is what the structured calls
        # need, and thinking_budget stays unset for a reasoning-heavy call.
        raw = await gemini_text(
            prompt, system=_SYSTEM, json_schema=schema_for(tool),
            temperature=0.7, max_tokens=6000,
        )
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc, "ideation", trend_id) from exc

    return parse_prefill(_usable(raw), tool=tool, post=prefill_post_for(trend))
