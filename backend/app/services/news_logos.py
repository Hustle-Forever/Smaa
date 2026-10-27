"""Industry Signal - the logo on a card that is about a company.

WHY. Every news card with no publisher picture wore one of ten drawn house
covers, and the owner's word for that was "our generic logo everywhere". A
story about OpenAI is about OpenAI, and the one picture that says so without
claiming anything else is OpenAI's own mark. So a story the judge says is
mainly about one company (`company_site` in its metadata) gets that company's
logo, and a story about nobody in particular gets its publisher's picture and,
failing that, its publisher's logo. The house cover is the last resort now
rather than the normal case.

WHERE A LOGO COMES FROM, in order:

  1. The company's own website. Every site that cares about how it looks in a
     phone's home screen ships an ``apple-touch-icon`` (180px or larger, square,
     the mark on its own) and often a large ``rel=icon`` PNG. That is the
     company's own, current, self-chosen mark, fetched from the one place it is
     guaranteed to be right.
  2. A public favicon lookup, when the site blocks bots or ships only an SVG.
     It answers with a PNG for nearly any domain, and with a tiny generic globe
     for a domain it has never seen, which is why the size floor below exists.

WHAT THIS IS NOT. It never decides what is TRUE about a story: the judge names
the company, and this module fetches a decoration for that name. A logo that
fails to arrive costs a card its logo and nothing else, exactly as a missing
publisher picture does in `news_thumbs`. Every path returns None.

ONE FILE PER DOMAIN, shared by every story about that company, under its own
asset type so the path on a row is the durable record of which cards carry a
brand mark. The frontend reads that record to draw the tile differently: a
logo is shown small and centred on a tinted card, never stretched to fill it.
"""
from __future__ import annotations

import hashlib
import logging
import re
import struct
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from .storage import asset_local_path, relativise

log = logging.getLogger("archivist.news_logos")

# Its own asset type, next to `trend_thumbs` (publisher pictures) and
# `news_cover_art` (house covers). The directory name is the record.
LOGO_ASSET_TYPE = "news_brand_logos"

# A slow homepage must not hold up a batch of covers. These are decorations.
PAGE_TIMEOUT_SEC = 10.0
IMAGE_TIMEOUT_SEC = 10.0

# The <link> tags live in <head>; a homepage can be megabytes past that.
MAX_HTML_BYTES = 256 * 1024
# A logo is a small square. Anything past this is not one.
MAX_IMAGE_BYTES = 2 * 1024 * 1024

# THE SIZE FLOOR IS THE WHOLE GUARD AGAINST A GENERIC GLOBE. The fallback
# service answers an unknown domain with a default icon 16 pixels wide. A PNG
# says its width in its first 24 bytes, so a PNG is judged on that and on
# nothing else: Microsoft's mark is four flat squares and compresses to 426
# bytes at 128 pixels, and a byte floor threw it away on the live feed. Only
# a format whose size cannot be read cheaply falls back to the byte floor.
MIN_IMAGE_BYTES = 1024
MIN_LOGO_PX = 64

# Keyless, public, and answers a PNG for nearly any domain. The `sz` is a
# request, not a promise: it returns the largest it has, which for a well-known
# company is the 256px mark and for an unknown one is the default globe the
# floors above reject.
FALLBACK_URL = "https://www.google.com/s2/favicons?domain={domain}&sz=256"

# The same browser User-Agent `news_thumbs` uses, for the same reason: a bare
# client is answered 403 by half the web, and this is one page per company.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Every image type a logo may be on disk.
LOGO_SUFFIXES = ("png", "jpg", "webp", "gif")

_LINK_RE = re.compile(r"<link\b[^>]*>", re.I | re.S)
_ATTR_RE = re.compile(r'([a-z-]+)\s*=\s*["\']([^"\']*)["\']', re.I)
_SIZE_RE = re.compile(r"(\d+)x(\d+)", re.I)
_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)

# What a file's first bytes say it is. The extension has to match the CONTENT,
# because the route that serves these reads the type off the name: a PNG saved
# as `.jpg` is answered as image/jpeg and renders as a blank tile.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF8", "gif"),
)


def image_ext(data: bytes) -> str:
    """The file extension these bytes deserve. Defaults to jpg."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def png_width(data: bytes) -> Optional[int]:
    """A PNG's width from its header, or None when these bytes are not a PNG.

    The IHDR chunk is mandatory and first, so the width is always big-endian
    at bytes 16 to 20. Pure, and it is the cheapest possible answer to "is this
    a real mark or the 16px default".
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        return None
    if data[12:16] != b"IHDR":
        return None
    return struct.unpack(">I", data[16:20])[0]


def host_of(raw: str) -> str:
    """A bare host from a link, a host or a company's website as typed.

    "https://www.OpenAI.com/blog" -> "openai.com", "anthropic.com" ->
    "anthropic.com", "not a site" -> "". Lowercased, `www.` dropped, and only
    something shaped like a host survives: the judge writes this field and a
    sentence in it must become nothing rather than a request to nowhere.
    """
    text = " ".join(str(raw or "").split()).lower()
    if not text or " " in text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlsplit(text).netloc
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host if _HOST_RE.match(host) else ""


