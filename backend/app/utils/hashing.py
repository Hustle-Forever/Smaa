"""URL canonicalisation, hashing, and platform-specific URL parsers."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse


def canonicalize_url(url: str, base_url: str | None = None) -> str:
    """Lowercase scheme/host, drop fragment, absolutize against base_url."""
    if base_url:
        url = urljoin(base_url, url)
    url, _ = urldefrag(url)
    p = urlparse(url.strip())
    scheme = (p.scheme or "https").lower()
    host = (p.netloc or "").lower()
    return urlunparse((scheme, host, p.path or "/", p.params, p.query, ""))


def url_hash(url: str) -> str:
    """SHA-256 hex digest of the canonical URL — stable per-asset key."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


_YT_PATTERNS = [
    re.compile(r"youtube\.com/embed/([\w\-]+)", re.I),
    re.compile(r"youtube\.com/watch\?[^\"']*v=([\w\-]+)", re.I),
    re.compile(r"youtu\.be/([\w\-]+)", re.I),
]


def youtube_canonical(url: str) -> str | None:
    for pat in _YT_PATTERNS:
        m = pat.search(url)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    return None


_VIMEO_PATTERN = re.compile(r"vimeo\.com/(?:video/)?(\d+)", re.I)


def vimeo_canonical(url: str) -> str | None:
    m = _VIMEO_PATTERN.search(url)
    return f"https://vimeo.com/{m.group(1)}" if m else None


def normalize_page_url(url: str) -> str:
    """Page-level normalisation: drop trailing slash on non-root paths, drop fragment."""
    p = urlparse(url.strip())
    scheme = (p.scheme or "https").lower()
    host = (p.netloc or "").lower()
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    rebuilt = f"{scheme}://{host}{path}"
    if p.query:
        rebuilt += f"?{p.query}"
    return rebuilt


def is_target_domain(url: str, target: str) -> bool:
    host = urlparse(url).netloc.lower()
    target = target.lower().lstrip(".")
    return host == target or host.endswith("." + target)
