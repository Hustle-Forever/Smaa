"""Industry Signal - turning a found story into a row the feed already renders.

Nothing here is news-only plumbing bolted to the side. A story is written into
the SAME table, with the same columns, as everything else in Trend Finder, so it
inherits the feed, the ranking, the reports and the "Make this" hand-off without
any of them knowing it exists.

WHAT CARRIES THE TRANSLATION, and each one is a decision:

  ``ig_media_id``   the idempotency key, ``news:<sha1(canonical url)>``. A unique
                    index, so re-finding a story is a no-op rather than a second
                    card.
  ``caption``       THE PLAIN-ENGLISH REWRITE, not the publisher's headline. This
                    is the column the feed, the reports and the ideation prompt
                    all already read, so putting the readable version here is what
                    makes the whole tab readable. The publisher's own words are
                    kept in ``metadata.original_headline`` and shown in the detail
                    view next to the link, so nothing is hidden - the card is
                    plain, the provenance is one click away.
  ``platform``      the outlet, which the card's chip already falls back to.
  ``fit_score``     the relevance score, written AT INGEST. There is no separate
                    scoring pass any more: the call that judged the story is the
                    call that scored it.

THREE COLUMNS ARE DELIBERATELY NULL. ``outperformance`` measured how many outlets
ran a story, which a search cannot honestly count; ``uae_relevance`` and ``region``
encoded a UAE focus this feature no longer has. Leaving them NULL rather than
faking a constant is what makes ``compute_opportunity`` rank a story on its
relevance alone - see the note there about why the blend had to be made symmetric.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from . import trend_store as store

log = logging.getLogger("archivist.news_ingest")

# How long a story stays after it lands. Equal to the intake window in
# `news_search.MAX_STORY_AGE_DAYS`, so the feed is a rolling fortnight and nothing
# is ever stored only to be pruned on the next tick.
RETENTION_DAYS = 14

# Query keys that identify a CAMPAIGN rather than an article. Stripped so the same
# story shared through three channels is one row, not three.
_TRACKING_PREFIXES = ("utm_", "ref_", "mc_")
_TRACKING_KEYS = {
    "fbclid", "gclid", "igshid", "mibextid", "cmpid", "ncid", "sh", "smid",
    "srnd", "guccounter", "amp", "at_medium", "at_campaign", "spm", "s_cid",
}


def canonical_url(raw: str) -> str:
    """One address per article, whatever it was shared as.

    Forces https, drops ``www.``, drops the fragment, strips the tracking
    parameters above and keeps everything else - because a query string is
    sometimes the article (``?p=1234``) and throwing all of it away would merge
    two different stories into one row.

    The scheme is FORCED rather than lowercased. `http://x.com/a` and
    `https://x.com/a` are the same article, and the model quotes whichever the
    page redirected it to, so keeping the scheme would file the same story twice
    on two mornings and put a duplicate card in the feed.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    host = (parts.netloc or "").lower().split("@")[-1]
    if host.startswith("www."):
        host = host[4:]
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower() in _TRACKING_KEYS
                or any(k.lower().startswith(p) for p in _TRACKING_PREFIXES))
    ]
    query = "&".join(f"{k}={v}" for k, v in kept)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def external_id(url: str) -> str:
    """The idempotency key. Namespaced so it can never collide with a real post id."""
    return "news:" + hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()


