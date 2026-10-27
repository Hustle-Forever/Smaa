"""Trend Radar: what is new in AI and coding today, judged for 42 Abu Dhabi.

Once a morning (Gulf time) the radar reads a fixed list of public sources,
keeps what is new, asks Gemini three questions about each item and stores the
ten best. The tab shows that list as simple cards.

  collect   RSS / public JSON where a source has it, plain public pages
            otherwise. Short timeouts. A failing source is skipped, never fatal.
  judge     the studio's Vertex Gemini client, in batches, capped at
            ``MAX_JUDGED`` items a day. Returns category, three 1-5 scores and
            a one-line content idea.
  store     one small JSON file under ``config.trend_radar_root``. No database.

Every parser is a pure function over text or JSON, so the tests feed it
fixtures and never touch the network. There is no generation cap anywhere in
here: one scheduled build a day is the whole budget, and it is bounded by item
count, not by a counter.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from ..config import config

log = logging.getLogger("archivist.trend_radar")

GULF_TZ = ZoneInfo("Asia/Dubai")
TIMEOUT_SEC = 12.0
USER_AGENT = "42-Studio-TrendRadar/1.0 (+https://42abudhabi.ae)"

KEEP_DAYS = 4          # an item older than this is not news any more
MAX_JUDGED = 60        # items sent to the model per day
BATCH_SIZE = 20
TOP_N = 10
PER_SOURCE_CANDIDATES = 8

CATEGORIES: tuple[str, ...] = (
    "AI Models", "AI Agents", "Coding", "Developer Tools", "Open Source",
    "Robotics", "Cybersecurity", "AI Education", "Startups", "Research",
    "UAE/GCC AI", "Competitor Activity",
)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

# kind: rss = feed, page = plain page links, then the special JSON/HTML ones.
SOURCES: tuple[dict, ...] = (
    {"id": "openai", "name": "OpenAI", "kind": "rss", "official": True,
     "url": "https://openai.com/news/rss.xml", "hint": "AI Models"},
    {"id": "deepmind", "name": "Google DeepMind", "kind": "rss", "official": True,
     "url": "https://deepmind.google/blog/rss.xml", "hint": "Research"},
    {"id": "anthropic", "name": "Anthropic", "kind": "page", "official": True,
     "url": "https://www.anthropic.com/news", "link_re": r"^/news/[a-z0-9-]+$",
     "hint": "AI Models"},
    {"id": "github", "name": "GitHub Trending", "kind": "github",
     "url": "https://github.com/trending?since=daily", "hint": "Open Source"},
    {"id": "hf", "name": "Hugging Face", "kind": "huggingface",
     "url": "https://huggingface.co/api/daily_papers", "hint": "Open Source"},
    {"id": "hn", "name": "Hacker News", "kind": "hn",
     "url": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=60",
     "hint": "Developer Tools"},
    {"id": "mit", "name": "MIT Technology Review", "kind": "rss",
     "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
     "hint": "AI Models"},
    {"id": "batch", "name": "The Batch", "kind": "page",
     "url": "https://www.deeplearning.ai/the-batch/",
     "link_re": r"^/the-batch/[a-z0-9-]+/?$", "hint": "AI Education"},
    {"id": "arxiv", "name": "arXiv cs.AI", "kind": "rss", "keywords": True,
     "url": "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=40", "hint": "Research"},
    {"id": "nvidia", "name": "NVIDIA Developer", "kind": "rss", "official": True,
     "url": "https://developer.nvidia.com/blog/feed/", "hint": "Developer Tools"},
    {"id": "mbzuai", "name": "MBZUAI", "kind": "page", "official": True,
     "url": "https://mbzuai.ac.ae/news/", "link_re": r"^/news/[a-z0-9-]+/?$",
     "hint": "UAE/GCC AI"},
    {"id": "aioffice", "name": "UAE AI Office", "kind": "page", "official": True,
     "url": "https://ai.gov.ae/", "link_re": r"^/[a-z0-9-]+/?$",
     "hint": "UAE/GCC AI"},
    {"id": "g42", "name": "G42", "kind": "page", "official": True,
     "url": "https://www.g42.ai/", "link_re": r"^/(resources|news|press)[a-z0-9/-]*$",
     "hint": "UAE/GCC AI"},
    {"id": "hub71", "name": "Hub71", "kind": "page", "official": True,
     "url": "https://www.hub71.com/", "link_re": r"^/(news|blog|insights|press)[a-z0-9/-]*$",
     "hint": "Startups"},
    {"id": "core42", "name": "Core42", "kind": "page", "official": True,
     "url": "https://www.core42.ai/", "link_re": r"^/(news|resources|blog|press)[a-z0-9/-]*$",
     "hint": "UAE/GCC AI"},
)

# Words that make an item from a broad feed (HN, arXiv) worth looking at.
_AI_WORDS = re.compile(
    r"\b(ai|a\.i\.|llm|llms|gpt|claude|gemini|openai|anthropic|deepmind|agent|agents|"
    r"agentic|model|models|neural|transformer|diffusion|copilot|cursor|coding|"
    r"codex|compiler|rust|python|typescript|open[- ]source|robot|robotics|"
    r"security|vulnerab\w*|exploit|gpu|nvidia|inference|rag|mcp|developer|"
    r"programming|software|api|sdk|startup|uae|abu dhabi|emirates|g42|mbzuai)\b",
    re.I,
)


def make_item(*, source: dict, title: str, url: str, summary: str = "",
              published: Optional[datetime] = None, extra: str = "") -> dict:
    """The one shape every parser returns."""
    return {
        "source_id": source["id"],
        "source": source["name"],
        "official": bool(source.get("official")),
        "hint": source.get("hint", ""),
        "title": re.sub(r"\s+", " ", title or "").strip()[:220],
        "url": (url or "").strip(),
        "summary": re.sub(r"\s+", " ", summary or "").strip()[:400],
        "published": published.astimezone(timezone.utc).isoformat() if published else None,
        "extra": extra,
    }


def _strip_html(text: str) -> str:
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def _parse_date(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(text: str, source: dict) -> list[dict]:
    """RSS 2.0 or Atom. A malformed entry is skipped, a malformed feed is []."""
    try:
        root = ET.fromstring(text.lstrip())
    except ET.ParseError:
        return []
    out: list[dict] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        link = ""
        for child in node:
            name = _local(child.tag)
            if name == "link":
                href = child.attrib.get("href")
                if href and (child.attrib.get("rel") in (None, "alternate")):
                    link = link or href
                elif child.text and not link:
                    link = child.text.strip()
            elif name in ("title", "description", "summary", "content", "pubDate",
                          "published", "updated", "date"):
                fields.setdefault(name, "".join(child.itertext()))
        title = _strip_html(fields.get("title", ""))
        if not title or not link:
            continue
        when = _parse_date(fields.get("pubDate") or fields.get("published")
                           or fields.get("updated") or fields.get("date"))
        body = _strip_html(fields.get("description") or fields.get("summary")
                           or fields.get("content") or "")
        out.append(make_item(source=source, title=title, url=link, summary=body,
                             published=when))
    return out


def parse_page_links(html: str, source: dict) -> list[dict]:
    """Article-looking links on a plain page: same site, matching ``link_re``,
    with a headline-sized link text. Undated, so the store dates them by when
    the radar first saw them."""
    pattern = re.compile(source.get("link_re") or r"^/.+")
    base = source["url"]
    host = urlparse(base).netloc.replace("www.", "")
    soup = BeautifulSoup(html or "", "html.parser")
    seen: set[str] = set()
    out: list[dict] = []
    for a in soup.find_all("a", href=True):
        full = urljoin(base, a["href"]).split("#")[0].split("?")[0]
        parsed = urlparse(full)
        if parsed.netloc.replace("www.", "") != host:
            continue
        if not pattern.match(parsed.path) or full.rstrip("/") == base.rstrip("/"):
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True) or a.get("aria-label") or "")
        if len(text) < 18 or full in seen:
            continue
        seen.add(full)
        out.append(make_item(source=source, title=text, url=full))
    return out


def parse_github_trending(html: str, source: dict) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[dict] = []
    for row in soup.select("article.Box-row"):
        a = row.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        name = re.sub(r"\s+", "", a.get_text()).strip("/")
        desc = row.select_one("p")
        stars = row.select_one("span.d-inline-block.float-sm-right")
        out.append(make_item(
            source=source, title=name, url=urljoin("https://github.com", a["href"]),
            summary=desc.get_text(" ", strip=True) if desc else "",
            extra=stars.get_text(" ", strip=True) if stars else "",
        ))
    return out


def parse_hf_papers(data: Any, source: dict) -> list[dict]:
    out: list[dict] = []
    for row in data if isinstance(data, list) else []:
        paper = (row or {}).get("paper") or {}
        pid, title = paper.get("id"), (row.get("title") or paper.get("title") or "")
        if not pid or not title:
            continue
        out.append(make_item(
            source=source, title=title, url=f"https://huggingface.co/papers/{pid}",
            summary=paper.get("summary") or "",
            published=_parse_date(row.get("publishedAt") or paper.get("publishedAt")),
            extra=f"{paper.get('upvotes', 0)} upvotes",
        ))
    return out


def parse_hn(data: Any, source: dict) -> list[dict]:
    out: list[dict] = []
    for hit in (data or {}).get("hits", []) if isinstance(data, dict) else []:
        title = hit.get("title") or ""
        if not title or not _AI_WORDS.search(title):
            continue
        oid = hit.get("objectID")
        out.append(make_item(
            source=source, title=title,
            url=hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
            published=_parse_date(hit.get("created_at")),
            extra=f"{hit.get('points', 0)} points",
        ))
    return out


def parse_source(source: dict, body: str) -> list[dict]:
    """Dispatch one fetched body to its parser. Pure."""
    kind = source["kind"]
    if kind == "rss":
        items = parse_feed(body, source)
        if source.get("keywords"):
            items = [i for i in items if _AI_WORDS.search(i["title"] + " " + i["summary"])]
        return items
    if kind == "page":
        return parse_page_links(body, source)
    if kind == "github":
        return parse_github_trending(body, source)
    try:
        data = json.loads(body)
    except ValueError:
        return []
    if kind == "huggingface":
        return parse_hf_papers(data, source)
    if kind == "hn":
        return parse_hn(data, source)
    return []


async def fetch_source(client: httpx.AsyncClient, source: dict) -> list[dict]:
    """One source. Never raises: a down site is an empty list and a log line."""
    try:
        resp = await client.get(source["url"])
        resp.raise_for_status()
        return parse_source(source, resp.text)
    except Exception as exc:  # noqa: BLE001 - a failing source must not break the radar
        log.warning("[radar] %s skipped: %s", source["id"], exc)
        return []


async def collect() -> tuple[list[dict], dict[str, int]]:
    """Every source at once. Returns (items, {source_id: how many came back})."""
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SEC, follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    ) as client:
        results = await asyncio.gather(*(fetch_source(client, s) for s in SOURCES))
    items: list[dict] = []
    counts: dict[str, int] = {}
    for src, got in zip(SOURCES, results):
        counts[src["id"]] = len(got)
        items.extend(got)
    return items, counts


# --------------------------------------------------------------------------- #
# Pick what to judge
# --------------------------------------------------------------------------- #

def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _norm_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.netloc.replace('www.', '')}{p.path.rstrip('/')}".lower()


def pick_candidates(items: list[dict], first_seen: dict[str, str], *,
                    now: datetime, limit: int = MAX_JUDGED) -> list[dict]:
    """Recent, de-duplicated, and spread across sources.

    An undated item counts as published when the radar first saw it, so a page
    full of old links is not "new" on day two. Duplicates (same link, or the
    same headline from two places) keep the official announcement. Sources then
    take turns so a loud feed cannot crowd the rest out of the 60 slots."""
    cutoff = now - timedelta(days=KEEP_DAYS)
    fresh: list[dict] = []
    for it in items:
        when = _parse_date(it.get("published")) or _parse_date(first_seen.get(it["url"]))
        if when is None:
            when = now
        if when >= cutoff and it.get("title") and it.get("url"):
            fresh.append({**it, "when": when.isoformat()})

    unique_rows: list[dict] = []
    slot: dict[str, int] = {}  # url / headline key -> index in unique_rows
    for it in fresh:
        keys = (_norm_url(it["url"]), _norm_title(it["title"]))
        hit = next((slot[k] for k in keys if k in slot), None)
        if hit is None:
            hit = len(unique_rows)
            unique_rows.append(it)
        elif it["official"] and not unique_rows[hit]["official"]:
            unique_rows[hit] = it
        for k in keys:
            slot.setdefault(k, hit)
    unique = {it["url"]: it for it in unique_rows}

    by_source: dict[str, list[dict]] = {}
    for it in unique.values():
        by_source.setdefault(it["source_id"], []).append(it)
    for rows in by_source.values():
        rows.sort(key=lambda r: r["when"], reverse=True)
        del rows[PER_SOURCE_CANDIDATES:]

    picked: list[dict] = []
    known = [s["id"] for s in SOURCES]
    order = sorted(by_source, key=lambda sid: known.index(sid) if sid in known else len(known))
    while len(picked) < limit and any(by_source[s] for s in order):
        for sid in order:
            if by_source[sid] and len(picked) < limit:
                picked.append(by_source[sid].pop(0))
    return picked


# --------------------------------------------------------------------------- #
# Judge with Gemini
# --------------------------------------------------------------------------- #

_JUDGE_SYSTEM = (
    "You are the trend analyst for 42 Abu Dhabi, a free, peer-to-peer coding school "
    "in Abu Dhabi. Its students are young coders and its marketing team posts on "
    "Instagram and LinkedIn. For each news item decide: how important it is for the "
    "market, how useful it is for 42 (a workshop, hackathon, student project, "
    "curriculum topic, partnership or event), and how well it would work as content "
    "(an Instagram or LinkedIn post, a Reel, an article, a student challenge). "
    "Be strict: most items are a 2 or 3. Reserve 5 for real industry shifts. "
    "Write plain, short English a teenager understands. No jargon, no hype."
)

_JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "the item number given"},
                    "headline": {"type": "string", "description": "the item's title cleaned up: no dates, no 'Read:' prefix, max 80 chars"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "important": {"type": "integer", "description": "1-5 market impact"},
                    "good_for_42": {"type": "integer", "description": "1-5 fit for 42 Abu Dhabi"},
                    "post_it": {"type": "integer", "description": "1-5 worth posting about"},
                    "why": {"type": "string", "description": "ONE short line (max 90 chars): why it matters"},
                    "idea": {"type": "string", "description": "ONE short content idea (max 90 chars), e.g. 'Students test the new AI coding agent' (Reel)"},
                },
                "required": ["n", "headline", "category", "important", "good_for_42", "post_it", "why", "idea"],
            },
        }
    },
    "required": ["items"],
}


def _clamp_score(value: Any) -> int:
    try:
        return max(1, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        return 1


def _short(text: Any, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def build_judge_prompt(batch: list[dict]) -> str:
    lines = [
        f"Judge these {len(batch)} items. Answer once per item, using its number.",
        f"Categories: {', '.join(CATEGORIES)}.",
        "Use 'UAE/GCC AI' for anything about the UAE or Gulf, 'Competitor Activity' for "
        "other coding schools or bootcamps.",
        "",
    ]
    for n, it in enumerate(batch, 1):
        official = " (official announcement)" if it["official"] else ""
        lines.append(f"{n}. [{it['source']}{official}] {it['title']}")
        if it["summary"]:
            lines.append(f"   {it['summary'][:260]}")
        if it["extra"]:
            lines.append(f"   ({it['extra']})")
    return "\n".join(lines)


def apply_judgement(batch: list[dict], answer: Any) -> list[dict]:
    """Merge the model's answer into the items. Pure and forgiving: an entry
    with a bad number or category is dropped, the rest are kept, every score
    is clamped to 1-5 and every string to a card-sized length."""
    out: list[dict] = []
    rows = answer.get("items") if isinstance(answer, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            item = batch[int(row.get("n")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        category = row.get("category")
        if category not in CATEGORIES:
            category = item.get("hint") if item.get("hint") in CATEGORIES else "Research"
        imp, fit, post = (_clamp_score(row.get(k)) for k in ("important", "good_for_42", "post_it"))
        out.append({
            "title": _short(row.get("headline") or item["title"], 120),
            "url": item["url"],
            "source": item["source"],
            "official": item["official"],
            "category": category,
            "important": imp,
            "good_for_42": fit,
            "post_it": post,
            "score": imp + fit + post,
            "why": _short(row.get("why"), 100),
            "idea": _short(row.get("idea"), 100),
            "when": item.get("when"),
        })
    return out


async def judge(candidates: list[dict]) -> list[dict]:
    """Batched Gemini calls (about three a day). One failed batch costs only
    that batch."""
    from . import vertex_client

    async def one(batch: list[dict]) -> list[dict]:
        try:
            answer = await vertex_client.gemini_text(
                build_judge_prompt(batch), system=_JUDGE_SYSTEM,
                json_schema=_JUDGE_SCHEMA, temperature=0.3, max_tokens=8000,
            )
            return apply_judgement(batch, answer)
        except Exception as exc:  # noqa: BLE001
            log.warning("[radar] a judging batch failed: %s", exc)
            return []

    batches = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]
    parts = await asyncio.gather(*(one(b) for b in batches))
    return [row for part in parts for row in part]


def rank(judged: list[dict], top: int = TOP_N) -> list[dict]:
    """Best total first. Ties go to the official announcement, then the newer
    one. At most three from one category so the ten are not all the same."""
    ordered = sorted(
        judged,
        key=lambda r: (r["score"], r["official"], r.get("when") or ""),
        reverse=True,
    )
    picked: list[dict] = []
    per_cat: dict[str, int] = {}
    for row in ordered:
        if per_cat.get(row["category"], 0) >= 3:
            continue
        per_cat[row["category"]] = per_cat.get(row["category"], 0) + 1
        picked.append(row)
        if len(picked) >= top:
            break
    for row in ordered:  # fewer than ten categories could fill it: top up
        if len(picked) >= top:
            break
        if row not in picked:
            picked.append(row)
    return picked


# --------------------------------------------------------------------------- #
# Store: one small JSON file
# --------------------------------------------------------------------------- #

def _store_path():
    return config.trend_radar_root / "radar.json"


def load_store() -> dict:
    try:
        data = json.loads(_store_path().read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_store(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)


def today_gulf(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(GULF_TZ).date().isoformat()


def latest(now: Optional[datetime] = None) -> dict:
    """What the tab shows: the newest radar, whether it is from today, and
    whether a build is running."""
    store = load_store()
    radar = store.get("radar") or {}
    return {
        "date": radar.get("date"),
        "built_at": radar.get("built_at"),
        "items": radar.get("items") or [],
        "is_today": radar.get("date") == today_gulf(now),
        "building": _building,
        "error": store.get("error"),
    }


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

_building = False


def is_building() -> bool:
    return _building


async def build(now: Optional[datetime] = None) -> dict:
    """Collect, judge, rank and store one radar. Raises nothing the caller must
    handle: a total failure is stored as ``error`` and the previous radar is
    left in place for the tab to keep showing."""
    global _building
    if _building:
        return latest(now)
    _building = True
    now = now or datetime.now(timezone.utc)
    try:
        store = load_store()
        seen: dict[str, str] = store.get("first_seen") or {}
        items, counts = await collect()
        for it in items:
            seen.setdefault(it["url"], now.isoformat())
        candidates = pick_candidates(items, seen, now=now)
        if not candidates:
            raise RuntimeError("no sources answered")
        judged = await judge(candidates)
        if not judged:
            raise RuntimeError("the AI could not judge today's items")
        top = rank(judged)
        cutoff = (now - timedelta(days=30)).isoformat()
        store.update({
            "radar": {"date": today_gulf(now), "built_at": now.isoformat(),
                      "items": top, "sources": counts, "judged": len(judged)},
            "first_seen": {u: t for u, t in seen.items() if t >= cutoff},
            "error": None,
        })
        save_store(store)
        log.info("[radar] built %d cards from %d judged items", len(top), len(judged))
        await email_digest(top)
    except Exception as exc:  # noqa: BLE001
        log.exception("[radar] build failed")
        store = load_store()
        store["error"] = _short(exc, 160)
        try:
            save_store(store)
        except OSError:
            pass
    finally:
        _building = False
    return latest(now)


async def email_digest(top: list[dict]) -> bool:
    """Mail the top ten to the reminder address, only if SMTP is configured."""
    from . import email_service

    if not (email_service.email_configured() and config.reminder_email):
        return False
    lines = [f"{i}. [{r['category']}] {r['title']}\n   {r['why']}\n   {r['url']}"
             for i, r in enumerate(top, 1)]
    return await email_service.send_email(
        to=config.reminder_email,
        subject="42 Trend Radar: today's top 10",
        text="Today's AI and coding radar for 42 Abu Dhabi\n\n" + "\n\n".join(lines),
    )
