"""Industry Signal - the picture on the card.

A story arrives from a grounded search as four things: a headline, an outlet, a
date and a link. There is no image anywhere in that, so every news row was
written with ``thumbnail_url=None`` and every news card fell back to the tinted
CoverTile. A whole tab of coloured rectangles is what that looks like, and the
owner's word for it was that the thumbnails are missing, which they were.

THE PUBLISHER ALREADY CHOSE THE PICTURE. ``og:image`` is the image a link
preview shows in Slack, in WhatsApp, on LinkedIn and on X. Using it means a card
here agrees with every other place the same link gets pasted, and it needs no
judgement from us: there is exactly one right answer per article and the
publisher published it.

WHY THIS IS NOT THE RULE THE NEWS PIPELINE DELETED. ``news_search`` deliberately
does no page fetching, because it must not use the network to decide WHAT IS
TRUE - the model proposes a story and nothing here goes and checks it. This
module never touches a claim. It fetches a decoration, and a decoration that
fails to arrive costs a card its picture and nothing else. Those are different
jobs and only one of them can be wrong in a way that matters.

EVERY FAILURE IS A MISSING PICTURE, NEVER AN ERROR. Publishers block bots, put
articles behind consent walls, time out, and serve pages with no image at all.
Measured on one real morning: of six stories, two returned an image, two were
403 to any non-browser client, one timed out and two links were dead. That is
the normal shape of this job, so every path below returns None and the card
falls back to the tile it already had.

WHEN THE PUBLISHER GIVES US NOTHING, A HOUSE COVER IS USED. Three of eight
stories on a real morning had no reachable picture: two dead links and a paywall
that refuses every client, including the Facebook and Twitter crawler agents.
Scraping harder fixes none of those.

So there is a small POOL of drawn covers, generated once and then reused. A
story with no publisher picture is given one of them, chosen from its own
headline so the same story always shows the same card and a refresh never
reshuffles the grid.

A POOL, NOT ONE DRAWING PER STORY, and the reason is not only money (though it
is $0.04 a story against $0.00). A picture drawn from one headline LOOKS like a
picture OF that story, and it is not: it is a guess made by an image model from
a sentence. A recognisably house-style card makes no such claim. Every brief in
the pool is deliberately generic (a desk, a classroom, a screen) for exactly
that reason, and none of them may contain text, a real face or a brand mark.

The pool lives under its OWN asset type, so the path on a row is the durable
record of which cards carry the publisher's photograph and which carry ours.

A COMPANY'S OWN LOGO COMES BEFORE ALL OF THAT. A story the judge says is
mainly about one company (``company_site`` in its metadata) is given that
company's mark by ``news_logos`` before the publisher is asked for anything:
an OpenAI story wearing the OpenAI mark says what it is about at a glance,
which neither a stock photograph nor a drawn desk does. And a story about
nobody in particular whose publisher gives no picture is given the
PUBLISHER'S logo before it falls back to the pool. The order, then:

    company logo  ->  publisher's picture  ->  publisher's logo  ->  house cover

The house cover is the last resort, not the normal case, which is what the
owner asked for in as many words: no generic cover everywhere.

THE BYTES ARE STORED LOCALLY, under the same ``trend_thumbs`` asset type a
competitor post uses, so ``GET /api/trends/posts/{id}/thumb`` serves a story
exactly as it serves a post and the frontend needed no new route. Storing rather
than hot-linking also means the card keeps working when the publisher moves the
file, and it costs one download per story per fortnight.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import config
from .news_logos import fetch_logo, host_of, image_ext, read_capped as _read_capped
from .storage import asset_local_path, relativise, resolve_local
from . import trend_store as store
from .vertex_client import gemini_image

log = logging.getLogger("archivist.news_thumbs")

# A browser User-Agent, and it is load-bearing rather than impolite. Measured on
# the live feed: developer-tech.com, forbes.com and washingtonpost.com all
# answer 403 to a bare client and 200 to this. We are fetching one page that a
# reader is about to open anyway, once per story, and taking only the <head>.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# A slow publisher must not hold up a morning. Short enough that a whole batch
# of ten finishes inside a tick even if every one of them stalls.
PAGE_TIMEOUT_SEC = 12.0
IMAGE_TIMEOUT_SEC = 15.0

# The tags live in <head>. Reading the first slice rather than the whole
# document keeps a 2MB article page from becoming 2MB of memory for one string.
MAX_HTML_BYTES = 512 * 1024
# Past this it is not a thumbnail. A 4/5 card renders about 400px wide.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# How many pages to fetch at once. Every one is a different host, so this is
# politeness to nobody in particular and protection for the event loop.
CONCURRENCY = 4

# How many rows one backfill pass will try. A tick is every five minutes and a
# morning brings ten stories, so this clears a backlog in a few ticks without
# ever becoming a crawl.
BACKFILL_LIMIT = 12

# Where the house covers live. A SEPARATE asset type from `trend_thumbs` on
# purpose: `local_thumb_path` is the only column either kind writes to, so the
# directory name is the one durable record of which covers are the publisher's
# photograph and which are ours. Nothing has to be migrated to ask that question
# later, and no card can lose the answer.
GENERATED_ASSET_TYPE = "news_cover_art"

# How many covers the pool holds. Ten is enough that a screen of stories does
# not obviously repeat, and small enough that filling it costs well under a
# dollar ONCE, after which every story that needs one is free forever.
COVER_POOL_SIZE = 10

# How many to draw in a single pass. Filling an empty pool takes three ticks
# rather than one burst against the shared meter, and a tick is five minutes.
MAX_GENERATED_PER_PASS = 4

# The card tile is `aspect-[4/5]`, so a portrait frame crops least.
GENERATED_ASPECT = "3:4"

# Every image type a cover may legitimately be on disk.
POOL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

# In preference order. `og:image` first because it is the one publishers
# actually maintain; `twitter:image` is the common second; `link rel=image_src`
# is old but still emitted by some CMSes and costs nothing to try.
_IMAGE_PATTERNS = (
    re.compile(
        r'<meta[^>]+?property=["\']og:image(?::url|:secure_url)?["\'][^>]*?'
        r'content=["\']([^"\']+)["\']', re.I | re.S),
    re.compile(
        r'<meta[^>]+?content=["\']([^"\']+)["\'][^>]*?'
        r'property=["\']og:image(?::url|:secure_url)?["\']', re.I | re.S),
    re.compile(
        r'<meta[^>]+?name=["\']twitter:image(?::src)?["\'][^>]*?'
        r'content=["\']([^"\']+)["\']', re.I | re.S),
    re.compile(
        r'<link[^>]+?rel=["\']image_src["\'][^>]*?href=["\']([^"\']+)["\']',
        re.I | re.S),
)

# Data URIs and SVGs are not photographs. An SVG is nearly always a logo or a
# placeholder, which on a 4/5 tile looks worse than the tinted fallback it would
# be replacing.
_BAD_IMAGE_SUFFIXES = (".svg", ".svgz")


def find_image_url(html: str, page_url: str) -> Optional[str]:
    """The publisher's own preview image, absolute. None when there is none.

    Pure, so the tests drive it with fixture HTML and no network.
    """
    for pattern in _IMAGE_PATTERNS:
        m = pattern.search(html)
        if not m:
            continue
        raw = (m.group(1) or "").strip()
        if not raw or raw.startswith("data:"):
            continue
        # Protocol-relative, root-relative and fully-relative all appear in the
        # wild; `urljoin` handles all three and leaves an absolute URL alone.
        absolute = urljoin(page_url, raw)
        scheme = urlsplit(absolute).scheme
        if scheme not in ("http", "https"):
            continue
        if urlsplit(absolute).path.lower().endswith(_BAD_IMAGE_SUFFIXES):
            continue
        return absolute
    return None


async def _save_image(client: httpx.AsyncClient, image_url: str) -> Optional[str]:
    """Download one image into local storage; return its project-relative path."""
    url_hash = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:32]
    # Named .jpg up front and renamed below once the bytes say otherwise: the
    # cache hit has to be answerable without a download.
    dest = asset_local_path("trend_thumbs", url_hash, "jpg")
    for ext in ("png", "webp", "gif"):
        other = asset_local_path("trend_thumbs", url_hash, ext)
        if other.is_file() and other.stat().st_size > 0:
            return relativise(other)
    if dest.is_file() and dest.stat().st_size > 0:
        # Two stories can share a cover (a publisher's default social card), and
        # a story re-found after a prune is the same URL again.
        return relativise(dest)

    async with client.stream("GET", image_url, timeout=IMAGE_TIMEOUT_SEC) as r:
        r.raise_for_status()
        kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        # A consent wall answers 200 with text/html. Writing that to a .jpg is
        # how a card ends up with a broken-image glyph instead of a clean tile.
        if not kind.startswith("image/"):
            log.debug("[news] %s is %s, not an image", image_url, kind or "untyped")
            return None
        data = await _read_capped(r, MAX_IMAGE_BYTES)

    if len(data) < 512:  # a tracking pixel or an error page
        return None
    dest = asset_local_path("trend_thumbs", url_hash, image_ext(data))
    dest.write_bytes(data)
    return relativise(dest)


async def fetch_cover(url: str, *, client: Optional[httpx.AsyncClient] = None
                      ) -> Optional[str]:
    """The story's cover image, cached locally. None whenever anything is off.

    Never raises. A dead link, a bot wall, a consent page, a timeout and an
    article that simply has no picture all land here as None, which is the
    same answer the card already handled.
    """
    if not url:
        return None

    owned = client is None
    c = client or httpx.AsyncClient(
        timeout=PAGE_TIMEOUT_SEC, follow_redirects=True, headers=_HEADERS)
    try:
        async with c.stream("GET", url, timeout=PAGE_TIMEOUT_SEC) as r:
            if r.status_code >= 400:
                log.debug("[news] %s answered %d", url, r.status_code)
                return None
            kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if kind and not kind.startswith(("text/html", "application/xhtml")):
                return None
            html = (await _read_capped(r, MAX_HTML_BYTES)).decode(
                r.encoding or "utf-8", errors="replace")
            final_url = str(r.url)

        image_url = find_image_url(html, final_url)
        if not image_url:
            return None
        return await _save_image(c, image_url)
    except Exception:  # noqa: BLE001 - a missing picture is never an error
        log.debug("[news] no cover for %s", url, exc_info=True)
        return None
    finally:
        if owned:
            await c.aclose()


# The pool's briefs. Ten different rooms, desks and screens, each one a scene a
# story about AI, code, school or work could plausibly sit beside, and NONE of
# them about any particular story. Generic is the point: a house card must not
# look like a photograph OF the headline above it.
#
# Kept as a literal list rather than generated, so what the pool contains is
# readable here and a brief can be swapped without anybody guessing what the
# eleventh one would have been.
COVER_BRIEFS: tuple[str, ...] = (
    "an open laptop on a tidy desk beside a coffee cup and a small plant",
    "a classroom whiteboard covered in simple diagrams and arrows",
    "a stack of books next to a tablet showing a chart",
    "an abstract network of connected dots and soft lines",
    "a desk lamp lighting a keyboard and a notebook at night",
    "a hand drawing a flowchart on paper with a pencil",
    "a row of screens showing simple graphs and code brackets",
    "a paper plane flying over a city skyline of simple blocks",
    "a magnifying glass over a grid of small square tiles",
    "a plant growing out of an open book beside a computer mouse",
)


# `image_ext` lives in `news_logos` now and is imported above: the extension
# has to match the CONTENT because the route that serves these reads the type
# off the name, and both this module and the logo store need the same answer.


def cover_prompt(scene: str) -> str:
    """The whole brief for one house cover. Pure, so a test can read it.

    THREE RULES AND EACH ONE IS LOAD-BEARING:

    - FLAT ILLUSTRATION, NEVER A PHOTOGRAPH. These sit on a card next to a real
      headline and a real link. Anything photographic would be read as a
      photograph OF the story, and it is not one.
    - NO TEXT. Image models render words badly, and any word they got right
      would be a second headline competing with the real one underneath.
    - NO REAL PEOPLE, LOGOS OR BRANDS. A recognisable face or wordmark on an
      invented image is the one way this could misrepresent somebody.
    """
    return (
        "A clean, flat editorial illustration for a news card: "
        f"{scene}.\n\n"
        "Style: simple vector illustration, bold flat shapes, generous empty "
        "space, a calm two or three colour palette, soft geometric composition. "
        "Clearly a drawing, never photographic and never photorealistic.\n"
        "Absolutely no text, no letters, no numbers, no captions, no watermarks.\n"
        "No real people, no recognisable faces, no company logos or brand marks."
    )


def pool_dir() -> Path:
    """The directory the pool lives in. Its own function so a test can move it."""
    return config.asset_root / GENERATED_ASSET_TYPE


def pool() -> list[str]:
    """Every house cover on disk, in a stable order.

    SORTED, and that is not tidiness. The rotation below is an index into this
    list, so an unstable order would hand the same story a different picture on
    every restart.
    """
    root = pool_dir()
    if not root.is_dir():
        return []
    files = sorted(
        (f for f in root.rglob("*")
         if f.is_file() and f.suffix.lower() in POOL_SUFFIXES
         and f.stat().st_size > 512),
        key=lambda f: f.name,
    )
    return [relativise(f) for f in files]


def pick_cover(seed: str, covers: Optional[list[str]] = None, *,
               avoid: Optional[set[str]] = None) -> Optional[str]:
    """Which house cover this story gets. None while the pool is empty.

    Chosen FROM THE STORY, never at random and never round-robin: the same
    headline must land on the same picture every time, or the grid reshuffles
    itself on every refresh and a card people recognised becomes a card they do
    not.

    `avoid` holds the covers already handed out in the same batch. The headline
    still decides where the search STARTS; this only walks on to the next free
    card so two stories on screen together do not wear the same picture. When
    every cover is spoken for the first choice is returned anyway, because a
    repeated illustration beats a blank tile.
    """
    covers = pool() if covers is None else covers
    if not covers:
        return None
    digest = hashlib.sha256(" ".join(str(seed or "").split()).encode("utf-8")).digest()
    start = int.from_bytes(digest[:8], "big") % len(covers)
    if not avoid:
        return covers[start]
    for step in range(len(covers)):
        candidate = covers[(start + step) % len(covers)]
        if candidate not in avoid:
            return candidate
    return covers[start]


async def fill_pool(target: int = COVER_POOL_SIZE, *,
                    max_new: int = MAX_GENERATED_PER_PASS) -> int:
    """Top the pool up to `target`. Returns how many were drawn this call.

    Idempotent and cheap once full: a full pool is one directory listing and no
    model call at all, which is what makes it safe to call on every tick.

    EDITING A BRIEF DOES NOT REDRAW IT. The count check below short-circuits a
    full pool before any brief is looked at, so a reworded scene is only drawn
    if the pool is also short. That is the right trade for something called
    every five minutes; to force one, delete the file and let the next tick
    refill the gap.
    """
    have = len(pool())
    if have >= target:
        return 0

    drawn = 0
    for scene in COVER_BRIEFS[:target]:
        if drawn >= max_new or have + drawn >= target:
            break
        key = hashlib.sha256(scene.encode("utf-8")).hexdigest()[:32]
        if any(asset_local_path(GENERATED_ASSET_TYPE, key, ext).is_file()
               and asset_local_path(GENERATED_ASSET_TYPE, key, ext).stat().st_size > 512
               for ext in ("png", "jpg", "webp", "gif")):
            continue
        try:
            data = await gemini_image(cover_prompt(scene), aspect_ratio=GENERATED_ASPECT)
        except Exception:  # noqa: BLE001 - a thinner pool is not an error
            log.warning("[news] could not draw a house cover", exc_info=True)
            break  # no budget or no credentials: the next brief fails the same way
        if not data or len(data) < 512:
            continue
        dest = asset_local_path(GENERATED_ASSET_TYPE, key, image_ext(data))
        dest.write_bytes(data)
        drawn += 1
        log.info("[news] drew a house cover: %s", scene)
    return drawn


async def _covers_in_use() -> set[str]:
    """House covers already showing on a news card.

    Best effort: if this cannot be read, the batch still gets covers, it just
    loses the guarantee that it will not repeat one. A picture nobody can see
    beats no picture at all.
    """
    try:
        return set(await store.news_house_covers_in_use(GENERATED_ASSET_TYPE))
    except Exception:  # noqa: BLE001 - a nicety, never a blocker
        log.warning("[news] could not read which covers are in use", exc_info=True)
        return set()


async def add_covers(stories: list[tuple], *,
                     max_generated: int = MAX_GENERATED_PER_PASS,
                     publisher_logo: bool = True) -> dict:
    """Give a cover to each (post id, article url, headline[, company site]).

    Returns ``{"logo": n, "found": n, "outlet": n, "house": n, "none": n}``:
    every outcome named, because "8 stories have pictures" and "8 stories have
    one of our house cards" are different facts and the log must be able to
    tell them apart.

    THE ORDER IS THE POINT, per story and in parallel across the batch:

      logo    the company the story is about, when the judge named one
      found   the publisher's own picture of the story
      outlet  the publisher's logo, when it gave no picture
      house   a card from the pool, and only for what is still blank

    A company logo comes before the publisher's photograph because it says
    what the story is about at a glance; a publisher's logo comes after the
    photograph because a real picture of the story beats a masthead. Both
    logos are one request per DOMAIN, cached, so the second OpenAI story and
    the tenth TechRadar story cost nothing.

    ``publisher_logo=False`` skips the outlet step: for a competitor post the
    "publisher" is instagram.com, and its logo is not a picture of the post.
    """
    if not stories:
        return {"logo": 0, "found": 0, "outlet": 0, "house": 0, "none": 0}

    items: list[tuple[int, str, str, str]] = [
        (int(s[0]), str(s[1] or ""), str(s[2] or ""),
         str(s[3] or "") if len(s) > 3 else "")
        for s in stories
    ]

    gate = asyncio.Semaphore(CONCURRENCY)

    async def save(post_id: int, rel: str) -> bool:
        try:
            await store.set_local_thumb(post_id, rel)
            return True
        except Exception:  # noqa: BLE001 - one row is not the batch
            log.exception("[news] could not record the cover for post %d", post_id)
            return False

    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT_SEC, follow_redirects=True, headers=_HEADERS
    ) as client:
        async def resolve(post_id: int, url: str, company_site: str
                          ) -> tuple[str, Optional[str]]:
            async with gate:
                if company_site:
                    rel = await fetch_logo(company_site, client=client)
                    if rel:
                        return "logo", rel
                rel = await fetch_cover(url, client=client)
                if rel:
                    return "found", rel
                publisher = host_of(url) if publisher_logo else ""
                if publisher:
                    rel = await fetch_logo(publisher, client=client)
                    if rel:
                        return "outlet", rel
                return "none", None

        fetched = await asyncio.gather(
            *(resolve(pid, url, site) for pid, url, _, site in items),
            return_exceptions=True,
        )

    out = {"logo": 0, "found": 0, "outlet": 0, "house": 0, "none": 0}
    unresolved: list[tuple[int, str]] = []
    for (post_id, _url, headline, _site), got in zip(items, fetched):
        how, rel = got if isinstance(got, tuple) else ("none", None)
        if isinstance(rel, str) and rel and await save(post_id, rel):
            out[how] += 1
        else:
            unresolved.append((post_id, headline))

    if not unresolved:
        log.info("[news] covers: %d company logos, %d from the publisher, "
                 "%d publisher logos", out["logo"], out["found"], out["outlet"])
        return out

    # Top the pool up only when something actually needs it, then read the list
    # ONCE for the whole batch: `pick_cover` would otherwise re-list the
    # directory per story, and a pool that grew mid-loop would hand two stories
    # in the same batch pictures chosen from different-length lists.
    await fill_pool(max_new=max_generated)
    covers = pool()

    # The headline picks the starting card; a card already spoken for steps to
    # the next free one. Without that step, five stories over ten covers collide
    # about as often as not (it is the birthday problem, not bad luck), and two
    # identical illustrations side by side in a grid reads as a bug.
    #
    # SEEDED FROM WHAT IS ALREADY ON SCREEN, not just from this batch. Batch-
    # local was the first version and it was not enough: stories arrive a few at
    # a time, so the second batch re-picked covers the first batch was already
    # using. Measured on the live feed, seven house covers were five distinct
    # pictures. Probing costs nothing because a story is assigned once and then
    # never again.
    taken: set[str] = set(await _covers_in_use())
    for post_id, headline in unresolved:
        chosen = pick_cover(headline, covers, avoid=taken)
        if chosen and await save(post_id, chosen):
            taken.add(chosen)
            out["house"] += 1
        else:
            out["none"] += 1

    log.info("[news] covers: %d company logos, %d from the publisher, %d publisher "
             "logos, %d house, %d still blank",
             out["logo"], out["found"], out["outlet"], out["house"], out["none"])
    return out


async def backfill(limit: int = BACKFILL_LIMIT) -> int:
    """Give covers to stories that have none yet. Returns how many landed.

    Called on the poller's tick, so it covers both the morning's new stories and
    every story that predates this module, a few at a time, with no separate
    schedule to reason about.

    A STORY IS ONLY EVER TRIED ONCE. It leaves here with a picture either way,
    the publisher's or one of ours, so it no longer matches "has no cover" and
    is not seen again. That is deliberate: a card with a house cover looks
    finished, and re-asking a publisher that already refused us, every five
    minutes, for a picture nobody is waiting for, is not worth the requests.
    """
    try:
        rows = await store.news_without_thumb(limit=limit)
    except Exception:  # noqa: BLE001
        log.exception("[news] could not list stories without a cover")
        return 0
    targets = [
        (int(r["id"]), str(r.get("permalink") or ""), str(r.get("caption") or ""),
         str(((r.get("metadata") or {}) if isinstance(r.get("metadata"), dict)
              else {}).get("company_site") or ""))
        for r in rows
        if r.get("id") is not None and r.get("permalink")
    ]
    result = await add_covers(targets)
    return result["logo"] + result["found"] + result["outlet"] + result["house"]


async def _save_direct(client: httpx.AsyncClient, url: Optional[str]) -> Optional[str]:
    """A post's own image URL, cached locally. None when it is not a picture.

    An expired Instagram CDN link (403) and a reel's `media_url` (a video, which
    `_save_image` refuses by content type) are the normal failures here.
    """
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return None
    try:
        return await _save_image(client, str(url))
    except Exception:  # noqa: BLE001 - a dead link is a missing picture
        log.debug("[trends] direct thumbnail failed for %s", str(url)[:120], exc_info=True)
        return None


async def backfill_competitors(limit: int = BACKFILL_LIMIT) -> int:
    """Give every Competitors / Sentiment card a picture. Returns how many landed.

    The news cards' promise, applied to competitor posts: no card is left as an
    imageless tile. A post qualifies when its cached thumbnail is missing, either
    because the scan could not download it (the CDN link had already expired, or
    the only URL was a reel's video) or because the file is gone from disk.

    In order of preference:
      1. the post's own image, `thumbnail_url` then `media_url`;
      2. the post page's preview image (`og:image` on the permalink);
      3. a house cover from the pool.
    Steps 2 and 3 are exactly `add_covers`, so a competitor post without a
    picture ends up where a news story without one does.
    """
    try:
        rows = await store.recent_competitor_thumbs()
    except Exception:  # noqa: BLE001
        log.exception("[trends] could not list competitor posts for covers")
        return 0
    targets = [
        r for r in rows
        if r.get("id") is not None
        and not (r.get("local_thumb_path")
                 and resolve_local(str(r["local_thumb_path"])).is_file())
    ][:limit]
    if not targets:
        return 0

    direct = 0
    unresolved: list[tuple[int, str, str]] = []
    async with httpx.AsyncClient(
        timeout=PAGE_TIMEOUT_SEC, follow_redirects=True, headers=_HEADERS
    ) as client:
        for r in targets:
            rel = (await _save_direct(client, r.get("thumbnail_url"))
                   or await _save_direct(client, r.get("media_url")))
            if rel:
                try:
                    await store.set_local_thumb(int(r["id"]), rel)
                    direct += 1
                    continue
                except Exception:  # noqa: BLE001 - one row is not the batch
                    log.exception("[trends] could not record the thumbnail for post %s", r["id"])
            unresolved.append(
                (int(r["id"]), str(r.get("permalink") or ""), str(r.get("caption") or "")))

    result = (await add_covers(unresolved, publisher_logo=False) if unresolved
              else {"logo": 0, "found": 0, "outlet": 0, "house": 0})
    log.info("[trends] competitor pictures: %d own image, %d page preview, %d house",
             direct, result["found"], result["house"])
    return direct + result["found"] + result["house"]