def build_row(story: dict, *, brief: str = "") -> dict:
    """One found story in the shape ``trend_store.upsert_post`` expects.

    `story` is an entry from ``news_search.find_stories``. Everything the model
    decided arrives already scored, so this writes the score columns too and the
    row lands finished rather than waiting for a second pass.
    """
    url = str(story.get("url") or "")
    published = story.get("published")
    if isinstance(published, datetime) and published > datetime.now(timezone.utc):
        published = datetime.now(timezone.utc)

    headline = str(story.get("headline") or "")[:4000]
    return {
        "ig_media_id": external_id(url),
        "username": str(story.get("outlet") or "")[:200],
        "permalink": url,
        "media_type": None,
        "media_url": None,
        "thumbnail_url": None,
        "caption": headline,
        "posted_at": published,
        "engagement": None,
        # NULL, not a constant. See the module docstring.
        "outperformance": None,
        "source": "news",
        "region": None,
        "language": "en",
        "platform": str(story.get("outlet") or "")[:200],
        "metadata": {
            "original_headline": str(story.get("original_headline") or "")[:500],
            "why_it_matters": str(story.get("why_it_matters") or "")[:400],
            "canonical_url": canonical_url(url),
            # A story the model gave no date for is shown at today's date, and
            # this flag is how the detail view can say the date is our estimate
            # rather than the publisher's.
            "date_is_estimated": bool(story.get("date_is_estimated")),
            # Who the story is about, so the card can carry that company's
            # logo rather than a drawn house cover. Empty when it is not about
            # one company. `company_site` is a bare host (openai.com).
            "company": str(story.get("company") or "")[:120],
            "company_site": str(story.get("company_site") or "")[:120],
            # The website we always check that this came from, or empty when
            # the open search found it.
            "fixed_site": str(story.get("fixed_site") or "")[:120],
            # How likely a post about this would travel, 0-100, and one plain
            # sentence on why. The feed's news channel is ordered by it.
            "viral_potential": int(story.get("viral") or 0),
            "viral_reason": str(story.get("viral_reason") or "")[:300],
            "brief_used": str(brief or "")[:1000],
            "found_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def score_of(story: dict) -> dict:
    """The score columns, written at ingest by the same call that judged the story."""
    return {
        "topic": str(story.get("topic") or story.get("headline") or "")[:200],
        "topic_summary": str(story.get("why_it_matters") or "")[:400],
        "theme": str(story.get("theme") or "")[:120],
        "fit_score": int(story.get("relevance") or 0),
        "fit_reason": str(story.get("why_it_matters") or "")[:400],
        "uae_relevance": None,
        "suggested_angle": str(story.get("angle") or "")[:1000],
        "suggested_format": story.get("suggested_format") or None,
        "effort": story.get("effort") or None,
        "tags": list(story.get("tags") or []),
    }


async def existing_keys(keys: list[str]) -> set[str]:
    """Which of these identity keys the table already holds.

    The exact-duplicate guard. The unique index would refuse a second insert
    anyway, but asking first is what lets the run log say "we already had this"
    instead of counting a silent no-op as a new story.
    """
    if not keys:
        return set()
    return await store.existing_news_keys(keys)


async def ingest(stories: list[dict], *, brief: str = "") -> dict:
    """Write the morning's stories. Returns ``{new, duplicate, failed}``.

    Per-row try/except on purpose: one malformed story must not cost the other
    nine, and the counts are what the run log prints.
    """
    if not stories:
        return {"new": 0, "duplicate": 0, "failed": 0}

    rows = [(s, build_row(s, brief=brief)) for s in stories]
    seen = await existing_keys([r["ig_media_id"] for _, r in rows])

    out = {"new": 0, "duplicate": 0, "failed": 0}
    fresh: set[str] = set()
    for story, row in rows:
        key = row["ig_media_id"]
        if key in seen or key in fresh:
            out["duplicate"] += 1
            continue
        try:
            post_id = await store.upsert_post(None, row)
            if post_id is not None:
                await store.save_score(int(post_id), score_of(story))
            fresh.add(key)
            out["new"] += 1
        except Exception:  # noqa: BLE001 - one bad row is not the whole morning
            log.exception("[news] could not store %r", row.get("permalink"))
            out["failed"] += 1
    return out


async def prune() -> int:
    """Drop stories past the retention window.

    ``delete_old_news`` takes a number of DAYS and touches only ``status='new'``
    rows: a dismissed story is precisely what stops it re-appearing, and a saved
    or used one may be mid-production. Both of those outlive the window on
    purpose.
    """
    return await store.delete_old_news(RETENTION_DAYS)
