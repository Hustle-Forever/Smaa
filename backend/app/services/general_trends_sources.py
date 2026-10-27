"""General Trends — sources, parsers and fetchers.

"What is the whole internet doing right now?" — answered from free public
feeds rather than any paid aggregator. One shape out of every source:

  google_trends   daily search spikes per country      (RSS)
  bluesky         Bluesky trending topics              (JSON, public API)
  reddit          the day's TOP posts per subreddit    (Atom — ``.json`` is 403)
  kym             Know Your Meme newsfeed              (RSS)
  kym_entries     KYM new/confirmed entries + sitemap  (RSS + XML + entry HTML)
  kym_editorials  KYM "what is the X meme?" explainers (RSS)
  wikipedia       most-viewed articles, D-1 vs D-2     (JSON, Wikimedia REST)
  youtube         most-popular videos per region       (JSON, needs an API key)
  youtube_shorts  songs creators USED most on Shorts   (JSON, keyless InnerTube)
  x_trends        X/Twitter via the trends24.in mirror (HTML — off by default)
  hacker_news     front page via the Algolia index     (JSON, keyless)
  lemmy           lemmy.world TopDay                   (JSON, keyless)
  github          repos created in the last N days     (JSON, keyless)
  google_news     curated Gulf + tech queries          (RSS, ``when:7d``)
  tiktok          Creative Center trending hashtags    (HTML SSR blob)
  instagram       Graph hashtag recent-media           (needs a live token)
  instagram_watch Graph business-discovery watchlist   (needs a live token)

Design rules this module keeps to:

  * Every parser is a PURE function over text/JSON → ``list[RawItem]`` and
    never raises on a malformed entry (the entry is skipped). Tests feed the
    parsers real, trimmed fixtures and never touch the network.
  * Every fetcher is ``async fetch_<source>(client, …) -> list[RawItem]`` over
    an ``httpx.AsyncClient`` handed in, and DOES raise on HTTP errors — the
    pipeline (``fetch_all``) catches, records the failure per source and moves
    on. Fetchers are the monkeypatch seam (``monkeypatch.setattr(gts,
    "fetch_google_trends", …)``); ``fetch_all`` resolves them from module
    globals at call time so a patched name is honoured.
  * ``RawItem`` is a plain dict with a fixed key set (see ``make_item``) so the
    store / clustering code downstream can rely on every key being present.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import config

log = logging.getLogger("archivist.general_trends_sources")


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

SOURCE_IDS: tuple[str, ...] = (
    "google_trends",
    "bluesky",
    "reddit",
    "kym",
    "kym_entries",
    "kym_editorials",
    "wikipedia",
    "youtube",
    "youtube_shorts",
    "x_trends",
    "hacker_news",
    "lemmy",
    "github",
    "google_news",
    "tiktok",
    "instagram",
    "instagram_watch",
)

SOURCE_LABELS: dict[str, str] = {
    "google_trends": "Google Trends",
    "bluesky": "Bluesky",
    "reddit": "Reddit",
    "kym": "Know Your Meme",
    "kym_entries": "Know Your Meme (entries)",
    "kym_editorials": "Know Your Meme (explainers)",
    "wikipedia": "Wikipedia",
    "youtube": "YouTube",
    "youtube_shorts": "YouTube Shorts (sounds)",
    "x_trends": "X (via trends24)",
    "hacker_news": "Hacker News",
    "lemmy": "Lemmy",
    "github": "GitHub",
    "google_news": "Google News",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "instagram_watch": "Instagram (watch)",
}

# Sources that have no per-country variant.
GLOBAL_REGION = "global"
GLOBAL_SOURCES: frozenset[str] = frozenset({
    "bluesky", "reddit", "kym", "kym_entries", "kym_editorials", "wikipedia",
    "hacker_news", "lemmy", "github", "instagram", "instagram_watch",
})

# Sources whose items are individual posts or bare tokens: they may CORROBORATE
# a trend (attach as a sighting on an existing row) but may never CREATE one.
#
# Measured on the live corpus (363 trends, one real run): 47% of the feed was
# individual Reddit posts and 22% was context-free trends24 hashtags with an
# empty summary — more than two thirds of every card the owner saw was never a
# trend in the first place. A single Reddit post is one person's joke; a single
# Wikipedia row is one page's traffic; an x_trends row is a bare token with no
# summary at all; an instagram_watch row is one creator's post. None of those is
# evidence that anything is spreading, so none may mint a trend on its own.
#
# ENFORCED BY THE PIPELINE (``_file_item``), not here — this module only owns
# the list, because this is where source identity lives.
# MINTING IS AN ALLOWLIST, and the allowlist is "registries of things people
# are MAKING". A trend is a participatory format — a hashtag thousands of
# people posted under, a meme template being remade, a sound being used. A
# source that reports what HAPPENED can corroborate such a format (it supplies
# the why) but can never create one.
#
# Measured on a full live fetch (2026-08-20): 328 items kept, of which only 28
# came from a format registry. The other 300 were google_news 80, reddit 45,
# lemmy 40, wikipedia 40, github 30, hacker_news 28, bluesky 25,
# google_trends 12 — headlines, repos, link-aggregator posts and search spikes.
# Not one of them is something a social team could make their own version of,
# and every one of them used to become a card.
#
# An allowlist rather than a denylist on purpose: a source added later is
# non-minting until someone deliberately decides it is a format registry.
MINTING_SOURCES: frozenset[str] = frozenset({
    "tiktok",      # Creative Center hashtags — carries a POST COUNT, i.e. how
                   # many people actually made one
    "kym",         # Know Your Meme — names a format and explains it
    "kym_entries", # the KYM entry database itself: a NAMED format, with KYM's
                   # own `Type:` taxonomy saying whether it is participatory
    "instagram",   # hashtag + co-tag mining (one item per hashtag, never per post)
    # The songs creators USED most in their Shorts. Using a sound is the
    # cheapest possible act of recreating a format, and this is the only
    # geo-correct participatory feed we have — AE's #1 is genuinely not US's.
    "youtube_shorts",
})
# NOT minting, deliberately: `kym_editorials` is an ARTICLE ABOUT a format
# ("What Is The 'Flobby Samuel Insect' Meme?"), not the format's own entry. It
# is superb corroboration — it explains the why in KYM's own words — but a
# card minted from an explainer would be a card about a magazine piece.


def is_minting(source: Any) -> bool:
    """May an item from this source CREATE a trend, or only corroborate one?"""
    return str(source or "") in MINTING_SOURCES


# Derived, and kept because the pipeline and the tests read it by name. It is
# every known source that is NOT a format registry — the complement, so the two
# can never drift apart.
NON_MINTING_SOURCES: frozenset[str] = frozenset(SOURCE_IDS) - MINTING_SOURCES

# trends24.in country slugs. Unknown geo ⇒ the x_trends unit is skipped.
REGION_SLUGS: dict[str, str] = {
    "US": "united-states",
    "AE": "united-arab-emirates",
    "GB": "united-kingdom",
    "IN": "india",
    "SA": "saudi-arabia",
    "EG": "egypt",
}

# Reddit refuses the generic bot UA; this one is accepted on the Atom feeds.
REDDIT_UA = "42studio-trends/1.0 (by u/42studio)"

GOOGLE_TRENDS_URL = "https://trends.google.com/trending/rss"
BLUESKY_TRENDS_URL = "https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrends"
# /rising is pre-consensus BY CONSTRUCTION: of 167 rising posts captured in the
# live run, not one was still visible 80 minutes later. /top?t=day is the day's
# consensus and costs exactly the same one throttled request. Verified 200 with
# REDDIT_UA on 2026-08-20 (x-ratelimit-used 1, remaining 0, reset 39).
REDDIT_TOP_URL = "https://www.reddit.com/r/{sub}/top.rss"
REDDIT_TOP_PARAMS: dict[str, str] = {"t": "day"}
KYM_NEWSFEED_URL = "https://knowyourmeme.com/newsfeed.rss"
# --- Know Your Meme: the rest of the family -------------------------------- #
# /newsfeed.rss is a TODAY-ONLY feed and returned 4 items on a live run — the
# highest-signal source in the system, throttled to a trickle. These are the
# feeds that carry the entry DATABASE itself (live counts, 2026-08-20):
#
#   /memes/all.rss        16 items, 16/16 inside 7 days  — the new-format feed
#   /memes/confirmed.rss  16 items, editor-validated formats
#   /sitemap.xml          44 /memes/ URLs, 29 of them in NO feed, all recent
#
# all.rss and confirmed.rss are DISJOINT (0 overlap measured), and the sitemap
# adds another 29 on top: 63 distinct entries where the newsfeed gave 3.
#
# Confirmed dead, do not resurrect: /memes/trending (404), /memes/popular.rss
# (all-time — Slender Man, 2010), /memes/newsworthy.rss (abandoned, newest
# 2020), /news.rss (stale since 2025), /memes/{events,subcultures}.rss (404),
# and `?page=`/`?sort=` (silently ignored, byte-identical response).
# /memes/submissions.rss is byte-identical to all.rss — one of the two only.
#
# /memes/deadpool.rss is alive (16 items) but is deliberately NOT a unit: it is
# the list of entries KYM's editors REJECTED as not real memes, so fetching it
# only to mark every row rejected is pure cost. The `rejected` flag still
# exists because a deadpooled entry can reach us through the sitemap.
KYM_ENTRY_FEEDS: dict[str, str] = {
    "all": "https://knowyourmeme.com/memes/all.rss",
    "confirmed": "https://knowyourmeme.com/memes/confirmed.rss",
}
KYM_SITEMAP_URL = "https://knowyourmeme.com/sitemap.xml"
KYM_EDITORIALS_URL = "https://knowyourmeme.com/editorials.rss"
KYM_ENTRY_URL_PREFIX = "https://knowyourmeme.com/memes/"

# Entry-page enrichment costs one GET per entry, so it is BOUNDED: at most this
# many pages per run, newest first, feed entries before sitemap-only ones.
# Measured: 16 entry pages unpaced in 6.4s, all 200, no rate limiting, and the
# pages answer to the plain config User-Agent (no browser UA needed).
KYM_ENTRY_FETCH_MAX = 40
KYM_ENTRY_CONCURRENCY = 4

# KYM's own `Type:` taxonomy IS the owner's definition of a trend, written in
# KYM's vocabulary. A FORMAT type means "there is a thing to make": a template,
# a dance, a snowclone, a sound. A NON-format type means the entry documents
# something that HAPPENED — a death, a film, a character, a viral video — which
# nobody can make their own version of, however big it was.
#
# Slugs, taken from the entry page's /types/<slug> links, so a re-worded label
# on the site cannot silently change the verdict.
KYM_FORMAT_TYPES: frozenset[str] = frozenset({
    "participatory-media", "exploitable", "snowclone", "image-macro", "dance",
    "catchphrase", "remix", "reaction", "copypasta", "visual-effect", "parody",
    "fan-labor", "slang", "song", "ai-generated",
})
#: Recorded for the report, not consulted: `is_format` is decided by the
#: PRESENCE of a format type, so a type nobody has classified yet (KYM ships
#: "animal", which is in neither list) counts as "not a format" rather than
#: quietly passing through an allow-by-default hole.
KYM_NON_FORMAT_TYPES: frozenset[str] = frozenset({
    "death", "film", "character", "viral-debate", "pop-culture-reference",
    "viral-video", "influencer", "vlogger", "creepypasta", "hoax", "animal",
})

#: THE PENGUIN RULE. `Year` on a KYM entry is the format's ORIGIN, not its
#: current wave. `/memes/penguin-walking-toward-mountain-nihilist-penguin` —
#: the owner's own example of a trend, thousands of people restaging it, a UAE
#: shop walking the penguin to their store — is **Year 2008**. Any filter of
#: the shape "Year must be recent" deletes the brief's headline example.
#:
#: So an old Year in a FRESH feed is not a reject, it is a REVIVAL: kept, and
#: stamped `extra["revival"] = True` so downstream can require corroboration.
#: The entry's own numbers then settle it, and this module only EXPOSES them —
#: it invents no threshold:
#:     penguin      71,594 views,  5 videos, 17 photos  (Year 2008) -> real
#:     Mrs. Puff       135 views,  0 videos, 12 photos  (Year 2020) -> stale
#: Both were published to KYM this week. Only the numbers tell them apart.
KYM_REVIVAL_OLDER_THAN_YEARS = 1

#: The honest fix for Year's origin/wave ambiguity: the RSS <description>
#: states the WAVE in prose — "The story was reported in mid-August 2026 and
#: inspired memes that month", "In early 2026, the clip became a prevalent
#: meme". A four-digit year standing near one of these words is the wave year.
_KYM_WAVE_WORDS = re.compile(
    r"viral|virally|spread\w*|trend\w*|report\w*|inspir\w*|resurfac\w*|"
    r"circulat\w*|popular\w*|became|blew up|took off|gained|emerg\w*|"
    r"took over|meme[sd]?\b|challenge",
    re.IGNORECASE,
)
_KYM_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
#: How far either side of a year we look for a wave word. 90 chars ≈ one clause.
_KYM_WAVE_WINDOW = 90
_KYM_TRAILING_READ_MORE = re.compile(r"\s*Read More\s*$", re.IGNORECASE)

# --- YouTube Charts: songs creators USED on Shorts ------------------------- #
# An undocumented InnerTube RPC (charts.youtube.com), keyless, cookieless and
# OAuth-free. Verified live 2026-08-20: AE/SA/US/GB/IN/EG all 200, ~190 kB,
# listType TOP_SHORTS_BY_USAGE, 50-52 rows, endDate 2026-08-13 (weekly).
#
# WHY IT MATTERS: the chart's own subtitle is "Weekly ranking of the songs used
# most by creators on Shorts" — this is a USAGE chart, not a listening chart.
# Using a sound IS making your own version, so every row is a participatory
# format by construction, and it is the ONLY geo-correct one we found: AE's #1
# was "Headlights (feat. KIDDO)" while US's was "misery. — pupsies" in the same
# request second. For a school in Abu Dhabi that difference is the whole point.
YOUTUBE_CHARTS_URL = "https://charts.youtube.com/youtubei/v1/browse"
YOUTUBE_CHARTS_BROWSE_ID = "FEmusic_analytics_charts_home"
YOUTUBE_CHARTS_CLIENT: dict[str, str] = {
    "clientName": "WEB_MUSIC_ANALYTICS",
    "clientVersion": "2.0",
    "hl": "en",
    "theme": "MUSIC",
}
YOUTUBE_CHARTS_FLAGS = "MusicCharts__enable_apac_and_shorts_charts_expansion"
YOUTUBE_SHORTS_CHART_TYPE = "SHORTS_TRACKS_BY_USAGE"
#: WEEKLY, not DAILY: daily is measurably 3 days behind (endDate 2026-08-16 on
#: 2026-08-20) and weekly is only 7 (2026-08-13), for 7x the sample.
YOUTUBE_SHORTS_PERIODS: tuple[str, ...] = ("WEEKLY", "DAILY")
#: No allow-list is needed and none is invented: an unsupported country code is
#: HTTP 400 ("Request contains an invalid argument"), NOT a silent fallback to
#: US the way TikTok's Creative Center does. The server validates for us.
SHORTS_DEFAULT_REGIONS: tuple[str, ...] = ("AE", "SA", "US")
#: The "sound page" — every Short made with this track. The participatory URL:
#: it is literally the list of people who already made their own version.
YOUTUBE_SOUND_URL = "https://www.youtube.com/source/{video_id}/shorts"
WIKIPEDIA_TOP_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y}/{m}/{d}"
)
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
TRENDS24_URL = "https://trends24.in/{slug}/"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"
LEMMY_POSTS_URL = "https://lemmy.world/api/v3/post/list"   # /api/v4/ is 404 — v3 only
LEMMY_POST_URL = "https://lemmy.world/post/{id}"
GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GOOGLE_NEWS_SEARCH_URL = "https://news.google.com/rss/search"
TIKTOK_HASHTAG_URL = "https://ads.tiktok.com/creative/creativeCenter/trends/hashtag"

DEFAULT_TIMEOUT = 20.0
SUMMARY_MAX_CHARS = 600

# XML namespaces.
_NS_HT = "https://trends.google.com/trending/rss"
_NS_ATOM = "http://www.w3.org/2005/Atom"
_NS_MEDIA = "http://search.yahoo.com/mrss/"

# Wikipedia housekeeping pages that are never a "trend".
_WIKI_SKIP_EXACT = frozenset({"Main_Page", "-"})
_WIKI_SKIP_PREFIXES = ("Special:", "Wikipedia:", "File:", "Portal:", "Help:", "Talk:")

# YouTube's numeric snippet.categoryId → a human bucket. Anything unknown falls
# back to the raw id string so the value is still a stable category.
_YT_CATEGORIES: dict[str, str] = {
    "1": "film", "2": "autos", "10": "music", "15": "pets", "17": "sports",
    "19": "travel", "20": "gaming", "22": "people", "23": "comedy",
    "24": "entertainment", "25": "news", "26": "howto", "27": "education",
    "28": "science", "29": "nonprofits",
}

# --- GitHub ---------------------------------------------------------------- #
# ``created:>YYYY-MM-DD`` is a SERVER-SIDE recency filter: the owner's "nothing
# older than about a month", enforced by the source instead of by a post-hoc
# gate we pay an LLM to undo. 30 days matches GENERAL_TRENDS_MAX_CONTENT_AGE_DAYS.
GITHUB_CREATED_WITHIN_DAYS = 30
GITHUB_ACCEPT = "application/vnd.github+json"

# --- Google News ----------------------------------------------------------- #
# The x_trends replacement. trends24 gave 80 cards of context-free tokens with
# an empty summary (22% of the feed, and the origin of the invented "Financial
# Transactions / Fintech" card). These two curated queries give the same
# regional intent in ENGLISH with a headline, a named publisher and a real
# <pubDate> — and ``when:7d`` is a server-side recency filter, so the feed
# cannot go stale at the source. Verified 200 (100 items each) on 2026-08-20.
GOOGLE_NEWS_UNITS: tuple[dict[str, str], ...] = (
    {
        "id": "gulf",
        "region": "AE",
        "query": '(UAE OR "Abu Dhabi") (tech OR AI OR startup) when:7d',
        "hl": "en-AE", "gl": "AE", "ceid": "AE:en",
    },
    {
        "id": "tech",
        "region": "US",
        "query": '(AI OR "artificial intelligence" OR programming OR "software developers") when:7d',
        "hl": "en-US", "gl": "US", "ceid": "US:en",
    },
)

# --- TikTok Creative Center ------------------------------------------------ #
# Browser UA + ``Accept: text/html`` — the Accept header is LOAD-BEARING.
# httpx defaults to ``*/*``, which returns HTTP 200 with the SSR payload
# SILENTLY ABSENT (40 kB of shell instead of 74 kB with data). Same IP, same
# second, only the header differs. Do not "tidy" this dict.
TIKTOK_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
TIKTOK_HEADERS: dict[str, str] = {
    "User-Agent": TIKTOK_BROWSER_UA,
    "Accept": "text/html",
    "Accept-Language": "en-US,en;q=0.9",
}
# The page's own getTrendsFilterConfig ships exactly these 27 codes. An
# unsupported code (tried: IN) does NOT error — it silently returns US data,
# which would mislabel the region on every card. Validate, never trust.
TIKTOK_REGIONS: frozenset[str] = frozenset({
    "US", "FR", "DE", "IT", "ES", "GB", "AR", "AU", "BR", "CA", "CO", "EG", "ID",
    "IL", "JP", "KR", "MY", "MX", "PH", "SA", "SG", "ZA", "TW", "TH", "TR", "AE", "VN",
})
TIKTOK_PERIODS: tuple[int, ...] = (7, 30)   # 7 and 30 only; 120 degrades to 7
_TT_BLOB_RE = re.compile(r'id="__MODERN_ROUTER_DATA__">(.*?)</script>', re.S)
_TT_LOADER_KEY = "creativeCenter/trends/(tab)/page"
# Harvested by pairing JSON industryIDs against the rendered label across a
# 54-page sweep. Unknown ids fall back to the raw id string (same convention as
# _YT_CATEGORIES above).
_TT_INDUSTRIES: dict[str, str] = {
    "10000000000": "education", "11000000000": "vehicles", "12000000000": "baby",
    "14000000000": "beauty", "17000000000": "travel", "22000000000": "apparel",
    "23000000000": "entertainment", "25000000000": "games", "27000000000": "food",
    "28000000000": "sports", "29000000000": "health",
}


class TikTokLayoutChanged(RuntimeError):
    """The Creative Center page no longer carries the SSR loader payload.

    This is a scrape of a server-rendering DETAIL, not an API contract — the
    sibling Video tab has already gone ``ssr: false``. Raising (rather than
    returning []) is the single most important line of the integration: it turns
    a silent zero-item source into a visible source failure in the run card.
    """


class KymEntryLayoutChanged(RuntimeError):
    """A Know Your Meme feed answered 200 but carried no parsable entries.

    Same reasoning as :class:`TikTokLayoutChanged`. These are RSS feeds and
    HTML scrapes of a site that has already retired four sibling endpoints on
    us (``/memes/trending``, ``newsworthy.rss``, ``news.rss``, the ``events``
    and ``subcultures`` feeds); the failure mode when the fifth goes is HTTP
    200 with nothing in it, and that is invisible for weeks unless we raise.
    """


class ShortsChartChanged(RuntimeError):
    """The YouTube Charts InnerTube reply is not the shape we parse.

    THE reason this exception exists: an undocumented RPC can keep answering
    200 while renaming a field. ``musicAnalyticsSectionRenderer`` gone,
    ``trackViews`` renamed, ``listType`` no longer a SHORTS usage chart, or
    zero rows — each of those is schema DRIFT, not an outage, and none of them
    shows up as a non-200. Alarm on the shape, not on the status code.
    """


RawItem = dict[str, Any]


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

def make_item(
    *,
    source: str,
    source_key: str,
    title: str,
    url: Optional[str] = None,
    summary: str = "",
    image_url: Optional[str] = None,
    metric_value: Optional[float] = None,
    metric_label: Optional[str] = None,
    published_at: Optional[str] = None,
    region: str = GLOBAL_REGION,
    category: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> RawItem:
    """Build a RawItem with EVERY key present (the downstream contract)."""
    return {
        "source": source,
        "source_key": source_key,
        "title": title,
        "url": url or None,
        "summary": cap_text(summary),
        "image_url": image_url or None,
        "metric_value": metric_value,
        "metric_label": metric_label,
        "published_at": published_at,
        "region": region,
        "category": category or None,
        "extra": dict(extra or {}),
    }


def collapse_ws(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def cap_text(text: Optional[str], limit: int = SUMMARY_MAX_CHARS) -> str:
    t = collapse_ws(text)
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"


def html_to_text(html: Optional[str]) -> str:
    """Strip markup to readable text with collapsed whitespace ("" on junk)."""
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
        return collapse_ws(soup.get_text(" "))
    except Exception:  # pragma: no cover — bs4 is very forgiving
        return collapse_ws(re.sub(r"<[^>]+>", " ", html))


def first_img_src(html: Optional[str]) -> Optional[str]:
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        img = soup.find("img")
        if img is not None:
            src = img.get("src")
            if isinstance(src, str) and src.strip():
                return src.strip()
    except Exception:  # pragma: no cover
        return None
    return None


_TRAFFIC_MULT = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}


def traffic_to_float(raw: Optional[str]) -> Optional[float]:
    """"2,000+" / "2000+" / "5K+" / "1.2M+" → float; None when unparsable."""
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(",", "").replace("+", "").replace(" ", "")
    if not s:
        return None
    mult = 1.0
    if s[-1] in _TRAFFIC_MULT:
        mult = _TRAFFIC_MULT[s[-1]]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def to_float(raw: Any) -> Optional[float]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return traffic_to_float(str(raw))


def _iso(dt: datetime) -> str:
    """Normalise to an ISO-8601 string in UTC (naive input is taken as UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_rfc822(raw: Optional[str]) -> Optional[str]:
    """RSS ``pubDate`` → ISO 8601 string, None when missing/malformed."""
    if not raw or not str(raw).strip():
        return None
    try:
        dt = parsedate_to_datetime(str(raw).strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return _iso(dt)


def parse_iso(raw: Optional[str]) -> Optional[str]:
    """Atom/ISO timestamps (with a trailing 'Z' allowed) → normalised ISO."""
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Python <3.11 chokes on >6 fractional digits; trim and retry once.
        m = re.match(r"^(.*?\.\d{1,6})\d*([+-]\d\d:\d\d)?$", s)
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(m.group(1) + (m.group(2) or ""))
        except ValueError:
            return None
    return _iso(dt)


def epoch_to_iso(raw: Any) -> Optional[str]:
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return _iso(datetime.fromtimestamp(ts, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _xml_root(xml_text: Optional[str]) -> Optional[ET.Element]:
    if not xml_text or not str(xml_text).strip():
        return None
    try:
        raw = xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
        return ET.fromstring(raw)
    except ET.ParseError:
        log.warning("general_trends: unparsable XML feed (%d chars)", len(xml_text))
        return None


def _text(el: Optional[ET.Element], path: str, ns: Optional[dict[str, str]] = None) -> str:
    if el is None:
        return ""
    found = el.find(path, ns) if ns else el.find(path)
    if found is None or found.text is None:
        return ""
    return collapse_ws(found.text)


# --------------------------------------------------------------------------- #
# Parsers — pure
# --------------------------------------------------------------------------- #

def parse_google_trends_rss(xml_text: str, geo: str) -> list[RawItem]:
    """Google Trends daily RSS → items. ``source_key = "{geo}:{title}"``."""
    root = _xml_root(xml_text)
    if root is None:
        return []
    ns = {"ht": _NS_HT}
    geo = (geo or "").upper() or GLOBAL_REGION
    out: list[RawItem] = []
    for item in root.iter("item"):
        try:
            title = _text(item, "title")
            if not title:
                continue
            news_items: list[dict[str, str]] = []
            for n in item.findall("ht:news_item", ns):
                nt = _text(n, "ht:news_item_title", ns)
                nu = _text(n, "ht:news_item_url", ns)
                nsrc = _text(n, "ht:news_item_source", ns)
                if nt or nu:
                    news_items.append({"title": nt, "url": nu, "source": nsrc})
            first_url = next((n["url"] for n in news_items if n["url"]), "")
            url = first_url or _text(item, "link") or None
            summary = " · ".join(n["title"] for n in news_items if n["title"])
            picture = _text(item, "ht:picture", ns) or None
            traffic_raw = _text(item, "ht:approx_traffic", ns)
            out.append(make_item(
                source="google_trends",
                source_key=f"{geo}:{title}",
                title=title,
                url=url,
                summary=summary,
                image_url=picture,
                metric_value=traffic_to_float(traffic_raw),
                metric_label="searches",
                published_at=parse_rfc822(_text(item, "pubDate")),
                region=geo,
                extra={
                    "news_items": news_items,
                    "picture_source": _text(item, "ht:picture_source", ns) or None,
                    "approx_traffic": traffic_raw or None,
                },
            ))
        except Exception as e:  # never let one bad entry sink the feed
            log.debug("google_trends: skipped malformed item: %s", e)
    return out


def parse_bluesky(payload: Any) -> list[RawItem]:
    """``app.bsky.unspecced.getTrends`` JSON → items. ``source_key`` = topic id."""
    if not isinstance(payload, dict):
        return []
    trends = payload.get("trends")
    if not isinstance(trends, list):
        return []
    out: list[RawItem] = []
    for t in trends:
        try:
            if not isinstance(t, dict):
                continue
            title = collapse_ws(str(t.get("displayName") or ""))
            if not title:
                continue
            topic = collapse_ws(str(t.get("topic") or "")) or title
            link = str(t.get("link") or "")
            if link.startswith("/"):
                url: Optional[str] = "https://bsky.app" + link
            else:
                url = link or None
            out.append(make_item(
                source="bluesky",
                source_key=topic,
                title=title,
                url=url,
                summary=str(t.get("description") or ""),
                image_url=None,
                metric_value=to_float(t.get("postCount")),
                metric_label="posts",
                published_at=parse_iso(t.get("startedAt")),
                region=GLOBAL_REGION,
                category=(collapse_ws(str(t.get("category") or "")).lower() or None),
                extra={
                    "topic": topic,
                    "status": t.get("status"),
                    "link": link or None,
                },
            ))
        except Exception as e:
            log.debug("bluesky: skipped malformed trend: %s", e)
    return out


_REDDIT_BOILERPLATE = re.compile(
    r"submitted by\s+/u/\S+|\[link\]|\[comments\]", re.IGNORECASE)


def _reddit_summary(content_html: str) -> str:
    text = html_to_text(content_html)
    text = _REDDIT_BOILERPLATE.sub(" ", text)
    return collapse_ws(text)


def _clean_sub(sub: Optional[str]) -> str:
    s = (sub or "").strip().lstrip("/")
    if s.lower().startswith("r/"):
        s = s[2:]
    return s.strip("/")


def parse_reddit_atom(xml_text: str, sub: str) -> list[RawItem]:
    """``/r/{sub}/top.rss?t=day`` Atom → items. ``source_key`` = entry id (t3_…).

    The Atom body carries NO engagement at all — no score, no comment count, no
    ratio (verified twice, independently; ``.json`` is 403 on ``www`` and
    ``old`` alike). So ``metric_value`` is None by necessity, which is exactly
    why reddit is in NON_MINTING_SOURCES.
    """
    root = _xml_root(xml_text)
    if root is None:
        return []
    ns = {"a": _NS_ATOM, "media": _NS_MEDIA}
    sub = _clean_sub(sub)
    out: list[RawItem] = []
    # /top?t=day is ORDERED BY SCORE, so while the feed hides the number the
    # ordering still encodes it. Position is therefore the closest thing to an
    # engagement signal Reddit will give us anonymously.
    for rank, entry in enumerate(root.findall("a:entry", ns), start=1):
        try:
            title = _text(entry, "a:title", ns)
            if not title:
                continue
            href = ""
            for ln in entry.findall("a:link", ns):
                h = ln.get("href") or ""
                if h and (ln.get("rel") in (None, "", "alternate")):
                    href = h
                    break
            entry_id = _text(entry, "a:id", ns)
            content_el = entry.find("a:content", ns)
            content_html = (content_el.text or "") if content_el is not None else ""
            thumb_el = entry.find("media:thumbnail", ns)
            image = (thumb_el.get("url") or "").strip() if thumb_el is not None else ""
            if not image:
                image = first_img_src(content_html) or ""
            author = _text(entry, "a:author/a:name", ns)
            out.append(make_item(
                source="reddit",
                source_key=entry_id or href or title,
                title=title,
                url=href or None,
                summary=_reddit_summary(content_html),
                image_url=image or None,
                metric_value=None,
                metric_label=None,
                published_at=parse_iso(_text(entry, "a:published", ns))
                or parse_iso(_text(entry, "a:updated", ns)),
                region=GLOBAL_REGION,
                extra={"sub": sub, "author": author or None, "rank": rank},
            ))
        except Exception as e:
            log.debug("reddit: skipped malformed entry: %s", e)
    return out


def _kym_slug(link: str) -> str:
    try:
        path = urlparse(link).path.rstrip("/")
    except ValueError:
        return ""
    return path.rsplit("/", 1)[-1] if path else ""


def parse_kym_rss(xml_text: str) -> list[RawItem]:
    """Know Your Meme newsfeed RSS → items. ``source_key`` = meme entry slug."""
    root = _xml_root(xml_text)
    if root is None:
        return []
    out: list[RawItem] = []
    # KYM publishes no counter of any kind, so its ONLY magnitude signal is
    # where an entry sits in the editors' newsfeed. Recording the position lets
    # the ranking module apply its rank curve; without it every KYM item has an
    # unmeasurable amplitude and the best-signal source in the system is ranked
    # on freshness alone.
    for rank, item in enumerate(root.iter("item"), start=1):
        try:
            title = _text(item, "title")
            if not title:
                continue
            link = _text(item, "link")
            desc_el = item.find("description")
            desc_html = (desc_el.text or "") if desc_el is not None else ""
            guid = _text(item, "guid")
            slug = _kym_slug(link) or guid or title
            out.append(make_item(
                source="kym",
                source_key=slug,
                title=title,
                url=link or None,
                summary=html_to_text(desc_html),
                image_url=first_img_src(desc_html),
                metric_value=None,
                metric_label=None,
                published_at=parse_rfc822(_text(item, "pubDate")),
                region=GLOBAL_REGION,
                extra={"guid": guid or None, "slug": slug, "rank": rank},
            ))
        except Exception as e:
            log.debug("kym: skipped malformed item: %s", e)
    return out


# --------------------------------------------------------------------------- #
# Know Your Meme — the entry database (kym_entries) and the explainers
# (kym_editorials). Registered as their OWN source ids, not folded into `kym`:
# `kym`'s measured freshness override is 72 HOURS, which is right for a
# today-only newsfeed and would gut a 30-day entry feed on arrival.
# --------------------------------------------------------------------------- #

def _kym_entry_path(link: str) -> str:
    """``…/memes/events/aocs-breakup`` -> ``events/aocs-breakup``.

    The whole path under ``/memes/`` rather than the last segment, so an entry
    filed under ``events/`` or ``subcultures/`` can never collide with a
    top-level slug of the same name.
    """
    try:
        path = urlparse(str(link or "")).path.strip("/")
    except ValueError:
        return ""
    if not path:
        return ""
    parts = path.split("/")
    if parts and parts[0] == "memes":
        parts = parts[1:]
    return "/".join(p for p in parts if p)


def kym_wave_year(text: Optional[str], *, today: Optional[date] = None) -> Optional[int]:
    """The year the entry's PROSE says the wave happened, or None.

    KYM's ``Year`` field is the format's ORIGIN (the penguin says 2008). The
    description says when it actually went round: "The story was reported in
    mid-August 2026 and inspired memes that month", "In early 2026, the clip
    became a prevalent meme". A four-digit year with a wave word inside
    ``_KYM_WAVE_WINDOW`` characters either side is taken as that year; the most
    recent such year wins, and anything beyond next year is a typo, not a wave.

    Pure and never raises — a wave year is a bonus, never a gate.
    """
    body = collapse_ws(text)
    if not body:
        return None
    try:
        limit = (today or datetime.now(timezone.utc).date()).year + 1
        best: Optional[int] = None
        for m in _KYM_YEAR_RE.finditer(body):
            window = body[max(0, m.start() - _KYM_WAVE_WINDOW): m.end() + _KYM_WAVE_WINDOW]
            if not _KYM_WAVE_WORDS.search(window):
                continue
            year = int(m.group(0))
            if year > limit:
                continue
            if best is None or year > best:
                best = year
        return best
    except Exception as e:  # noqa: BLE001 — a bonus field may never sink an item
        log.debug("kym: wave-year extraction failed: %s", e)
        return None


def kym_is_format(types: Any) -> bool:
    """Does KYM's own ``Type:`` taxonomy say there is a thing to MAKE here?

    True when at least one type slug is in :data:`KYM_FORMAT_TYPES`. An entry
    typed only ``character`` / ``viral-video`` / ``death`` documents something
    that HAPPENED, and no brand can make its own version of that.
    """
    if not isinstance(types, (list, tuple, set, frozenset)):
        return False
    return any(str(t or "").strip().lower() in KYM_FORMAT_TYPES for t in types)


def kym_is_revival(year: Any, *, today: Optional[date] = None) -> bool:
    """Is this an OLD format resurfacing rather than a new one?

    See :data:`KYM_REVIVAL_OLDER_THAN_YEARS`. ``Year >= last year`` is a new
    format; anything older is a revival — kept, flagged, never dropped. The
    penguin (Year 2008) is a revival and is the reason this returns a FLAG
    instead of a verdict.
    """
    y = to_float(year)
    if y is None:
        return False
    this_year = (today or datetime.now(timezone.utc).date()).year
    return int(y) < this_year - KYM_REVIVAL_OLDER_THAN_YEARS


def _kym_feed_items(
    xml_text: str,
    *,
    source: str,
    feed: str,
    url_prefix: str = "",
    today: Optional[date] = None,
) -> list[RawItem]:
    """Shared body of the KYM RSS parsers (all/confirmed/editorials)."""
    root = _xml_root(xml_text)
    if root is None:
        return []
    out: list[RawItem] = []
    for rank, item in enumerate(root.iter("item"), start=1):
        try:
            title = _text(item, "title")
            if not title:
                continue
            link = _text(item, "link")
            if url_prefix and link and not link.startswith(url_prefix):
                continue
            desc_el = item.find("description")
            desc_html = (desc_el.text or "") if desc_el is not None else ""
            guid = _text(item, "guid")
            summary = _KYM_TRAILING_READ_MORE.sub("", html_to_text(desc_html))
            key = _kym_entry_path(link) or guid or title
            out.append(make_item(
                source=source,
                source_key=key,
                title=title,
                url=link or None,
                summary=summary,
                image_url=first_img_src(desc_html),
                metric_value=None,
                metric_label=None,
                published_at=parse_rfc822(_text(item, "pubDate")),
                region=GLOBAL_REGION,
                extra={
                    "guid": guid or None,
                    "slug": key,
                    "feed": feed,
                    "rank": rank,
                    # Set from the prose NOW, before any enrichment: an entry we
                    # run out of budget to enrich still knows its wave.
                    "wave_year": kym_wave_year(summary, today=today),
                },
            ))
        except Exception as e:
            log.debug("%s: skipped malformed item: %s", source, e)
    # rank_total is what the ranking module's rank curve needs as its fanout;
    # without it the curve falls back to a guessed 40 and squashes a 16-row feed.
    for it in out:
        it["extra"]["rank_total"] = len(out)
    return out


def parse_kym_entry_feed(
    xml_text: str,
    *,
    feed: str = "all",
    today: Optional[date] = None,
) -> list[RawItem]:
    """``/memes/all.rss`` or ``/memes/confirmed.rss`` -> items.

    ``source_key`` is the entry path under ``/memes/``. Everything the entry
    PAGE knows (types, year, status, view counts) arrives later, in
    :func:`kym_apply_entry_facts` — an item that never gets enriched is still a
    perfectly good item, just without the taxonomy.
    """
    return _kym_feed_items(
        xml_text, source="kym_entries", feed=str(feed or "all"),
        url_prefix=KYM_ENTRY_URL_PREFIX, today=today)


def parse_kym_editorials_rss(
    xml_text: str,
    *,
    today: Optional[date] = None,
) -> list[RawItem]:
    """``/editorials.rss`` -> items. NON-MINTING by design.

    These are "What Is The 'Flobby Samuel Insect' Meme?" explainers: the best
    corroboration in the system (KYM explaining, in prose, why a thing is
    spreading) and never a trend in themselves.
    """
    return _kym_feed_items(
        xml_text, source="kym_editorials", feed="editorials", today=today)


def parse_kym_sitemap(xml_text: str) -> list[dict[str, Any]]:
    """``/sitemap.xml`` -> ``[{"url", "slug", "lastmod"}]`` for ``/memes/`` URLs.

    NOT RawItems: the sitemap has no title and no text, so an entry found only
    here becomes an item only once its page has been fetched. 29 of the 44
    ``/memes/`` URLs in the live sitemap appear in NO feed, which is the entire
    reason this unit exists.

    KYM writes the sitemap with newlines inside every tag, so the text is
    stripped rather than read as-is. Newest ``lastmod`` first; undated last.
    """
    root = _xml_root(xml_text)
    if root is None:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for el in root.iter():
        try:
            if str(el.tag).rsplit("}", 1)[-1] != "url":
                continue
            loc = ""
            lastmod = ""
            for child in el:
                tag = child.tag.rsplit("}", 1)[-1]
                value = collapse_ws(child.text)
                if tag == "loc":
                    loc = value
                elif tag == "lastmod":
                    lastmod = value
            if not loc.startswith(KYM_ENTRY_URL_PREFIX):
                continue
            slug = _kym_entry_path(loc)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            out.append({"url": loc, "slug": slug, "lastmod": parse_iso(lastmod)})
        except Exception as e:
            log.debug("kym_entries: skipped malformed sitemap url: %s", e)
    out.sort(key=lambda u: (u["lastmod"] or ""), reverse=True)
    return out


def _kym_count(soup: Any, klass: str) -> Optional[float]:
    """``<dd class='views' title='71,598 Views'>`` -> 71598.0 (None when absent)."""
    try:
        el = soup.find("dd", class_=klass)
    except Exception:
        return None
    if el is None:
        return None
    raw = el.get("title") or ""
    value = to_float(collapse_ws(raw).split(" ")[0]) if raw else None
    if value is None:
        value = to_float(collapse_ws(el.get_text(" ")))
    return value


def parse_kym_entry_page(
    html_text: str,
    *,
    url: str = "",
    today: Optional[date] = None,
) -> dict[str, Any]:
    """One KYM entry page -> the facts its definition list carries.

    Pure, never raises, ``{}`` when the page is not an entry page. The block we
    read is KYM's own metadata, verbatim::

        <a class="entry-category-badge">Meme</a>
        <dt> Status </dt><dd> Confirmed </dd>
        <dt> Type: </dt><dd> <a href="/types/dance">Dance</a>, … </dd>
        <dt> Year </dt><dd> 2008 </dd>
        <dt> Origin </dt><dd> YouTube </dd>
        <dt> Region </dt><dd> United States </dd>

    plus ``<dd class='views' title='71,598 Views'>`` and its videos / photos /
    comments siblings, and ``<meta property='article:published_time'>``.

    Returned keys mirror what goes into ``extra`` (see
    :func:`kym_apply_entry_facts`), with three DERIVED verdicts:
    ``is_format`` (KYM's Type says there is something to make), ``revival``
    (an old ``Year`` — the penguin case), ``rejected`` (``Status: Deadpool``,
    i.e. KYM's own editors judged it not a real meme; every sampled deadpool
    entry had 191-833 views).
    """
    if not html_text or not str(html_text).strip():
        return {}
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:  # pragma: no cover — bs4 is very forgiving
        return {}
    facts: dict[str, Any] = {}
    try:
        badge = soup.find("a", class_="entry-category-badge")
        # The entry BADGE (Meme | Subculture | Event | Person) — this is what
        # `category` means everywhere else in this module. A format type is a
        # different axis entirely and lives in extra["types"].
        facts["category"] = collapse_ws(badge.get_text(" ")) if badge is not None else None

        pairs: dict[str, Any] = {}
        types: list[str] = []
        for dl in soup.find_all("dl"):
            dts = dl.find_all("dt")
            if not dts:
                continue
            for dt in dts:
                label = collapse_ws(dt.get_text(" ")).rstrip(":").strip().lower()
                dd = dt.find_next_sibling("dd")
                if dd is None or not label:
                    continue
                if label == "type":
                    for a in dd.find_all("a"):
                        href = str(a.get("href") or "")
                        slug = href.split("/types/", 1)[-1].split("?")[0].strip("/") \
                            if "/types/" in href else ""
                        slug = slug or collapse_ws(a.get_text(" ")).lower().replace(" ", "-")
                        if slug and slug not in types:
                            types.append(slug)
                    if not types:
                        for chunk in collapse_ws(dd.get_text(" ")).split(","):
                            slug = chunk.strip().lower().replace(" ", "-")
                            if slug and slug not in types:
                                types.append(slug)
                else:
                    pairs.setdefault(label, collapse_ws(dd.get_text(" ")) or None)

        facts["types"] = types
        facts["status"] = pairs.get("status")
        facts["origin"] = pairs.get("origin")
        facts["region"] = pairs.get("region")
        year = to_float((pairs.get("year") or "").strip()) if pairs.get("year") else None
        facts["year"] = int(year) if year is not None else None

        facts["views"] = _kym_count(soup, "views")
        facts["videos"] = _kym_count(soup, "videos")
        facts["photos"] = _kym_count(soup, "photos")
        facts["comments"] = _kym_count(soup, "comments")

        pub = soup.find("meta", attrs={"property": "article:published_time"})
        facts["published_at"] = parse_iso(pub.get("content")) if pub is not None else None

        h1 = soup.find("h1", class_="entry-title")
        title = collapse_ws(h1.get_text(" ")) if h1 is not None else ""
        if not title:
            og_title = soup.find("meta", attrs={"property": "og:title"})
            title = collapse_ws(og_title.get("content")) if og_title is not None else ""
            title = re.sub(r"\s*\|\s*Know Your Meme\s*$", "", title)
        facts["title"] = title or None

        og_image = soup.find("meta", attrs={"property": "og:image"})
        facts["image_url"] = (collapse_ws(og_image.get("content")) or None) \
            if og_image is not None else None

        facts["summary"] = _kym_body_text(soup)

        facts["is_format"] = kym_is_format(types)
        facts["revival"] = kym_is_revival(facts["year"], today=today)
        facts["rejected"] = str(facts["status"] or "").strip().lower() == "deadpool"
        facts["wave_year"] = kym_wave_year(facts["summary"], today=today)
        facts["url"] = collapse_ws(url) or None
    except Exception as e:  # noqa: BLE001 — a bad page degrades, never raises
        log.debug("kym_entries: entry page %r only partly parsed: %s", url, e)
    # A page that carries NONE of the entry metadata is not an entry page (a
    # 404 body, an interstitial, a re-skin). Returning the all-empty dict would
    # be worse than returning nothing: it would stamp `is_format: False` onto
    # the item, i.e. claim KYM judged it — when KYM said nothing at all.
    if not any(facts.get(k) for k in ("status", "types", "year", "views", "title")):
        return {}
    return facts


def _kym_body_text(soup: Any) -> str:
    """The entry's own prose, without the table-of-contents furniture."""
    try:
        body = soup.find(class_="bodycopy")
        if body is None:
            meta = soup.find("meta", attrs={"property": "og:description"})
            return collapse_ws(meta.get("content")) if meta is not None else ""
        for junk in body.select("table.navigation, .mobile-only, script, style"):
            junk.decompose()
        text = " ".join(collapse_ws(p.get_text(" ")) for p in body.find_all("p", limit=6))
        return collapse_ws(text) or collapse_ws(body.get_text(" "))
    except Exception:  # pragma: no cover
        return ""


def _kym_entry_summary(base: str, facts: dict[str, Any]) -> str:
    """Fold the taxonomy into the prose so the SCORER can read it too."""
    bits: list[str] = []
    if facts.get("category"):
        bits.append(str(facts["category"]))
    if facts.get("status"):
        bits.append(f"status {facts['status']}")
    types = facts.get("types") or []
    if types:
        bits.append("type " + ", ".join(str(t).replace("-", " ") for t in types[:6]))
    if facts.get("origin"):
        bits.append(f"origin {facts['origin']}")
    if facts.get("year"):
        bits.append(str(facts["year"]))
    counts = []
    for key, word in (("views", "views"), ("videos", "videos"), ("photos", "images")):
        value = to_float(facts.get(key))
        if value is not None:
            counts.append(f"{int(value):,} {word}")
    if counts:
        bits.append(", ".join(counts) + " on Know Your Meme")
    tail = " · ".join(bits)
    if not tail:
        return base
    return f"{base} — {tail}" if base else tail


def kym_apply_entry_facts(item: RawItem, facts: dict[str, Any]) -> RawItem:
    """Merge one entry page's facts into a feed item, IN PLACE.

    Everything the pipeline needs to set ``trend_kind`` FROM THE SOURCE rather
    than pay a model to guess it goes into ``extra``: ``types``, ``year``,
    ``wave_year``, ``origin``, ``region``, ``status``, ``views``, ``videos``,
    ``photos``, ``revival``, ``rejected``, ``is_format``.

    ``category`` is set to the entry BADGE (Meme / Subculture / Event /
    Person) and never to a format type — those are two different axes.

    Never raises: enrichment is a bonus, and a half-parsed page must leave the
    feed item exactly as good as it was.
    """
    if not isinstance(item, dict):
        return item
    if not isinstance(facts, dict) or not facts:
        return item
    # Same reasoning as the tail of parse_kym_entry_page: a facts dict with
    # nothing in it must leave the item untouched rather than stamp it
    # `enriched` and imply KYM had an opinion.
    if not any(facts.get(k) for k in
               ("status", "types", "year", "views", "category", "published_at")):
        return item
    try:
        extra = item.setdefault("extra", {})
        for key in ("types", "year", "origin", "region", "status", "views",
                    "videos", "photos", "comments", "revival", "rejected",
                    "is_format"):
            if key in facts:
                extra[key] = facts[key]
        if facts.get("wave_year") and not extra.get("wave_year"):
            extra["wave_year"] = facts["wave_year"]
        extra["enriched"] = True
        if facts.get("category"):
            item["category"] = facts["category"]
        if facts.get("image_url") and not item.get("image_url"):
            item["image_url"] = facts["image_url"]
        if facts.get("published_at") and not item.get("published_at"):
            item["published_at"] = facts["published_at"]
        views = to_float(facts.get("views"))
        if views is not None:
            # A real, externally-counted magnitude — the first KYM has ever
            # given us. It is KYM PAGE traffic, not the meme's reach, so it is
            # labelled as such and only ever compared against other KYM rows
            # (the ranking module scores every source against its own median).
            item["metric_value"] = views
            item["metric_label"] = "KYM views"
        item["summary"] = cap_text(_kym_entry_summary(
            _KYM_TRAILING_READ_MORE.sub("", str(item.get("summary") or "")), facts))
    except Exception as e:  # noqa: BLE001
        log.debug("kym_entries: could not apply entry facts: %s", e)
    return item


def kym_item_from_entry_page(
    facts: dict[str, Any],
    *,
    url: str,
    slug: str = "",
    lastmod: Optional[str] = None,
    rank: int = 1,
    rank_total: Optional[int] = None,
) -> Optional[RawItem]:
    """Build a RawItem for an entry that appears in NO feed (sitemap-only).

    None when the page gave us no title — a nameless entry is not an item.
    """
    if not isinstance(facts, dict) or not facts.get("title"):
        return None
    key = slug or _kym_entry_path(url)
    item = make_item(
        source="kym_entries",
        source_key=key or url,
        title=str(facts["title"]),
        url=url or None,
        summary="",
        image_url=facts.get("image_url"),
        metric_value=None,
        metric_label=None,
        published_at=facts.get("published_at") or lastmod,
        region=GLOBAL_REGION,
        extra={
            "guid": None,
            "slug": key,
            "feed": "sitemap",
            "rank": int(rank),
            "rank_total": int(rank_total) if rank_total else None,
            "lastmod": lastmod,
            "wave_year": facts.get("wave_year"),
        },
    )
    # cap_text here as well as inside kym_apply_entry_facts: a page that gave
    # us a title and nothing else makes that call a no-op, and the RawItem
    # contract (summary <= SUMMARY_MAX_CHARS) holds either way.
    item["summary"] = cap_text(facts.get("summary"))
    return kym_apply_entry_facts(item, facts)


# --------------------------------------------------------------------------- #
# YouTube Shorts — the songs creators USED most (charts.youtube.com InnerTube)
# --------------------------------------------------------------------------- #

def youtube_shorts_chart(payload: Any) -> Optional[dict[str, Any]]:
    """The chart block out of an InnerTube reply, or None when the shape moved.

    Pure and non-raising (the module contract). The FETCHER turns a None into a
    :class:`ShortsChartChanged` so schema drift becomes a visible source
    failure instead of a source that quietly returns nothing for a month.
    """
    if not isinstance(payload, dict):
        return None
    try:
        sections = ((payload.get("contents") or {})
                    .get("sectionListRenderer") or {}).get("contents")
        if not isinstance(sections, list):
            return None
        for section in sections:
            if not isinstance(section, dict):
                continue
            content = ((section.get("musicAnalyticsSectionRenderer") or {})
                       .get("content") or {})
            track_types = content.get("trackTypes")
            if not isinstance(track_types, list) or not track_types:
                continue
            first = track_types[0]
            if isinstance(first, dict) and isinstance(first.get("trackViews"), list):
                return first
    except Exception as e:  # noqa: BLE001
        log.debug("youtube_shorts: chart extraction failed: %s", e)
    return None


def parse_youtube_shorts(
    payload: Any,
    region: str,
    period: str = "WEEKLY",
) -> list[RawItem]:
    """YouTube Charts "songs used most by creators on Shorts" -> items.

    ``metric_value`` is deliberately **None**. The chart hands over a POSITION,
    and a position is not a headcount — turning "#3 of 50" into a number would
    be inventing an audience size, and the ranking module would then compare
    that invention against the source's own median as though it were real. What
    it actually wants is in ``extra``: ``rank`` and ``rank_total`` drive its
    rank curve, ``prev_rank`` and ``growth_ratio`` its growth term. Those are
    YouTube's numbers, not ours.

    ``growth_ratio`` is ``1 + percentViewsChange``, i.e. YouTube's own
    week-over-week change in the track's Shorts usage — the only externally
    measured growth series in this module besides TikTok's popularity curve.
    """
    chart = youtube_shorts_chart(payload)
    if chart is None:
        return []
    region = (region or "").upper() or GLOBAL_REGION
    period = str(period or "WEEKLY").upper()
    rows = [r for r in (chart.get("trackViews") or []) if isinstance(r, dict)]
    end_date = collapse_ws(str(chart.get("endDate") or ""))
    published = f"{end_date}T00:00:00+00:00" if re.fullmatch(r"\d{4}-\d\d-\d\d", end_date) else None
    list_type = collapse_ws(str(chart.get("listType") or "")) or None
    total = len(rows)
    period_label = "Weekly" if period == "WEEKLY" else "Daily"
    out: list[RawItem] = []
    for index, row in enumerate(rows, start=1):
        try:
            name = collapse_ws(str(row.get("name") or ""))
            video_id = collapse_ws(str(row.get("encryptedVideoId") or ""))
            if not name or not video_id:
                continue
            artists = [
                collapse_ws(str(a.get("name") or ""))
                for a in (row.get("artists") or []) if isinstance(a, dict)
            ]
            artists = [a for a in artists if a]
            meta = row.get("chartEntryMetadata")
            meta = meta if isinstance(meta, dict) else {}
            rank = to_float(meta.get("currentPosition")) or float(index)
            prev_rank = to_float(meta.get("previousPosition"))
            pct = to_float(meta.get("percentViewsChange"))
            periods_on_chart = to_float(meta.get("periodsOnChart"))
            image = None
            thumbs = ((row.get("thumbnail") or {}).get("thumbnails")
                      if isinstance(row.get("thumbnail"), dict) else None)
            if isinstance(thumbs, list):
                for t in thumbs:
                    if isinstance(t, dict) and t.get("url"):
                        image = str(t["url"])
                        break
            growth = (1.0 + pct) if pct is not None and (1.0 + pct) > 0 else None

            summary = (
                f"#{int(rank)} of {total} on YouTube's {period_label} Top Songs on Shorts "
                f"chart in {region} — the songs creators used most in their own Shorts."
            )
            if prev_rank is not None:
                move = "Up from" if prev_rank > rank else ("Down from" if prev_rank < rank else "Holding at")
                summary += f" {move} #{int(prev_rank)}."
            else:
                # Absent previousPosition means "not on the PREVIOUS chart" —
                # which is not the same as new to the chart at all (a track can
                # show periodsOnChart 17 and still have dropped out last week).
                summary += " Not on the previous chart."
            if periods_on_chart is not None:
                unit = "weeks" if period == "WEEKLY" else "days"
                summary += f" {int(periods_on_chart)} {unit} on chart."
            if pct is not None:
                summary += f" Usage {pct * 100:+.0f}% vs the period before."

            out.append(make_item(
                source="youtube_shorts",
                source_key=f"ytshorts:{video_id}:{region}",
                title=(f"{name} — {', '.join(artists)}" if artists else name),
                url=YOUTUBE_SOUND_URL.format(video_id=quote(video_id, safe="")),
                summary=summary,
                image_url=image,
                # See the docstring: an ordinal is not a headcount.
                metric_value=None,
                metric_label=None,
                published_at=published,
                region=region,
                category="music",
                extra={
                    "rank": rank,
                    "rank_total": float(total),
                    "prev_rank": prev_rank,
                    "previous_position": prev_rank,
                    "percent_change": pct,
                    "growth_ratio": growth,
                    "periods_on_chart": periods_on_chart,
                    "country": region,
                    "period": period,
                    "video_id": video_id,
                    "artists": artists[:5],
                    "track": name,
                    "chart_end_date": end_date or None,
                    "list_type": list_type,
                },
            ))
        except Exception as e:
            log.debug("youtube_shorts: skipped malformed row: %s", e)
    return out


def _wiki_articles(payload: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    """→ (articles, "YYYY-MM-DD" | None) from a Wikimedia top-pageviews payload."""
    if not isinstance(payload, dict):
        return [], None
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return [], None
    first = items[0]
    arts = first.get("articles")
    day: Optional[str] = None
    try:
        y, m, d = str(first.get("year")), str(first.get("month")), str(first.get("day"))
        if y.isdigit() and m.isdigit() and d.isdigit():
            day = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except Exception:
        day = None
    return (arts if isinstance(arts, list) else []), day


def wiki_is_housekeeping(article: str) -> bool:
    if not article or article in _WIKI_SKIP_EXACT:
        return True
    return article.startswith(_WIKI_SKIP_PREFIXES)


# Without a comparison day nothing can be measured, so fall back to "near the
# very top" instead of emitting the whole 1000-row list as trends.
_WIKI_NO_COMPARISON_MAX_RANK = 25


def wiki_is_rising(
    views: Optional[float],
    prev: Optional[float],
    *,
    baseline_high: float = 25_000,
    min_ratio: float = 1.5,
    min_ratio_evergreen: float = 3.0,
    min_rise: float = 20_000,
) -> bool:
    """Is this article's traffic a SPIKE, or just a big page having a Tuesday?

    ``views > yesterday`` kept 40 of 40 rows, which is why the evergreen page
    "ChatGPT" (+1.71x, +23,476 views) ranked #2 in the live feed. A flat
    threshold cannot work either: ChatGPT's raw rise beats any naive bar, while
    a genuine spike like "Nancy Mace" (+5.99x) starts from a tiny baseline. So
    the bar scales with the page's OWN baseline — the house pattern used
    everywhere else in this repo (``trend_finder`` ranks each account against
    its own median, not against a global one).

    Measured over the live top-40: keeps 27, and drops exactly ChatGPT (1.71x),
    Erection (1.08x), Ejaculation (1.71x), Deaths in 2026 (1.37x) and List of
    highest-grossing films (1.00x) — so the owner's "no sexual content" ask
    falls out of the rise gate for free, with no word list involved. Keeps
    Google (4.25x), Nancy Mace (5.99x) and Hayden Panettiere (new to top-1000).
    """
    if views is None:
        return False
    if prev is None:      # no comparison day — the caller falls back on rank
        return True
    if prev <= 0:         # absent yesterday ⇒ new to the top-1000 ⇒ rising
        return True
    needed = min_ratio_evergreen if prev >= baseline_high else min_ratio
    return views >= prev * needed and (views - prev) >= min_rise


def parse_wikipedia_top(today_payload: Any, yesterday_payload: Any) -> list[RawItem]:
    """Most-viewed EN articles: keep only those genuinely RISING (see
    ``wiki_is_rising``) vs the previous day.

    ``yesterday_payload`` may be None/empty (the previous day wasn't available):
    then nothing can be compared, so non-housekeeping articles inside the top
    ``_WIKI_NO_COMPARISON_MAX_RANK`` are kept and ``views_delta`` is None. An
    article missing from yesterday's list is new to the top-1000 and counts as
    rising from 0.
    """
    today_arts, day = _wiki_articles(today_payload)
    if not today_arts:
        return []
    y_arts, _ = _wiki_articles(yesterday_payload)
    have_yesterday = bool(y_arts)
    y_views: dict[str, float] = {}
    for a in y_arts:
        if isinstance(a, dict) and a.get("article"):
            v = to_float(a.get("views"))
            if v is not None:
                y_views[str(a["article"])] = v
    published = f"{day}T00:00:00+00:00" if day else None
    out: list[RawItem] = []
    for a in today_arts:
        try:
            if not isinstance(a, dict):
                continue
            article = str(a.get("article") or "")
            if wiki_is_housekeeping(article):
                continue
            views = to_float(a.get("views"))
            if views is None:
                continue
            prev = y_views.get(article, 0.0) if have_yesterday else None
            rank = a.get("rank")
            if have_yesterday:
                if not wiki_is_rising(views, prev):
                    continue
            else:
                rank_num = to_float(rank)
                if rank_num is not None and rank_num > _WIKI_NO_COMPARISON_MAX_RANK:
                    continue
            delta = (views - (prev or 0.0)) if have_yesterday else None
            title = collapse_ws(article.replace("_", " "))
            if not title:
                continue
            summary = f"#{rank} most-viewed on English Wikipedia · {int(views):,} views"
            if delta is not None:
                summary += f" (+{int(delta):,} vs the day before)"
            out.append(make_item(
                source="wikipedia",
                source_key=article,
                title=title,
                url=f"https://en.wikipedia.org/wiki/{quote(article, safe='():,_-')}",
                summary=summary,
                image_url=None,
                metric_value=views,
                metric_label="views",
                published_at=published,
                region=GLOBAL_REGION,
                extra={
                    "rank": rank,
                    "views_today": views,
                    "views_yesterday": prev,
                    "views_delta": delta,
                    "day": day,
                },
            ))
        except Exception as e:
            log.debug("wikipedia: skipped malformed article: %s", e)
    return out


def parse_youtube(payload: Any, region: str) -> list[RawItem]:
    """YouTube ``videos?chart=mostPopular`` JSON → items. ``source_key`` = video id."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    region = (region or "").upper() or GLOBAL_REGION
    out: list[RawItem] = []
    for v in items:
        try:
            if not isinstance(v, dict):
                continue
            vid = str(v.get("id") or "").strip()
            snip = v.get("snippet") or {}
            stats = v.get("statistics") or {}
            if not isinstance(snip, dict):
                continue
            if not isinstance(stats, dict):
                stats = {}
            title = collapse_ws(str(snip.get("title") or ""))
            if not title or not vid:
                continue
            thumbs = snip.get("thumbnails") or {}
            image = None
            if isinstance(thumbs, dict):
                for key in ("high", "medium", "standard", "default", "maxres"):
                    t = thumbs.get(key)
                    if isinstance(t, dict) and t.get("url"):
                        image = str(t["url"])
                        break
            cat_id = str(snip.get("categoryId") or "")
            category = _YT_CATEGORIES.get(cat_id) or (cat_id or None)
            tags = snip.get("tags")
            out.append(make_item(
                source="youtube",
                source_key=vid,
                title=title,
                url=f"https://www.youtube.com/watch?v={vid}",
                summary=str(snip.get("description") or ""),
                image_url=image,
                metric_value=to_float(stats.get("viewCount")),
                metric_label="views",
                published_at=parse_iso(snip.get("publishedAt")),
                region=region,
                category=category,
                extra={
                    "channel": snip.get("channelTitle"),
                    "channel_id": snip.get("channelId"),
                    "category_id": cat_id or None,
                    "tags": [str(t) for t in tags[:10]] if isinstance(tags, list) else [],
                    "like_count": to_float(stats.get("likeCount")),
                    "comment_count": to_float(stats.get("commentCount")),
                },
            ))
        except Exception as e:
            log.debug("youtube: skipped malformed video: %s", e)
    return out


def parse_trends24(html_text: str, geo: str) -> list[RawItem]:
    """trends24.in country page → items from the FIRST (most recent) hourly list.

    ``source_key = "{geo}:{trend text}"``. Duplicate trend texts inside the
    list (the mirror sometimes repeats one) are collapsed to the first.
    """
    if not html_text or not str(html_text).strip():
        return []
    geo = (geo or "").upper() or GLOBAL_REGION
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception:
        return []
    ol = soup.find("ol", class_="trend-card__list")
    if ol is None:
        return []
    snapshot_at: Optional[str] = None
    try:
        container = ol.find_parent(class_="list-container") or ol.parent
        h3 = container.find("h3") if container is not None else None
        if h3 is not None:
            snapshot_at = epoch_to_iso(h3.get("data-timestamp"))
            if snapshot_at is None:
                snapshot_at = parse_rfc822(h3.get_text(" ", strip=True)) or None
    except Exception:
        snapshot_at = None
    out: list[RawItem] = []
    seen: set[str] = set()
    rank = 0
    for li in ol.find_all("li"):
        try:
            a = li.find("a", class_="trend-link") or li.find("a")
            if a is None:
                continue
            text = collapse_ws(a.get_text(" "))
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            href = a.get("href")
            url = href.strip() if isinstance(href, str) and href.strip() else (
                "https://x.com/search?q=" + quote(text))
            count_el = li.find("span", class_="tweet-count")
            count = None
            if count_el is not None:
                count = to_float(count_el.get("data-count"))
                if count is None:
                    count = to_float(
                        collapse_ws(count_el.get_text(" ")).lower().replace("tweets", "").strip())
            out.append(make_item(
                source="x_trends",
                source_key=f"{geo}:{text}",
                title=text,
                url=url,
                summary="",
                image_url=None,
                metric_value=count,
                metric_label="tweets",
                published_at=snapshot_at,
                region=geo,
                extra={"rank": rank, "snapshot_at": snapshot_at},
            ))
        except Exception as e:
            log.debug("x_trends: skipped malformed li: %s", e)
    return out


def _domain(url: Optional[str]) -> Optional[str]:
    try:
        host = urlparse(str(url or "")).netloc.lower()
    except ValueError:
        return None
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def parse_hacker_news(payload: Any) -> list[RawItem]:
    """HN front page via the Algolia index → items. ``source_key`` = objectID.

    Every hit carries ``points``, ``num_comments`` AND an ISO ``created_at`` —
    the first free source in this module where the ranking module's amplitude
    term has real, absolute inputs rather than a rank we invented a number for.
    """
    if not isinstance(payload, dict):
        return []
    hits = payload.get("hits")
    if not isinstance(hits, list):
        return []
    out: list[RawItem] = []
    for h in hits:
        try:
            if not isinstance(h, dict):
                continue
            title = collapse_ws(str(h.get("title") or ""))
            obj_id = collapse_ws(str(h.get("objectID") or ""))
            if not title or not obj_id:
                continue
            hn_url = HN_ITEM_URL.format(id=obj_id)
            link = collapse_ws(str(h.get("url") or "")) or None
            points = to_float(h.get("points"))
            comments = to_float(h.get("num_comments"))
            bits: list[str] = []
            body = html_to_text(str(h.get("story_text") or ""))
            if body:
                bits.append(body)
            counted = f"{int(points or 0):,} points, {int(comments or 0):,} comments on Hacker News"
            host = _domain(link)
            bits.append(f"{counted} · {host}" if host else counted)
            out.append(make_item(
                source="hacker_news",
                source_key=obj_id,
                title=title,
                url=link or hn_url,
                summary=" — ".join(bits),
                image_url=None,
                metric_value=points,
                metric_label="points",
                published_at=parse_iso(h.get("created_at")),
                region=GLOBAL_REGION,
                extra={
                    "comments": comments,
                    "author": h.get("author") or None,
                    "hn_url": hn_url,
                    "domain": host,
                },
            ))
        except Exception as e:
            log.debug("hacker_news: skipped malformed hit: %s", e)
    return out


def parse_lemmy(payload: Any) -> list[RawItem]:
    """lemmy.world ``/api/v3/post/list?sort=TopDay`` → items. ``source_key`` = post id.

    The Reddit substitute that DOES hand over the score and comment count
    Reddit's Atom refuses to give. It also ships an explicit ``nsfw`` bool on
    both the post and its community — a free, pre-model content gate, which is
    why sexual content is filtered here rather than paid for downstream.
    """
    if not isinstance(payload, dict):
        return []
    posts = payload.get("posts")
    if not isinstance(posts, list):
        return []
    out: list[RawItem] = []
    for p in posts:
        try:
            if not isinstance(p, dict):
                continue
            post = p.get("post")
            if not isinstance(post, dict):
                continue
            community = p.get("community") if isinstance(p.get("community"), dict) else {}
            counts = p.get("counts") if isinstance(p.get("counts"), dict) else {}
            creator = p.get("creator") if isinstance(p.get("creator"), dict) else {}
            if post.get("deleted") or post.get("removed"):
                continue
            if post.get("nsfw") or community.get("nsfw"):
                continue          # the source labels it; never pay a model to re-discover it
            title = collapse_ws(str(post.get("name") or ""))
            pid = collapse_ws(str(post.get("id") or ""))
            if not title or not pid:
                continue
            body = collapse_ws(str(post.get("body") or "")) or collapse_ws(str(post.get("alt_text") or ""))
            comm = collapse_ws(str(community.get("name") or "")) or None
            score = to_float(counts.get("score"))
            head = f"c/{comm} · {int(score or 0):,} votes, {int(to_float(counts.get('comments')) or 0):,} comments" \
                if comm else f"{int(score or 0):,} votes on Lemmy"
            out.append(make_item(
                source="lemmy",
                source_key=pid,
                title=title,
                url=LEMMY_POST_URL.format(id=pid),
                summary=f"{head} — {body}" if body else head,
                image_url=collapse_ws(str(post.get("thumbnail_url") or "")) or None,
                metric_value=score,
                metric_label="votes",
                published_at=parse_iso(post.get("published") or counts.get("published")),
                region=GLOBAL_REGION,
                category=comm,
                extra={
                    "community": comm,
                    "community_title": collapse_ws(str(community.get("title") or "")) or None,
                    "comments": to_float(counts.get("comments")),
                    "upvotes": to_float(counts.get("upvotes")),
                    "downvotes": to_float(counts.get("downvotes")),
                    "creator": collapse_ws(str(creator.get("name") or "")) or None,
                    "link": collapse_ws(str(post.get("url") or "")) or None,
                    "ap_id": collapse_ws(str(post.get("ap_id") or "")) or None,
                },
            ))
        except Exception as e:
            log.debug("lemmy: skipped malformed post: %s", e)
    return out


def parse_github(payload: Any) -> list[RawItem]:
    """GitHub repo search → items. ``source_key`` = ``owner/name``.

    The query carries ``created:>DATE``, so recency is enforced BY THE SOURCE:
    nothing older than the window can appear at all. Stars are an absolute,
    externally-measured count.
    """
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    out: list[RawItem] = []
    for repo in items:
        try:
            if not isinstance(repo, dict):
                continue
            full = collapse_ws(str(repo.get("full_name") or ""))
            if not full:
                continue
            stars = to_float(repo.get("stargazers_count"))
            desc = collapse_ws(str(repo.get("description") or ""))
            lang = collapse_ws(str(repo.get("language") or "")) or None
            head = f"{int(stars or 0):,} stars since it was created"
            if lang:
                head += f" · {lang}"
            topics = repo.get("topics")
            owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
            out.append(make_item(
                source="github",
                source_key=full,
                title=full,
                url=collapse_ws(str(repo.get("html_url") or "")) or f"https://github.com/{full}",
                summary=f"{desc} — {head}" if desc else head,
                image_url=None,
                metric_value=stars,
                metric_label="stars",
                published_at=parse_iso(repo.get("created_at")),
                region=GLOBAL_REGION,
                category=(lang.lower() if lang else None),
                extra={
                    "description": desc or None,
                    "language": lang,
                    "forks": to_float(repo.get("forks_count")),
                    "watchers": to_float(repo.get("watchers_count")),
                    "open_issues": to_float(repo.get("open_issues_count")),
                    "topics": [str(t) for t in topics[:10]] if isinstance(topics, list) else [],
                    "owner": collapse_ws(str(owner.get("login") or "")) or None,
                },
            ))
        except Exception as e:
            log.debug("github: skipped malformed repo: %s", e)
    return out


def parse_google_news_rss(xml_text: str, geo: str, query: str = "") -> list[RawItem]:
    """Google News search RSS → items. ``source_key`` = the item's ``guid``.

    Google appends " - Publisher" to every ``<title>`` and repeats the same
    string inside ``<description>``; the publisher already has its own
    ``<source>`` element, so the suffix is stripped off the title (it would
    otherwise poison every clustering slug) and named in the summary instead.
    """
    root = _xml_root(xml_text)
    if root is None:
        return []
    geo = (geo or "").upper() or GLOBAL_REGION
    out: list[RawItem] = []
    # Google News returns the result list in relevance order and ships no
    # counter, so position is the only magnitude signal available (see the
    # same note on parse_kym_rss).
    for rank, item in enumerate(root.iter("item"), start=1):
        try:
            raw_title = _text(item, "title")
            if not raw_title:
                continue
            src_el = item.find("source")
            publisher = collapse_ws(src_el.text) if src_el is not None and src_el.text else ""
            publisher_url = (src_el.get("url") or "").strip() if src_el is not None else ""
            title = raw_title
            if publisher and title.endswith(f" - {publisher}"):
                title = title[: -(len(publisher) + 3)].rstrip()
            if not title:
                title = raw_title
            link = _text(item, "link")
            guid = _text(item, "guid")
            out.append(make_item(
                source="google_news",
                source_key=guid or link or f"{geo}:{title}",
                title=title,
                url=link or None,
                summary=f"{publisher}: {title}" if publisher else html_to_text(
                    (item.find("description").text or "") if item.find("description") is not None else ""),
                image_url=None,
                metric_value=None,
                metric_label=None,
                published_at=parse_rfc822(_text(item, "pubDate")),
                region=geo,
                extra={
                    "news_source": publisher or None,
                    "news_source_url": publisher_url or None,
                    "guid": guid or None,
                    "query": query or None,
                    "rank": rank,
                },
            ))
        except Exception as e:
            log.debug("google_news: skipped malformed item: %s", e)
    return out


def tiktok_loader_payload(html_text: Optional[str]) -> Optional[dict[str, Any]]:
    """The Creative Center SSR blob for the trends tab, or None when it is gone.

    Pure and non-raising (the module contract). The FETCHER turns a None into a
    TikTokLayoutChanged so a vanished payload becomes a visible source failure.
    """
    if not html_text:
        return None
    m = _TT_BLOB_RE.search(html_text)
    if not m:
        return None
    try:
        blob = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    if not isinstance(blob, dict):
        return None
    page = (blob.get("loaderData") or {}).get(_TT_LOADER_KEY)
    return page if isinstance(page, dict) else None


def _tiktok_rows(page: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    queries = (page.get("dehydratedState") or {}).get("queries")
    for q in queries if isinstance(queries, list) else []:
        if not isinstance(q, dict):
            continue
        data = (q.get("state") or {}).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            continue
        for pg in data["pages"]:
            if isinstance(pg, dict) and isinstance(pg.get("data"), list):
                rows.extend(r for r in pg["data"] if isinstance(r, dict))
    return rows


def parse_tiktok_hashtags(html_text: str, region: str, period: int = 7) -> list[RawItem]:
    """TikTok Creative Center trending hashtags → items.

    ``image_url`` is deliberately None: the only picture on offer is
    ``topCreators[].avatarURL``, which is signed with ``x-expires`` about 48h
    out and would 403 on the card a day after the run.

    ``extra["curve"]`` is ``popularityCurve`` — 7 (or 30) DAILY points, last
    point D-1, measured by TikTok. It is the only externally-measured growth
    series any free source in this module hands over, and the only thing that
    gives the ranking module a real growth term on run #1.
    """
    page = tiktok_loader_payload(html_text)
    if page is None:
        return []
    region = (region or "").upper() or GLOBAL_REGION
    try:
        period_days = int(period)
    except (TypeError, ValueError):
        period_days = 7
    out: list[RawItem] = []
    for row in _tiktok_rows(page):
        try:
            name = collapse_ws(str(row.get("hashtagName") or "")).lstrip("#")
            tag_id = collapse_ws(str(row.get("hashtagID") or ""))
            if not name:
                continue
            views = to_float(row.get("vv"))
            posts = to_float(row.get("publishCnt"))
            rank = to_float(row.get("rankIndex"))
            curve: list[dict[str, Any]] = []
            for pt in (row.get("popularityCurve") or []):
                if not isinstance(pt, dict):
                    continue
                ts = to_float(pt.get("timestamp"))
                val = to_float(pt.get("value"))
                if ts is None or val is None:
                    continue
                curve.append({"ts": int(ts), "at": epoch_to_iso(ts), "value": val})
            curve.sort(key=lambda p: p["ts"])
            # g in the ranking module's growth term: today's point over the one
            # before it. Values are TikTok's own 0-100 normalisation, so the
            # ratio is scale-free and directly comparable across hashtags.
            growth = None
            if len(curve) >= 2 and curve[-2]["value"] > 0:
                growth = curve[-1]["value"] / curve[-2]["value"]
            creators = [
                collapse_ws(str(c.get("handleName") or ""))
                for c in (row.get("topCreators") or []) if isinstance(c, dict)
            ]
            creators = [c for c in creators if c][:3]
            # A tag carried by three mega-accounts is a campaign; a tag carried
            # by small accounts is a grassroots format. Measured: AE's
            # #happyindependenceday is led by a 2.2M-follower account.
            follower_counts = [
                to_float(c.get("followedCnt"))
                for c in (row.get("topCreators") or []) if isinstance(c, dict)
            ]
            follower_counts = [f for f in follower_counts if f is not None][:3]
            industries = row.get("industryIDs")
            ind_id = str(industries[0]) if isinstance(industries, list) and industries else ""

            # VIEWS PER POST — the participation test, and the most useful single
            # number this feed gives us. A trend is a format people are MAKING,
            # and the ratio separates that from a story people merely WATCH:
            #   #marvelrivalssummerfest2   180k posts / 35M views =    196 v/post
            #   #haydenpanettiere           37k posts / 396M views = 10,725 v/post
            # A 55x separation between "everyone is making one" and "a celebrity
            # died", on two rows that look identical if you rank on views.
            views_per_post = None
            if posts and views and posts > 0:
                views_per_post = views / posts

            summary = (
                f"TikTok hashtag #{name} — {int(posts or 0):,} posts, {int(views or 0):,} views "
                f"in the last {period_days} days in {region}."
            )
            if views_per_post is not None:
                summary += f" {views_per_post:,.0f} views per post."
            if creators:
                summary += " Top creators: " + ", ".join("@" + c for c in creators) + "."
            out.append(make_item(
                source="tiktok",
                source_key=f"tiktok:{tag_id or name}:{region}:{period_days}",
                title=f"#{name}",
                url=f"https://www.tiktok.com/tag/{quote(name, safe='')}",
                summary=summary,
                image_url=None,          # avatarURL is signed and expires — see docstring
                # POSTS, not views. A trend is a format PEOPLE ARE MAKING, so
                # the headline number must be how many of them made one:
                # 24,297 people shot a #spidermanbrandnewday video is evidence
                # of a format; 57M views is evidence that a few videos did well.
                # Views stay in `extra` and in the summary line.
                metric_value=(posts if posts else views),
                metric_label=("posts" if posts else "views"),
                published_at=(curve[-1]["at"] if curve else None),
                region=region,
                category=(_TT_INDUSTRIES.get(ind_id) or (ind_id or None)),
                extra={
                    "hashtag": name,
                    "hashtag_id": tag_id or None,
                    "posts": posts,
                    "views": views,
                    # See the note above: low = people are MAKING it, high =
                    # people are WATCHING it.
                    "views_per_post": views_per_post,
                    "creator_followers": follower_counts,
                    "rank": rank,
                    "period_days": period_days,
                    "curve": curve,
                    "curve_start_ts": (curve[0]["ts"] if curve else None),
                    "growth_ratio": growth,
                    "top_creators": creators,
                    "industry_id": ind_id or None,
                },
            ))
        except Exception as e:
            log.debug("tiktok: skipped malformed row: %s", e)
    return out


# --------------------------------------------------------------------------- #
# Fetchers — async over an httpx.AsyncClient, raise on HTTP errors
# --------------------------------------------------------------------------- #

async def fetch_google_trends(client: httpx.AsyncClient, geo: str) -> list[RawItem]:
    geo = (geo or "US").upper()
    r = await client.get(GOOGLE_TRENDS_URL, params={"geo": geo})
    r.raise_for_status()
    return parse_google_trends_rss(r.text, geo)


async def fetch_bluesky(client: httpx.AsyncClient, limit: int = 25) -> list[RawItem]:
    r = await client.get(BLUESKY_TRENDS_URL, params={"limit": int(limit)})
    r.raise_for_status()
    return parse_bluesky(r.json())


# Reddit's anonymous RSS is throttled per IP and says so in headers
# (x-ratelimit-remaining / x-ratelimit-reset, seconds). Measured 2026-08-19: one
# request per window, then 429 until the reset. So the fetcher paces itself off
# those headers — module state shared by all reddit units (they run through one
# lane in fetch_all) — and retries a 429 once the window has reset.
REDDIT_ATTEMPTS = 3
REDDIT_MAX_WAIT_SEC = 120.0
_reddit_next_allowed_at = 0.0  # time.monotonic() before which we must not call Reddit


def _monotonic() -> float:  # seam for tests
    return time.monotonic()


def _reddit_wait_from_headers(headers: Any, *, status_code: int) -> float:
    """Seconds to wait before the NEXT Reddit request, from the rate-limit headers."""
    def _num(name: str) -> Optional[float]:
        try:
            raw = headers.get(name)
        except Exception:
            raw = None
        try:
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None
    retry_after = _num("retry-after")
    remaining = _num("x-ratelimit-remaining")
    reset = _num("x-ratelimit-reset")
    wait = 0.0
    if status_code == 429:
        wait = retry_after or (reset + 1.0 if reset else RATE_LIMIT_BACKOFF_SEC)
    elif remaining is not None and remaining < 1 and reset:
        wait = reset + 1.0
    return max(0.0, min(wait, REDDIT_MAX_WAIT_SEC))


async def fetch_reddit(client: httpx.AsyncClient, sub: str, limit: int = 25) -> list[RawItem]:
    global _reddit_next_allowed_at
    sub = _clean_sub(sub)
    if not sub:
        raise ValueError("reddit: empty subreddit")
    last_exc: Optional[BaseException] = None
    for attempt in range(1, REDDIT_ATTEMPTS + 1):
        delay = _reddit_next_allowed_at - _monotonic()
        if delay > 0:
            await _sleep(delay)
        r = await client.get(
            REDDIT_TOP_URL.format(sub=sub),
            params={**REDDIT_TOP_PARAMS, "limit": int(limit)},
            headers={"User-Agent": REDDIT_UA},
        )
        wait = _reddit_wait_from_headers(r.headers, status_code=r.status_code)
        if wait:
            _reddit_next_allowed_at = _monotonic() + wait
        if r.status_code == 429:
            log.info("general_trends: reddit r/%s rate-limited (429) — attempt %d/%d, next call in %.0fs",
                     sub, attempt, REDDIT_ATTEMPTS, wait)
            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
            continue
        r.raise_for_status()
        return parse_reddit_atom(r.text, sub)
    assert last_exc is not None
    setattr(last_exc, "_gt_no_retry", True)  # already spent its own budget; fetch_all must not retry again
    raise last_exc


async def fetch_kym(client: httpx.AsyncClient) -> list[RawItem]:
    r = await client.get(KYM_NEWSFEED_URL)
    r.raise_for_status()
    return parse_kym_rss(r.text)


async def _kym_entry_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """GET one entry page -> facts. ``{}`` on ANY failure.

    Enrichment is a bonus, so a 404, a timeout or a re-skinned page must cost
    us the taxonomy for that entry and nothing else. Losing the item would be
    strictly worse than keeping it with feed data only.
    """
    try:
        r = await client.get(url)
        r.raise_for_status()
        return parse_kym_entry_page(r.text, url=url, today=today)
    except Exception as e:  # noqa: BLE001 — see the docstring
        log.info("kym_entries: could not enrich %s (%s)", url, _err_str(e))
        return {}


async def _kym_enrich(
    client: httpx.AsyncClient,
    urls: list[str],
    *,
    concurrency: int = KYM_ENTRY_CONCURRENCY,
    today: Optional[date] = None,
) -> dict[str, dict[str, Any]]:
    """Fetch entry pages with modest concurrency -> ``{url: facts}``."""
    if not urls:
        return {}
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(url: str) -> tuple[str, dict[str, Any]]:
        async with sem:
            return url, await _kym_entry_page(client, url, today=today)

    pairs = await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    out: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        if isinstance(pair, BaseException):
            continue
        url, facts = pair
        if facts:
            out[url] = facts
    return out


async def fetch_kym_entries(
    client: httpx.AsyncClient,
    *,
    enrich_max: int = KYM_ENTRY_FETCH_MAX,
    concurrency: int = KYM_ENTRY_CONCURRENCY,
    today: Optional[date] = None,
) -> list[RawItem]:
    """The Know Your Meme entry DATABASE: all.rss + confirmed.rss + sitemap.

    One unit rather than three so the entry-page budget is a per-RUN number
    (``enrich_max``, ~40) instead of one that multiplies by however many feeds
    exist this month.

    Degradation follows ``fetch_wikipedia``'s precedent, which is already the
    house pattern for "a trunk request and some optional branches":

      * ``all.rss`` is the TRUNK. An HTTP error raises (a real source failure),
        and a 200 that parses to nothing raises :class:`KymEntryLayoutChanged`
        — the whole point of a named exception is that a feed which starts
        answering 200-with-no-items is otherwise invisible for weeks.
      * ``confirmed.rss`` and ``sitemap.xml`` are BRANCHES. A failure there is
        logged and the run continues on what the trunk gave us.
      * entry-page enrichment is a bonus per item: a page that fails leaves
        that item exactly as the feed delivered it.
    """
    budget = max(0, int(enrich_max))
    r = await client.get(KYM_ENTRY_FEEDS["all"])
    r.raise_for_status()
    items = parse_kym_entry_feed(r.text, feed="all", today=today)
    if not items:
        raise KymEntryLayoutChanged(
            f"kym_entries: {KYM_ENTRY_FEEDS['all']} returned no parsable <item> "
            f"({len(r.text or '')} bytes) — the feed shape moved"
        )

    seen: set[str] = {str(it["source_key"]) for it in items}
    for feed_id, url in KYM_ENTRY_FEEDS.items():
        if feed_id == "all":
            continue
        try:
            r2 = await client.get(url)
            r2.raise_for_status()
            for it in parse_kym_entry_feed(r2.text, feed=feed_id, today=today):
                key = str(it["source_key"])
                if key not in seen:
                    seen.add(key)
                    items.append(it)
        except (httpx.HTTPError, ValueError) as e:
            log.info("kym_entries: %s unavailable (%s) — continuing", url, e)

    # Sitemap-only entries: 29 of the live sitemap's 44 /memes/ URLs are in no
    # feed at all, and all of them were inside ten days.
    sitemap: list[dict[str, Any]] = []
    try:
        r3 = await client.get(KYM_SITEMAP_URL)
        r3.raise_for_status()
        sitemap = [u for u in parse_kym_sitemap(r3.text) if u["slug"] not in seen]
    except (httpx.HTTPError, ValueError) as e:
        log.info("kym_entries: sitemap unavailable (%s) — continuing", e)

    # Feed items get the budget first (they are the freshest and already have
    # a title); whatever is left goes to sitemap-only entries, newest first.
    feed_urls = [str(it["url"]) for it in items if it.get("url")][:budget]
    spare = max(0, budget - len(feed_urls))
    extra_urls = [str(u["url"]) for u in sitemap[:spare]]
    facts_by_url = await _kym_enrich(
        client, feed_urls + extra_urls, concurrency=concurrency, today=today)

    for it in items:
        facts = facts_by_url.get(str(it.get("url") or ""))
        if facts:
            kym_apply_entry_facts(it, facts)

    for offset, entry in enumerate(sitemap[:spare]):
        facts = facts_by_url.get(entry["url"])
        if not facts:
            continue
        built = kym_item_from_entry_page(
            facts, url=entry["url"], slug=entry["slug"], lastmod=entry["lastmod"],
            rank=len(items) + offset + 1, rank_total=len(items) + len(extra_urls))
        if built is not None:
            items.append(built)
    return items


async def fetch_kym_editorials(
    client: httpx.AsyncClient,
    *,
    today: Optional[date] = None,
) -> list[RawItem]:
    """KYM's "What is the X meme?" explainers. NON-minting corroboration."""
    r = await client.get(KYM_EDITORIALS_URL)
    r.raise_for_status()
    items = parse_kym_editorials_rss(r.text, today=today)
    if not items:
        raise KymEntryLayoutChanged(
            f"kym_editorials: {KYM_EDITORIALS_URL} returned no parsable <item> "
            f"({len(r.text or '')} bytes) — the feed shape moved"
        )
    return items


def youtube_shorts_body(region: str, period: str = "WEEKLY") -> dict[str, Any]:
    """The InnerTube request body for one (country, period).

    Split out because it IS the integration: the ``flags=`` in ``query`` is
    what unlocks the Shorts charts at all, and ``gl``/``chart_params_country_code``
    have to agree or the reply is for a different country than the label.
    """
    cc = (region or "").upper()
    return {
        "context": {"client": {**YOUTUBE_CHARTS_CLIENT, "gl": cc}},
        "browseId": YOUTUBE_CHARTS_BROWSE_ID,
        "query": (
            f"flags={YOUTUBE_CHARTS_FLAGS}"
            "&perspective=CHART_DETAILS"
            f"&chart_params_country_code={cc.lower()}"
            f"&chart_params_chart_type={YOUTUBE_SHORTS_CHART_TYPE}"
            f"&chart_params_period_type={str(period or 'WEEKLY').upper()}"
        ),
    }


async def fetch_youtube_shorts(
    client: httpx.AsyncClient,
    *,
    region: str,
    period: str = "WEEKLY",
) -> list[RawItem]:
    """One country's "songs used most by creators on Shorts" chart.

    Keyless, cookieless, no OAuth. No region allow-list is kept here on
    purpose: unlike TikTok's Creative Center — which silently answers with US
    data for a code it does not know — this endpoint returns HTTP 400 for an
    unknown country, so the server validates and a mislabelled card is
    impossible.

    Raises :class:`ShortsChartChanged` on SCHEMA DRIFT, which for an
    undocumented RPC is the failure that actually happens: 200 OK with
    ``musicAnalyticsSectionRenderer`` renamed, or with the chart swapped for a
    listening chart, or with an empty row list. None of those is a non-200 and
    none of them would ever be noticed.
    """
    cc = (region or "").upper()
    if not re.fullmatch(r"[A-Z]{2}", cc):
        raise ValueError(f"youtube_shorts: not an ISO-2 country code: {region!r}")
    per = str(period or "WEEKLY").upper()
    if per not in YOUTUBE_SHORTS_PERIODS:
        per = YOUTUBE_SHORTS_PERIODS[0]
    r = await client.post(
        YOUTUBE_CHARTS_URL,
        params={"alt": "json"},
        json=youtube_shorts_body(cc, per),
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    payload = r.json()
    chart = youtube_shorts_chart(payload)
    if chart is None:
        raise ShortsChartChanged(
            f"youtube_shorts: no musicAnalyticsSectionRenderer/trackViews in the "
            f"{cc}/{per} reply — the InnerTube schema moved"
        )
    list_type = str(chart.get("listType") or "")
    if "SHORTS" not in list_type.upper():
        raise ShortsChartChanged(
            f"youtube_shorts: {cc}/{per} returned listType {list_type!r}, not a "
            f"Shorts USAGE chart — a listening chart is not participation"
        )
    items = parse_youtube_shorts(payload, cc, per)
    if not items:
        raise ShortsChartChanged(
            f"youtube_shorts: {cc}/{per} chart parsed to zero rows "
            f"({len(chart.get('trackViews') or [])} raw) — the row shape moved"
        )
    return items


def _wiki_url(day: date) -> str:
    return WIKIPEDIA_TOP_URL.format(y=f"{day.year:04d}", m=f"{day.month:02d}", d=f"{day.day:02d}")


async def fetch_wikipedia(client: httpx.AsyncClient, *, today: Optional[date] = None) -> list[RawItem]:
    """Top articles for D-1 vs D-2 (UTC). The day's data lands with a lag, so
    when D-1 is still 404 we compare D-2 vs D-3 instead. If the comparison day
    itself is unavailable we still return D-x's list (nothing filtered by rise)."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(today, datetime):
        today = today.date()
    d1 = today - timedelta(days=1)
    r = await client.get(_wiki_url(d1))
    if r.status_code == 404:
        d1 = today - timedelta(days=2)
        r = await client.get(_wiki_url(d1))
    r.raise_for_status()
    today_payload = r.json()
    yesterday_payload: Any = None
    try:
        r2 = await client.get(_wiki_url(d1 - timedelta(days=1)))
        if r2.status_code < 400:
            yesterday_payload = r2.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("wikipedia: previous-day payload unavailable (%s); rise filter off", e)
    return parse_wikipedia_top(today_payload, yesterday_payload)


async def fetch_youtube(client: httpx.AsyncClient, region: str, api_key: str) -> list[RawItem]:
    if not api_key:
        raise ValueError("youtube: YOUTUBE_API_KEY is not set")
    region = (region or "US").upper()
    r = await client.get(YOUTUBE_VIDEOS_URL, params={
        "part": "snippet,statistics",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": 25,
        "key": api_key,
    })
    r.raise_for_status()
    return parse_youtube(r.json(), region)


async def fetch_trends24(client: httpx.AsyncClient, geo: str) -> list[RawItem]:
    geo = (geo or "").upper()
    slug = REGION_SLUGS.get(geo)
    if not slug:
        raise ValueError(f"x_trends: no trends24 slug for region {geo!r}")
    r = await client.get(TRENDS24_URL.format(slug=slug))
    r.raise_for_status()
    return parse_trends24(r.text, geo)


async def fetch_hacker_news(client: httpx.AsyncClient, limit: int = 30) -> list[RawItem]:
    """HN front page. Keyless, ~10k req/hour, one call for the whole feed."""
    r = await client.get(HN_SEARCH_URL, params={"tags": "front_page", "hitsPerPage": int(limit)})
    r.raise_for_status()
    return parse_hacker_news(r.json())


async def fetch_lemmy(client: httpx.AsyncClient, limit: int = 50) -> list[RawItem]:
    """lemmy.world, the day's top posts. Keyless, no observed throttle."""
    r = await client.get(LEMMY_POSTS_URL, params={
        "sort": "TopDay", "limit": int(limit), "type_": "All",
    })
    r.raise_for_status()
    return parse_lemmy(r.json())


async def fetch_github(
    client: httpx.AsyncClient,
    *,
    days: int = GITHUB_CREATED_WITHIN_DAYS,
    limit: int = 30,
    today: Optional[date] = None,
) -> list[RawItem]:
    """Repos created inside the window, most-starred first.

    ``today`` is a seam for tests only. 10 req/min keyless (30 with a free PAT).
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(today, datetime):
        today = today.date()
    since = (today - timedelta(days=max(1, int(days)))).isoformat()
    r = await client.get(
        GITHUB_SEARCH_URL,
        params={"q": f"created:>{since}", "sort": "stars", "order": "desc",
                "per_page": int(limit)},
        headers={"Accept": GITHUB_ACCEPT},
    )
    r.raise_for_status()
    return parse_github(r.json())


async def fetch_google_news(
    client: httpx.AsyncClient,
    *,
    query: str,
    geo: str = "US",
    hl: str = "en-US",
    gl: str = "US",
    ceid: str = "US:en",
) -> list[RawItem]:
    """One curated Google News query. The bare URL 302s — the shared client
    follows redirects, which is why this needs no special handling here."""
    if not (query or "").strip():
        raise ValueError("google_news: empty query")
    r = await client.get(GOOGLE_NEWS_SEARCH_URL, params={
        "q": query, "hl": hl, "gl": gl, "ceid": ceid,
    })
    r.raise_for_status()
    return parse_google_news_rss(r.text, (geo or gl or "").upper(), query)


async def fetch_tiktok_hashtags(
    client: httpx.AsyncClient,
    *,
    region: str,
    period: int = 7,
) -> list[RawItem]:
    """TikTok Creative Center trending hashtags for one (region, period).

    Three guards, all load-bearing (see the constants above): the browser
    ``Accept: text/html`` header, the 27-code region allow-list, and a hard
    raise when the SSR loader key is gone.
    """
    region = (region or "").upper()
    if region not in TIKTOK_REGIONS:
        # An unsupported code does not error server-side, it silently returns US
        # data — which would mislabel the region on every card it produced.
        raise ValueError(f"tiktok: unsupported region {region!r}")
    period_days = int(period) if int(period) in TIKTOK_PERIODS else TIKTOK_PERIODS[0]
    r = await client.get(
        TIKTOK_HASHTAG_URL,
        params={"region": region, "period": period_days},
        headers=TIKTOK_HEADERS,
    )
    r.raise_for_status()
    if tiktok_loader_payload(r.text) is None:
        raise TikTokLayoutChanged(
            f"tiktok: {_TT_LOADER_KEY!r} missing from the {region}/{period_days}d page "
            f"({len(r.text or '')} bytes) — the SSR payload moved or Accept was overridden"
        )
    return parse_tiktok_hashtags(r.text, region, period_days)


# --------------------------------------------------------------------------- #
# Instagram — thin lazy wrappers over general_trends_instagram
#
# That module imports THIS one (it reuses make_item/collapse_ws), so a top-level
# import here is circular: whichever module loads second gets a half-built
# partner, and the failure mode is silent and import-order dependent. Importing
# inside the call keeps the cycle broken while still exposing
# ``fetch_instagram_*`` in this module's globals, which is where ``_dispatch``
# and every monkeypatch seam look. A missing module then surfaces as an ordinary
# per-unit failure ("that source failed"), never as "the trends tab is down".
# --------------------------------------------------------------------------- #

async def fetch_instagram_hashtag(client: Any, *, tag: str, **kwargs: Any) -> list["RawItem"]:
    from .general_trends_instagram import fetch_instagram_hashtag as _impl
    return await _impl(client, tag=tag, **kwargs)


async def fetch_instagram_watch(client: Any, *, username: str, **kwargs: Any) -> list["RawItem"]:
    from .general_trends_instagram import fetch_instagram_watch as _impl
    return await _impl(client, username=username, **kwargs)


# --------------------------------------------------------------------------- #
# Plan + gather
# --------------------------------------------------------------------------- #

def _clean_list(values: Any, *, upper: bool = False) -> list[str]:
    out: list[str] = []
    for v in values or []:
        s = str(v or "").strip()
        if not s:
            continue
        s = s.upper() if upper else s
        if s not in out:
            out.append(s)
    return out


def build_fetch_plan(
    *,
    sources: list[str],
    geos: list[str],
    subreddits: list[str],
    youtube_api_key: str,
    tiktok_regions: Optional[list[str]] = None,
    ig_hashtags: Optional[list[str]] = None,
    ig_watch_accounts: Optional[list[str]] = None,
    shorts_regions: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Expand ``sources × geos/subs/tags`` into concrete fetch units.

    Each unit: ``{"source", "label", "region", "unit", "params"}`` where
    ``unit`` is a stable human id (``google_trends:US``, ``reddit:memes``,
    ``bluesky``) and ``params`` are the fetcher's keyword arguments.

    A source whose configuration is empty emits NO units rather than failing at
    fetch time — the pattern YouTube already used for its API key, now shared by
    tiktok (regions), instagram (hashtags) and instagram_watch (accounts).
    x_trends units exist only for geos with a known trends24 slug; unknown
    source ids are ignored.

    ``shorts_regions`` is the ONE exception to "empty config ⇒ no units": None
    falls back to :data:`SHORTS_DEFAULT_REGIONS` (AE, SA, US) because the
    endpoint is keyless and geo-correct, so there is nothing to configure
    before it works and a caller that has not been taught the new setting
    should still get the Gulf. An explicit empty list still turns it off.
    """
    wanted = _clean_list(sources)
    geo_list = _clean_list(geos, upper=True)
    sub_list = [s for s in (_clean_sub(x) for x in _clean_list(subreddits)) if s]
    tt_regions = _clean_list(tiktok_regions or [], upper=True)
    tags = [t.lstrip("#") for t in _clean_list(ig_hashtags or []) if t.lstrip("#")]
    accounts = [a.lstrip("@") for a in _clean_list(ig_watch_accounts or []) if a.lstrip("@")]
    shorts = _clean_list(
        list(SHORTS_DEFAULT_REGIONS) if shorts_regions is None else shorts_regions,
        upper=True)
    key = (youtube_api_key or "").strip()
    plan: list[dict[str, Any]] = []

    def add(source: str, region: str, unit: str, params: dict[str, Any]) -> None:
        plan.append({
            "source": source,
            "label": SOURCE_LABELS[source],
            "region": region,
            "unit": unit,
            "params": params,
        })

    for source in wanted:
        if source not in SOURCE_IDS:
            log.info("general_trends: unknown source id %r ignored", source)
            continue
        if source == "google_trends":
            for geo in geo_list:
                add(source, geo, f"google_trends:{geo}", {"geo": geo})
        elif source == "youtube":
            if not key:
                continue
            for geo in geo_list:
                add(source, geo, f"youtube:{geo}", {"region": geo, "api_key": key})
        elif source == "x_trends":
            for geo in geo_list:
                if geo in REGION_SLUGS:
                    add(source, geo, f"x_trends:{geo}", {"geo": geo})
        elif source == "reddit":
            for sub in sub_list:
                add(source, GLOBAL_REGION, f"reddit:{sub}", {"sub": sub})
        elif source == "bluesky":
            add(source, GLOBAL_REGION, "bluesky", {})
        elif source == "kym":
            add(source, GLOBAL_REGION, "kym", {})
        elif source == "kym_entries":
            # ONE unit on purpose: all.rss + confirmed.rss + the sitemap share a
            # single per-run entry-page budget (KYM_ENTRY_FETCH_MAX), which they
            # could not do as three independent units.
            add(source, GLOBAL_REGION, "kym_entries", {})
        elif source == "kym_editorials":
            add(source, GLOBAL_REGION, "kym_editorials", {})
        elif source == "youtube_shorts":
            for region in shorts:
                add(source, region, f"youtube_shorts:{region}",
                    {"region": region, "period": YOUTUBE_SHORTS_PERIODS[0]})
        elif source == "wikipedia":
            add(source, GLOBAL_REGION, "wikipedia", {})
        elif source == "hacker_news":
            add(source, GLOBAL_REGION, "hacker_news", {})
        elif source == "lemmy":
            add(source, GLOBAL_REGION, "lemmy", {})
        elif source == "github":
            add(source, GLOBAL_REGION, "github", {})
        elif source == "google_news":
            # Editorial queries, not geo expansion: each unit carries its own
            # language/edition triple, so the Gulf unit stays English even when
            # GENERAL_TRENDS_GEOS never mentions AE.
            for q in GOOGLE_NEWS_UNITS:
                add(source, q["region"], f"google_news:{q['id']}", {
                    "query": q["query"], "geo": q["region"],
                    "hl": q["hl"], "gl": q["gl"], "ceid": q["ceid"],
                })
        elif source == "tiktok":
            for region in tt_regions:
                if region not in TIKTOK_REGIONS:
                    log.info("general_trends: tiktok region %r not supported — skipped", region)
                    continue
                for period in TIKTOK_PERIODS:
                    add(source, region, f"tiktok:{region}:{period}d",
                        {"region": region, "period": period})
        elif source == "instagram":
            for tag in tags:
                add(source, GLOBAL_REGION, f"instagram:{tag}", {"tag": tag})
        elif source == "instagram_watch":
            for username in accounts:
                add(source, GLOBAL_REGION, f"instagram_watch:{username}", {"username": username})
    return plan


# --------------------------------------------------------------------------- #
# Low-value item shapes
# --------------------------------------------------------------------------- #
# Not a safety question and not a recency question — a shape question. Two
# item shapes are structurally incapable of becoming a trend a coding school
# could ride, and both were a large share of the first live run:
#
#   * A SPORTS FIXTURE. "al-najma vs al-ittihad", "giants vs guardians",
#     "sparks vs sun", "النصر ضد الدرعية". Measured: 11 of 36 Google Trends
#     cards in run #1. A fixture spikes for ninety minutes, is over, and is
#     never a format anyone can recreate on campus. It is also the single
#     largest source of "why is this here" in the feed.
#   * A BARE TOKEN. A one- or two-word title with no description and no
#     number: nothing downstream can identify it, so the scorer invents a
#     meaning for it ("Financial Transactions / Fintech" from an Arabic
#     hashtag). Dropping it at ingest is what stops the invention.
#
# Both are DROPS, so neither ever reaches the database or the card — they show
# up only in the run's `dropped_by` counters.

# "a vs b" / "a v. b" / "a ضد b" — the separator must be a whole word, so
# "Versace" and "Vsauce" are untouched. A bare hyphen is deliberately NOT a
# separator: "Spider-Man" and "Mercedes-Benz" would go with it.
_FIXTURE_SEPARATORS = re.compile(
    r"(?:^|\s)(?:vs\.?|v\.|ضد)(?:\s|$)", re.IGNORECASE | re.UNICODE)

# Sources whose items are a NAME with no prose. A bare token from these is
# unidentifiable; a short title from a feed that also ships a summary is not.
_BARE_TOKEN_MAX_WORDS = 2


def is_sports_fixture(title: Any) -> bool:
    """True for "X vs Y" style match-up titles (see the module note)."""
    text = collapse_ws(str(title or ""))
    if not text:
        return False
    return bool(_FIXTURE_SEPARATORS.search(text))


def is_bare_token(item: dict) -> bool:
    """True when an item is a short name with nothing to identify it by: no
    summary, no metric, and at most two words."""
    if not isinstance(item, dict):
        return False
    if collapse_ws(str(item.get("summary") or "")):
        return False
    if item.get("metric_value") is not None:
        return False
    words = collapse_ws(str(item.get("title") or "")).split()
    return 0 < len(words) <= _BARE_TOKEN_MAX_WORDS


def screen_shape(item: dict) -> tuple[str, str]:
    """Ingest screen for structurally useless items -> ("keep"|"drop", reason).

    Never raises: a malformed item is kept, because losing a source to a filter
    bug is worse than one bad card."""
    try:
        if is_sports_fixture(item.get("title")):
            return "drop", "fixture"
        if is_bare_token(item):
            return "drop", "bare_token"
    except Exception:  # noqa: BLE001
        log.debug("general_trends: shape screen failed for an item", exc_info=True)
    return "keep", ""


def default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": config.user_agent},
    )


async def _dispatch(client: httpx.AsyncClient, unit: dict[str, Any]) -> list[RawItem]:
    """Route one plan unit to its fetcher. Looks the fetcher up by NAME at call
    time so a monkeypatched ``gts.fetch_<source>`` is honoured."""
    source = unit.get("source")
    params = dict(unit.get("params") or {})
    if source == "google_trends":
        return await fetch_google_trends(client, params.get("geo", "US"))
    if source == "bluesky":
        return await fetch_bluesky(client)
    if source == "reddit":
        return await fetch_reddit(client, params.get("sub", ""))
    if source == "kym":
        return await fetch_kym(client)
    if source == "kym_entries":
        return await fetch_kym_entries(client)
    if source == "kym_editorials":
        return await fetch_kym_editorials(client)
    if source == "wikipedia":
        return await fetch_wikipedia(client)
    if source == "youtube":
        return await fetch_youtube(client, params.get("region", "US"), params.get("api_key", ""))
    if source == "youtube_shorts":
        return await fetch_youtube_shorts(
            client, region=params.get("region", ""),
            period=params.get("period", YOUTUBE_SHORTS_PERIODS[0]))
    if source == "x_trends":
        return await fetch_trends24(client, params.get("geo", ""))
    if source == "hacker_news":
        return await fetch_hacker_news(client)
    if source == "lemmy":
        return await fetch_lemmy(client)
    if source == "github":
        return await fetch_github(client)
    if source == "google_news":
        return await fetch_google_news(
            client,
            query=params.get("query", ""),
            geo=params.get("geo", "US"),
            hl=params.get("hl", "en-US"),
            gl=params.get("gl", "US"),
            ceid=params.get("ceid", "US:en"),
        )
    if source == "tiktok":
        return await fetch_tiktok_hashtags(
            client, region=params.get("region", ""), period=params.get("period", 7))
    if source == "instagram":
        return await fetch_instagram_hashtag(client, tag=params.get("tag", ""))
    if source == "instagram_watch":
        return await fetch_instagram_watch(client, username=params.get("username", ""))
    raise ValueError(f"unknown source {source!r}")


# Sources whose host throttles concurrent pulls from one client. Their units run
# through a single lane inside fetch_all (see there). Instagram is here for a
# different reason than Reddit: the Graph rate limit is SHARED with the publish
# path, so a doomed run must never burn it in parallel.
SERIAL_SOURCES: frozenset[str] = frozenset({"reddit", "instagram", "instagram_watch"})
SERIAL_UNIT_DELAY_SEC = 1.5
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_BACKOFF_SEC = 5.0
RATE_LIMIT_MAX_WAIT_SEC = 30.0


async def _sleep(seconds: float) -> None:  # monkeypatch seam for tests
    if seconds > 0:
        await asyncio.sleep(seconds)


def _retry_after_seconds(exc: httpx.HTTPStatusError) -> float:
    """Honour Retry-After (seconds form) when the server sends one; else back off."""
    raw = (exc.response.headers.get("retry-after") or "").strip()
    try:
        wait = float(raw) if raw else RATE_LIMIT_BACKOFF_SEC
    except ValueError:
        wait = RATE_LIMIT_BACKOFF_SEC
    return max(0.0, min(wait, RATE_LIMIT_MAX_WAIT_SEC))


async def _dispatch_with_retry(client: httpx.AsyncClient, unit: dict[str, Any]) -> list[RawItem]:
    """_dispatch, retrying HTTP 429 up to RATE_LIMIT_RETRIES times."""
    attempt = 0
    while True:
        try:
            return await _dispatch(client, unit)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or attempt >= RATE_LIMIT_RETRIES or getattr(exc, "_gt_no_retry", False):
                raise
            attempt += 1
            wait = _retry_after_seconds(exc)
            log.info("general_trends: %s rate-limited (429) — retry %d/%d in %.0fs",
                     unit.get("unit"), attempt, RATE_LIMIT_RETRIES, wait)
            await _sleep(wait)


def _err_str(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        msg = f"HTTP {exc.response.status_code} from {exc.request.url.host}"
    elif isinstance(exc, httpx.TimeoutException):
        msg = f"timeout ({exc.__class__.__name__})"
    else:
        msg = f"{exc.__class__.__name__}: {exc}"
    return collapse_ws(msg)[:300]


# item -> ("keep" | "flag" | "drop", reason). Supplied by the pipeline, which
# owns the safety and freshness modules; this module only calls it.
ScreenFn = Callable[[RawItem], tuple[str, str]]


async def fetch_all(
    plan: list[dict[str, Any]],
    *,
    client: Optional[httpx.AsyncClient] = None,
    concurrency: int = 6,
    max_items_per_unit: int = 40,
    caps: Optional[dict[str, int]] = None,
    screen: Optional[ScreenFn] = None,
) -> tuple[list[RawItem], dict[str, dict[str, Any]]]:
    """Run every unit of ``plan`` concurrently (bounded) and never raise.

    Returns ``(items, stats)`` where ``stats[source] = {"ok", "count", "error",
    "ms"}``: a source is ok when at least one of its units succeeded, ``error``
    joins the failed units' messages (None when all good), ``ms`` is the total
    time spent across that source's units. Items keep plan order; empty-title
    items are dropped.

    ``caps`` overrides ``max_items_per_unit`` per source. It exists because the
    budget was upside down: Reddit had 7 units x 40 = 280 slots while Know Your
    Meme — the best signal-to-noise source in the system, and the only one that
    ships a name AND a written explanation — had 8.

    ``screen`` runs INSIDE the per-unit worker, BEFORE the cap slice, so the cap
    yields good items instead of whatever happened to come first. A ``"flag"``
    keeps the item and stamps ``item["safety"]="flagged"`` /
    ``item["safety_reason"]``; a ``"drop"`` removes it, and both are counted
    into ``stats[source]["dropped"]`` / ``["dropped_by"]`` so a source's junk
    rate is visible on the /sources health row without reading logs.

    With no ``screen`` the behaviour — and the stats key set — is exactly what
    it was before the screen existed. That is a tested invariant.
    """
    if not plan:
        return [], {}
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    default_cap = max(0, int(max_items_per_unit))
    cap_by_source = {
        str(k): max(0, int(v)) for k, v in (caps or {}).items() if str(k)
    }
    own_client = client is None
    http = client if client is not None else default_client()

    # Hosts that rate-limit per client (Reddit answers 429 to concurrent RSS
    # pulls) get ONE lane: units for that source run one after another with a
    # polite pause, and a 429 is retried after the Retry-After (or a short
    # backoff) instead of losing the whole subreddit for this run.
    lanes: dict[str, asyncio.Lock] = {}

    def _screen_unit(items: list[RawItem]) -> tuple[list[RawItem], dict[str, int], int]:
        """Apply ``screen`` to one unit's items → (kept, dropped_by, flagged).

        Never raises: a screen that blows up must not cost us the source
        (degraded modes must survive), so a failing screen keeps the item.
        """
        kept: list[RawItem] = []
        dropped_by: dict[str, int] = {}
        flagged = 0
        for it in items:
            try:
                verdict, reason = screen(it)  # type: ignore[misc]
            except Exception as e:  # noqa: BLE001
                log.warning("general_trends: screen failed on an item (%s) — kept", e)
                kept.append(it)
                continue
            reason = str(reason or "") or "unspecified"
            if verdict == "drop":
                dropped_by[reason] = dropped_by.get(reason, 0) + 1
                continue
            if verdict == "flag":
                # Kept, stored, and never sent to the model: the audit trail the
                # owner needs to tell a fix from a bug.
                it["safety"] = "flagged"
                it["safety_reason"] = reason
                flagged += 1
            kept.append(it)
        return kept, dropped_by, flagged

    async def _one(unit: dict[str, Any]) -> tuple[list[RawItem], int, dict[str, int], int]:
        source = str(unit.get("source") or "")
        lane = lanes.setdefault(source, asyncio.Lock()) if source in SERIAL_SOURCES else None
        cap = cap_by_source.get(source, default_cap)
        async with sem:
            t0 = time.monotonic()
            try:
                if lane is not None:
                    async with lane:
                        items = await _dispatch_with_retry(http, unit)
                        await _sleep(SERIAL_UNIT_DELAY_SEC)
                else:
                    items = await _dispatch_with_retry(http, unit)
            except BaseException as exc:
                # Carry the elapsed time on the exception so stats can report it.
                setattr(exc, "_gt_ms", int((time.monotonic() - t0) * 1000))
                raise
            ms = int((time.monotonic() - t0) * 1000)
            good = [
                it for it in (items or [])
                if isinstance(it, dict) and collapse_ws(str(it.get("title") or ""))
            ]
            dropped_by: dict[str, int] = {}
            flagged = 0
            if screen is not None:
                good, dropped_by, flagged = _screen_unit(good)
            return (good[:cap] if cap else []), ms, dropped_by, flagged

    try:
        results = await asyncio.gather(*(_one(u) for u in plan), return_exceptions=True)
    finally:
        if own_client:
            await http.aclose()

    stats: dict[str, dict[str, Any]] = {}
    items_out: list[RawItem] = []
    errors: dict[str, list[str]] = {}
    for unit, res in zip(plan, results):
        source = unit["source"]
        base: dict[str, Any] = {"ok": False, "count": 0, "error": None, "ms": 0}
        if screen is not None:
            base["dropped"] = 0
            base["dropped_by"] = {}
            base["flagged"] = 0
        st = stats.setdefault(source, base)
        if isinstance(res, BaseException):
            msg = _err_str(res)
            errors.setdefault(source, []).append(f"{unit['unit']}: {msg}")
            st["ms"] += int(getattr(res, "_gt_ms", 0) or 0)
            log.warning("general_trends: %s failed: %s", unit["unit"], msg)
            continue
        got, ms, dropped_by, flagged = res
        st["ok"] = True
        st["count"] += len(got)
        st["ms"] += ms
        if screen is not None:
            st["dropped"] += sum(dropped_by.values())
            st["flagged"] += flagged
            for reason, n in dropped_by.items():
                st["dropped_by"][reason] = st["dropped_by"].get(reason, 0) + n
        items_out.extend(got)
    for source, errs in errors.items():
        stats[source]["error"] = "; ".join(errs)[:600]
    return items_out, stats


FetcherFn = Callable[..., Awaitable[list[RawItem]]]

__all__ = [
    "SOURCE_IDS", "SOURCE_LABELS", "REGION_SLUGS", "REDDIT_UA", "GLOBAL_REGION",
    "SERIAL_SOURCES", "SERIAL_UNIT_DELAY_SEC", "RATE_LIMIT_RETRIES",
    "GLOBAL_SOURCES", "NON_MINTING_SOURCES", "RawItem", "ScreenFn", "make_item",
    "TIKTOK_REGIONS", "TIKTOK_PERIODS", "TIKTOK_HEADERS", "TikTokLayoutChanged",
    "GOOGLE_NEWS_UNITS", "GITHUB_CREATED_WITHIN_DAYS",
    "MINTING_SOURCES", "is_minting",
    "KYM_ENTRY_FEEDS", "KYM_SITEMAP_URL", "KYM_EDITORIALS_URL",
    "KYM_ENTRY_FETCH_MAX", "KYM_ENTRY_CONCURRENCY", "KYM_FORMAT_TYPES",
    "KYM_NON_FORMAT_TYPES", "KymEntryLayoutChanged",
    "YOUTUBE_CHARTS_URL", "YOUTUBE_SHORTS_PERIODS", "SHORTS_DEFAULT_REGIONS",
    "ShortsChartChanged",
    "parse_google_trends_rss", "parse_bluesky", "parse_reddit_atom", "parse_kym_rss",
    "parse_wikipedia_top", "parse_youtube", "parse_trends24",
    "parse_hacker_news", "parse_lemmy", "parse_github", "parse_google_news_rss",
    "parse_tiktok_hashtags", "tiktok_loader_payload",
    "parse_kym_entry_feed", "parse_kym_editorials_rss", "parse_kym_sitemap",
    "parse_kym_entry_page", "kym_apply_entry_facts", "kym_item_from_entry_page",
    "kym_wave_year", "kym_is_format", "kym_is_revival",
    "parse_youtube_shorts", "youtube_shorts_chart", "youtube_shorts_body",
    "fetch_google_trends", "fetch_bluesky", "fetch_reddit", "fetch_kym",
    "fetch_kym_entries", "fetch_kym_editorials", "fetch_youtube_shorts",
    "fetch_wikipedia", "fetch_youtube", "fetch_trends24",
    "fetch_hacker_news", "fetch_lemmy", "fetch_github", "fetch_google_news",
    "fetch_tiktok_hashtags", "fetch_instagram_hashtag", "fetch_instagram_watch",
    "build_fetch_plan", "fetch_all", "default_client",
    "traffic_to_float", "html_to_text", "first_img_src", "cap_text", "collapse_ws",
    "parse_rfc822", "parse_iso", "wiki_is_housekeeping", "wiki_is_rising",
]