def _size_of(sizes: str) -> int:
    """The largest side declared in a `sizes` attribute. 0 when it says nothing."""
    best = 0
    for m in _SIZE_RE.finditer(sizes or ""):
        best = max(best, int(m.group(1)), int(m.group(2)))
    return best


def find_icon_url(html: str, page_url: str) -> Optional[str]:
    """The site's own icon, the largest one it declares, absolute. None if none.

    Pure, so the tests drive it with fixture HTML and no network.

    `apple-touch-icon` with no `sizes` is 180px by convention and an unsized
    `rel=icon` is the 32px favicon, which is what decides between them when a
    page declares both without numbers. SVG and ICO are skipped: an SVG cannot
    be checked for size or served as a plain picture, and an ICO is nearly
    always the 16px favicon, which is the blurry smudge this whole module is
    trying not to put on a card.
    """
    best: Optional[tuple[tuple[int, int], str]] = None
    for tag in _LINK_RE.findall(html or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        rel = attrs.get("rel", "").lower().split()
        href = attrs.get("href", "").strip()
        if not href or href.startswith("data:"):
            continue
        if "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            rank, default_px = 2, 180
        elif "icon" in rel:
            rank, default_px = 1, 32
        else:
            continue
        absolute = urljoin(page_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.path.lower().endswith((".svg", ".svgz", ".ico")):
            continue
        size = _size_of(attrs.get("sizes", "")) or default_px
        key = (size, rank)
        if best is None or key > best[0]:
            best = (key, absolute)
    return best[1] if best else None


async def read_capped(response: httpx.Response, cap: int) -> bytes:
    """Up to `cap` bytes of a streamed body, then stop asking for more."""
    out = bytearray()
    async for chunk in response.aiter_bytes():
        out.extend(chunk)
        if len(out) >= cap:
            break
    return bytes(out[:cap])


def _key(host: str) -> str:
    return hashlib.sha256(host.encode("utf-8")).hexdigest()[:32]


def cached_logo(host: str) -> Optional[str]:
    """The logo already on disk for this host, or None. No network."""
    key = _key(host)
    for ext in LOGO_SUFFIXES:
        p = asset_local_path(LOGO_ASSET_TYPE, key, ext)
        # Anything on disk passed `looks_like_a_logo` when it was written.
        if p.is_file() and p.stat().st_size > 0:
            return relativise(p)
    return None


def looks_like_a_logo(data: bytes) -> bool:
    """Big enough to be a mark rather than a favicon or a default globe.

    A PNG is judged on the width it declares, whatever it weighs. Anything
    else is judged on bytes, which is a cruder floor but the only cheap one.
    """
    if not data:
        return False
    width = png_width(data)
    if width is not None:
        return width >= MIN_LOGO_PX
    return len(data) >= MIN_IMAGE_BYTES


async def _download(client: httpx.AsyncClient, url: str, host: str) -> Optional[str]:
    """One image into the logo store. None for anything that is not a logo."""
    async with client.stream("GET", url, timeout=IMAGE_TIMEOUT_SEC) as r:
        if r.status_code >= 400:
            return None
        kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not kind.startswith("image/"):
            return None
        data = await read_capped(r, MAX_IMAGE_BYTES)
    if not looks_like_a_logo(data):
        return None
    dest = asset_local_path(LOGO_ASSET_TYPE, _key(host), image_ext(data))
    dest.write_bytes(data)
    return relativise(dest)


async def fetch_logo(domain: str, *, client: Optional[httpx.AsyncClient] = None
                     ) -> Optional[str]:
    """This company's logo, cached locally. None whenever anything is off.

    Never raises. Reads the cache first, so the second story about OpenAI costs
    nothing; then the site's own icon; then the public lookup. A site that
    blocks bots, ships only an SVG, times out or has no icon at all lands on
    the fallback, and a fallback that answers with the default globe is
    rejected by the size floors and lands on None.
    """
    host = host_of(domain)
    if not host:
        return None
    hit = cached_logo(host)
    if hit:
        return hit

    owned = client is None
    c = client or httpx.AsyncClient(
        timeout=PAGE_TIMEOUT_SEC, follow_redirects=True, headers=_HEADERS)
    try:
        icon: Optional[str] = None
        try:
            async with c.stream("GET", f"https://{host}/", timeout=PAGE_TIMEOUT_SEC) as r:
                kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if r.status_code < 400 and (not kind or kind.startswith(
                        ("text/html", "application/xhtml"))):
                    html = (await read_capped(r, MAX_HTML_BYTES)).decode(
                        r.encoding or "utf-8", errors="replace")
                    icon = find_icon_url(html, str(r.url))
        except Exception:  # noqa: BLE001 - the fallback is the next line
            log.debug("[news] could not read the homepage of %s", host, exc_info=True)

        if icon:
            try:
                got = await _download(c, icon, host)
                if got:
                    return got
            except Exception:  # noqa: BLE001
                log.debug("[news] the icon of %s did not download", host, exc_info=True)

        try:
            return await _download(c, FALLBACK_URL.format(domain=host), host)
        except Exception:  # noqa: BLE001
            log.debug("[news] no logo for %s", host, exc_info=True)
            return None
    finally:
        if owned:
            await c.aclose()
