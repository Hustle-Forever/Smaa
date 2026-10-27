"""HTML body extraction + asset-tag walking.

Trafilatura for body text (best for marketing-site heuristics); BeautifulSoup
for the asset walk. Tracking-pixel filter is conservative — better to miss a
borderline asset than burn vision API spend on a 1×1 GIF.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from .hashing import canonicalize_url, vimeo_canonical, youtube_canonical


TRACKING_HOST_PATTERNS = re.compile(
    r"(facebook\.com/tr|googletagmanager\.com|google-analytics\.com|"
    r"doubleclick\.net|linkedin\.com/px|hotjar\.com|segment\.io|"
    r"mixpanel\.com|/pixel\.gif|/spacer\.gif|1x1\.(png|gif))",
    re.I,
)


def is_tracking_pixel(src: str, width: int | None, height: int | None) -> bool:
    if src.startswith("data:"):
        return True
    if TRACKING_HOST_PATTERNS.search(src):
        return True
    if width is not None and height is not None and width <= 4 and height <= 4:
        return True
    return False


def _int_or_none(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip().rstrip("px")
    try:
        return int(float(s))
    except ValueError:
        return None


def extract_body_text(html: str) -> str:
    """Trafilatura first, BS4 strip fallback."""
    try:
        import trafilatura
        out = trafilatura.extract(
            html,
            favor_recall=True,
            deduplicate=True,
            include_links=False,
            include_comments=False,
            include_tables=False,
        )
        if out:
            return out.strip()
    except Exception:
        pass

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_title(html: str) -> str | None:
    m = TITLE_RE.search(html)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title[:500] or None


@dataclass
class ExtractedAsset:
    asset_type: str          # 'image' | 'video_native' | 'video_youtube' | 'video_vimeo' | 'instagram' | 'pdf' | 'iframe_other'
    source_url: str
    canonical_url: str
    metadata: dict


def parse_assets_from_html(html: str, base_url: str) -> list[ExtractedAsset]:
    soup = BeautifulSoup(html, "lxml")
    out: list[ExtractedAsset] = []
    seen: set[str] = set()

    def add(asset_type: str, src: str, meta: dict) -> None:
        try:
            canon = canonicalize_url(src, base_url=base_url)
        except Exception:
            return
        if not canon or canon in seen:
            return
        seen.add(canon)
        out.append(ExtractedAsset(
            asset_type=asset_type,
            source_url=src,
            canonical_url=canon,
            metadata=meta,
        ))

    # --- <img> ---
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        w = _int_or_none(img.get("width"))
        h = _int_or_none(img.get("height"))
        if is_tracking_pixel(src, w, h):
            continue
        add("image", src, {
            "alt": (img.get("alt") or "").strip()[:500] or None,
            "width_attr": w,
            "height_attr": h,
            "surrounding_text": _surrounding_text(img),
        })

    # --- <video> + nested <source> ---
    for video in soup.find_all("video"):
        sources = video.find_all("source")
        if sources:
            for s in sources:
                src = s.get("src")
                if src:
                    add("video_native", src, {"poster": video.get("poster")})
        else:
            src = video.get("src")
            if src:
                add("video_native", src, {"poster": video.get("poster")})

    # --- <iframe> ---
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src") or ""
        if not src:
            continue
        host = _host_of(src, base_url)
        meta = {"iframe_title": (iframe.get("title") or "").strip()[:200] or None}
        if "youtube.com" in host or "youtu.be" in host:
            canon = youtube_canonical(src) or src
            add("video_youtube", src, {**meta, "canonical": canon})
        elif "vimeo.com" in host:
            canon = vimeo_canonical(src) or src
            add("video_vimeo", src, {**meta, "canonical": canon})
        elif "instagram.com" in host:
            add("instagram", src, meta)
        else:
            add("iframe_other", src, {**meta, "iframe_host": host})

    # --- <a href="*.pdf"> ---
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.pdf(\?|#|$)", href, re.I):
            add("pdf", href, {"link_text": (a.get_text(" ", strip=True) or "")[:200] or None})

    # --- bare YT/Vimeo/Instagram links (not in iframes) ---
    for a in soup.find_all("a", href=True):
        href = a["href"]
        host = _host_of(href, base_url)
        if "youtube.com" in host or "youtu.be" in host:
            canon = youtube_canonical(href)
            if canon and canon not in seen:
                add("video_youtube", href, {"link_text": (a.get_text(" ", strip=True) or "")[:200] or None})
        elif "vimeo.com" in host:
            canon = vimeo_canonical(href)
            if canon and canon not in seen:
                add("video_vimeo", href, {"link_text": (a.get_text(" ", strip=True) or "")[:200] or None})
        elif "instagram.com" in host and re.search(r"/(p|reel|tv)/[\w-]+", href):
            add("instagram", href, {"link_text": (a.get_text(" ", strip=True) or "")[:200] or None})

    # Override canonical_url for YT/Vimeo using metadata canonical we set
    for asset in out:
        if asset.asset_type in ("video_youtube", "video_vimeo"):
            mc = asset.metadata.get("canonical")
            if mc:
                asset.canonical_url = mc

    return out


def _host_of(url: str, base_url: str) -> str:
    from urllib.parse import urljoin, urlparse
    return urlparse(urljoin(base_url, url)).netloc.lower()


def _surrounding_text(tag: Tag, *, max_chars: int = 200) -> str | None:
    parent = tag.parent
    if parent is None:
        return None
    text = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)).strip()
    if not text:
        return None
    return text[:max_chars]
