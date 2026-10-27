"""Phase 1 — sitemap discovery → website_pages rows.

Probes robots.txt + /sitemap.xml + /sitemap_index.xml, recurses into nested
sitemaps, filters URLs to the target domain, and inserts new rows into
website_pages with source_type='sitemap'.

Uses `requests` (sync) inside `asyncio.to_thread` since we do at most ~10 HTTP
calls during discovery and `requests` is what the legacy code used. The
fetch phase uses async httpx because it makes hundreds of calls.
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests
from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....utils.hashing import is_target_domain, normalize_page_url
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.discovery")

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _fetch_sync(session: requests.Session, url: str) -> tuple[int | None, str | None]:
    try:
        r = session.get(url, timeout=config.request_timeout, allow_redirects=True)
        return r.status_code, r.text
    except requests.RequestException as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None, None


def _parse_robots(robots_text: str) -> list[str]:
    out: list[str] = []
    for line in robots_text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            out.append(line.split(":", 1)[1].strip())
    return out


def _parse_sitemap(
    xml_text: str,
    parent: str,
    session: requests.Session,
    seen: set[str],
) -> list[tuple[str, str]]:
    """Returns list of (page_url, source_sitemap_url). Recurses sitemap indexes."""
    results: list[tuple[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("Sitemap parse error for %s: %s", parent, e)
        return results

    if root.tag.lower().endswith("sitemapindex"):
        for sm in root.findall(f"{SITEMAP_NS}sitemap"):
            loc = sm.find(f"{SITEMAP_NS}loc")
            if loc is None or not loc.text:
                continue
            child = loc.text.strip()
            if child in seen:
                continue
            seen.add(child)
            log.info("  -> nested sitemap: %s", child)
            status, text = _fetch_sync(session, child)
            if status == 200 and text:
                results.extend(_parse_sitemap(text, child, session, seen))
        return results

    for u in root.findall(f"{SITEMAP_NS}url"):
        loc = u.find(f"{SITEMAP_NS}loc")
        if loc is not None and loc.text:
            results.append((loc.text.strip(), parent))
    return results


def _discover_sync(target_url: str) -> tuple[dict[str, str], list[str]]:
    """Returns (url -> source_sitemap_url, list of sitemaps used)."""
    session = requests.Session()
    session.headers.update({"User-Agent": config.user_agent})

    candidates = [
        urljoin(target_url, "/sitemap.xml"),
        urljoin(target_url, "/sitemap_index.xml"),
    ]

    robots_url = urljoin(target_url, "/robots.txt")
    log.info("Fetching %s", robots_url)
    status, text = _fetch_sync(session, robots_url)
    if status == 200 and text:
        for sm in _parse_robots(text):
            if sm not in candidates:
                candidates.append(sm)

    discovered: dict[str, str] = {}
    used: list[str] = []
    seen: set[str] = set()

    for sm_url in candidates:
        if sm_url in seen:
            continue
        seen.add(sm_url)
        log.info("Probing sitemap %s", sm_url)
        status, text = _fetch_sync(session, sm_url)
        if status != 200 or not text:
            log.info("  -> not available (status=%s)", status)
            continue
        used.append(sm_url)
        for url, source in _parse_sitemap(text, sm_url, session, seen):
            discovered.setdefault(url, source)

    return discovered, used


async def run(ctx: PhaseContext) -> PhaseResult:
    """Discover URLs, filter to domain, insert into website_pages."""
    log.info("[run=%s] Discovery: target=%s", ctx.run_id, ctx.target_url)

    # robots + sitemap parsing is small + sync; run off the event loop.
    discovered, sitemaps_used = await asyncio.to_thread(_discover_sync, ctx.target_url)
    kept = {
        u: src for u, src in discovered.items()
        if is_target_domain(u, ctx.target_domain)
    }
    log.info(
        "[run=%s] Discovered %d URLs total; %d in target domain (%s)",
        ctx.run_id, len(discovered), len(kept), ctx.target_domain,
    )

    if ctx.is_cancelled and ctx.is_cancelled():
        raise CancellationError("discovery cancelled before insert")

    inserted = 0
    async with get_conn() as conn:
        # Update scrape_runs with sitemap info
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE scrape_runs SET pages_found_count=%s, sitemap_url=%s WHERE id=%s",
                (len(kept), ",".join(sitemaps_used) if sitemaps_used else None, ctx.run_id),
            )
        for url, src in kept.items():
            try:
                norm = normalize_page_url(url)
                domain = urlparse(url).netloc.lower()
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO website_pages
                            (url, normalized_url, domain, source_type, discovered_from, sitemap_url, metadata)
                        VALUES (%s, %s, %s, 'sitemap', %s, %s, %s)
                        ON CONFLICT (url) DO NOTHING
                        RETURNING id
                        """,
                        (
                            url, norm, domain, "sitemap", src,
                            Jsonb({"discovery": {"method": "sitemap", "sitemap_url": src}}),
                        ),
                    )
                    if await cur.fetchone():
                        inserted += 1
            except Exception:
                log.exception("Insert failed for %s", url)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE scrape_runs SET pages_inserted_count=%s WHERE id=%s",
                (inserted, ctx.run_id),
            )

    log.info(
        "[run=%s] Discovery done: %d discovered, %d new rows",
        ctx.run_id, len(kept), inserted,
    )
    return PhaseResult(
        name="discovery",
        processed=inserted,
        total=len(kept),
        extra={"sitemaps_used": sitemaps_used, "discovered": len(discovered)},
    )
