"""Trend Finder — the scan engine.

Pulls each watched competitor's recent Instagram posts through the Graph API's
``business_discovery``, works out which of them OVERPERFORMED for that account,
and stores them for the AI scoring pass.

Why outperformance rather than likes: engagement doesn't compare across accounts
of different sizes. Measured on real data — @42abudhabi lands ~0.3% of its
followers on a post, @visitabudhabi ~0.05%. A post with 600 likes can be a flop
and a post with 60 can be a hit. So every post is measured against the MEDIAN of
its own account, and "4.7× their average" is the number the UI leads with.

Two Instagram facts this module is built around, both verified live:

  * ``view_count`` exists only on VIDEO. Images and carousels have none, so it
    is never used in the comparable engagement figure — only stored and shown.
  * Many handles cannot be resolved at all. ``business_discovery`` only reaches
    Business/Creator accounts; anything else returns ``Invalid user id``. That
    is a normal outcome, not a bug, and it is reported per-competitor rather
    than failing the whole scan.
"""
from __future__ import annotations

import hashlib
import logging
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from . import instagram_client as ig
from . import trend_store as store
from .storage import asset_local_path, relativise, resolve_local

log = logging.getLogger("archivist.trend_finder")

# How far back a scan looks, and the hard page ceiling per competitor. 50 posts
# per page at ~3s a page, so 4 pages ≈ 12s per account.
DEFAULT_LOOKBACK_DAYS = 120
MAX_PAGES_PER_COMPETITOR = 4
PAGE_SIZE = 50

# The most posts one scan will take from one account, when the person who
# started it did not say. 200 is what the four-page ceiling above already
# allowed, so the default behaviour is exactly what it always was.
#
# A CAP, NOT A TARGET. Reading fewer is a normal outcome: an account that has
# posted eleven times this quarter returns eleven, and that is the right answer,
# not a shortfall.
DEFAULT_MAX_POSTS_PER_ACCOUNT = MAX_PAGES_PER_COMPETITOR * PAGE_SIZE
# Below this many posts an account's median is too noisy to call anything an
# outlier. Outperformance is still reported, but it is damped AND capped so a
# thin sample cannot manufacture a headline number.
MIN_POSTS_FOR_BASELINE = 6
# The most a low-confidence sample is allowed to claim. With three posts whose
# engagements are 1, 1 and 100, the raw ratio is "100x" — which is noise, not a
# hit. Capping keeps such a post visible without letting it outrank a genuine,
# well-measured 4.7x from an account we actually have a baseline for.
THIN_SAMPLE_MAX_CLAIM = 3.0

_THUMB_TIMEOUT = 20.0
_THUMB_ASSET_TYPE = "trend_thumbs"


class TrendScanError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Instagram fetch
# ---------------------------------------------------------------------------

def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        # Graph returns e.g. 2026-07-22T10:15:33+0000
        return datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None


def _trusted_outperformance(raw: float, confidence: float) -> float:
    """Damp and cap a raw engagement ratio by how much baseline we actually have.

    `confidence` is 1.0 once an account has enough posts to have a real median.
    Below that the ratio is shrunk toward 1.0 (no evidence of anything unusual)
    AND capped, because shrinking alone doesn't help: a 100x from three posts is
    still 50x after halving. Pure, so the tests drive it directly."""
    conf = max(0.0, min(float(confidence), 1.0))
    damped = 1.0 + (float(raw) - 1.0) * conf
    if conf >= 1.0:
        return round(damped, 3)
    ceiling = 1.0 + conf * (THIN_SAMPLE_MAX_CLAIM - 1.0)
    return round(min(damped, ceiling), 3)


def _engagement(media: dict) -> int:
    """Likes + comments. Deliberately excludes views: they exist only on video,
    so including them would make the median incomparable across media types."""
    return int(media.get("like_count") or 0) + int(media.get("comments_count") or 0)


async def fetch_recent_media(
    username: str, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_pages: int = MAX_PAGES_PER_COMPETITOR,
    max_posts: int = DEFAULT_MAX_POSTS_PER_ACCOUNT,
) -> tuple[dict, list[dict]]:
    """(account_fields, media[]) for one handle, newest first.

    Stops at whichever comes first: `max_posts`, the lookback cutoff, or the
    page ceiling. `max_posts` is the cap the person set when they pressed the
    button, and because Instagram returns newest first, cutting the list there
    keeps the most recent posts, which are the ones worth scanning."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    after: Optional[str] = None
    account: dict = {}
    out: list[dict] = []

    for page in range(max_pages):
        bd = await ig.fetch_account_media(username, after=after, page_size=PAGE_SIZE)
        if not account:
            account = {k: v for k, v in bd.items() if k != "media"}
        media_block = bd.get("media") or {}
        batch = media_block.get("data") or []
        if not batch:
            break

        hit_cutoff = False
        for m in batch:
            ts = _parse_ts(m.get("timestamp"))
            if ts and ts < cutoff:
                hit_cutoff = True
                continue
            out.append(m)
            if len(out) >= max_posts:
                return account, out[:max_posts]
        if hit_cutoff:
            break

        after = ((media_block.get("paging") or {}).get("cursors") or {}).get("after")
        if not after:
            break
        log.debug("[trends] @%s page %d done (%d kept)", username, page + 1, len(out))

    return account, out


async def _download_thumb(url: str) -> Optional[str]:
    """Fetch a post thumbnail into local storage and return its relative path.

    Instagram CDN URLs expire, so a stored URL eventually renders a broken
    image. The AI scoring pass needs these bytes anyway, so downloading here is
    effectively free and makes the feed permanently viewable."""
    if not url:
        return None
    try:
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        dest: Path = asset_local_path(_THUMB_ASSET_TYPE, url_hash, "jpg")
        if dest.is_file() and dest.stat().st_size > 0:
            return relativise(dest)
        async with httpx.AsyncClient(timeout=_THUMB_TIMEOUT, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.content
        if not data:
            return None
        dest.write_bytes(data)
        return relativise(dest)
    except Exception:  # noqa: BLE001 — a missing thumbnail must never fail a scan
        log.warning("[trends] thumbnail download failed", exc_info=True)
        return None


def _thumb_source(media: dict) -> Optional[str]:
    """Videos carry `thumbnail_url`; images and carousels use `media_url`."""
    return media.get("thumbnail_url") or media.get("media_url")


# ---------------------------------------------------------------------------
# One competitor
# ---------------------------------------------------------------------------

async def scan_one(
    competitor: dict, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_posts: int = DEFAULT_MAX_POSTS_PER_ACCOUNT,
    download_thumbs: bool = True,
) -> dict:
    """Fetch, rank and store one competitor's recent posts.

    Never raises for an unreachable handle — that outcome is recorded on the
    competitor row and returned, so one bad account can't abort a whole scan."""
    username = competitor["username"]
    cid = int(competitor["id"])

    try:
        account, media = await fetch_recent_media(
            username, lookback_days=lookback_days, max_posts=max_posts)
    except ig.InstagramConfigError as exc:
        await store.record_scan_result(cid, status="failed", error=str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 — per-account failure is expected
        msg = _humanise_ig_error(exc, username)
        log.warning("[trends] @%s scan failed: %s", username, msg)
        await store.record_scan_result(cid, status="failed", error=msg)
        return {"username": username, "ok": False, "error": msg, "posts": 0}

    if not media:
        await store.record_scan_result(
            cid, status="empty", error=None, posts_found=0,
            followers_count=account.get("followers_count"),
            media_count=account.get("media_count"),
            profile_picture_url=account.get("profile_picture_url"),
        )
        return {"username": username, "ok": True, "posts": 0,
                "note": "no posts in the lookback window"}

    engagements = [_engagement(m) for m in media]
    median = float(statistics.median(engagements)) if engagements else 0.0
    baseline = median if median > 0 else 1.0
    followers = int(account.get("followers_count") or 0)
    # A median taken from three posts is not a baseline. Rather than trusting or
    # discarding it, shrink every ratio toward 1.0 in proportion to how thin the
    # sample is — so a freshly-added account can't rocket to the top of the feed
    # on a "5x" computed from almost nothing, and gains its full weight once
    # enough posts are in.
    confidence = min(len(media) / MIN_POSTS_FOR_BASELINE, 1.0)
    trustworthy = confidence >= 1.0

    # Pictures this account's posts already have on disk. Each scan hands back
    # newly signed CDN links and the cached file is named after the link, so
    # asking first is what stops a daily scan storing every picture again.
    cached: dict[str, str] = {}
    if download_thumbs:
        try:
            cached = await store.local_thumbs_for([str(m.get("id")) for m in media])
        except Exception:  # noqa: BLE001 - worst case is a re-download
            log.warning("[trends] @%s: could not list cached thumbnails", username,
                        exc_info=True)

    stored = 0
    for m in media:
        eng = _engagement(m)
        row = {
            "ig_media_id": str(m.get("id")),
            "username": username,
            "permalink": m.get("permalink"),
            "media_type": m.get("media_type"),
            "media_product_type": m.get("media_product_type"),
            "media_url": m.get("media_url"),
            "thumbnail_url": m.get("thumbnail_url"),
            "caption": (m.get("caption") or "")[:4000],
            "posted_at": _parse_ts(m.get("timestamp")),
            "like_count": int(m.get("like_count") or 0),
            "comments_count": int(m.get("comments_count") or 0),
            # VIDEO only — never invented for images/carousels.
            "view_count": int(m["view_count"]) if m.get("view_count") is not None else None,
            "engagement": eng,
            "engagement_rate": (eng / followers) if followers else None,
            "outperformance": _trusted_outperformance(eng / baseline, confidence),
            "metadata": {
                "baseline_median": median,
                "baseline_sample": len(media),
                "baseline_confidence": round(confidence, 2),
                "baseline_trustworthy": trustworthy,
                "raw_outperformance": round(eng / baseline, 3),
            },
        }
        try:
            post_id = await store.upsert_post(cid, row)
            stored += 1
        except Exception:  # noqa: BLE001 — one bad row shouldn't stop the rest
            log.exception("[trends] @%s: failed to store media %s", username, m.get("id"))
            continue

        have = cached.get(row["ig_media_id"])
        if download_thumbs and not (have and resolve_local(have).is_file()):
            rel = await _download_thumb(_thumb_source(m) or "")
            if rel:
                await store.set_local_thumb(post_id, rel)

    await store.record_scan_result(
        cid, status="ok", error=None, posts_found=stored,
        median_engagement=median,
        followers_count=account.get("followers_count"),
        media_count=account.get("media_count"),
        profile_picture_url=account.get("profile_picture_url"),
    )
    log.info("[trends] @%s: %d post(s), median engagement %.0f", username, stored, median)
    return {"username": username, "ok": True, "posts": stored, "median": median}


def _humanise_ig_error(exc: Exception, username: str) -> str:
    """Graph API errors are opaque; the common one has a specific cause worth
    explaining, because it is the single biggest source of confusion here."""
    raw = str(exc)
    if "Invalid user id" in raw or "does not exist" in raw:
        return (f"@{username} can't be read by the Instagram API. Business "
                f"Discovery only reaches Business or Creator accounts — personal "
                f"accounts and some brand pages simply aren't reachable.")
    if "rate limit" in raw.lower() or "(4)" in raw:
        return "Instagram rate limit reached — try again in a little while."
    return raw[:300]


# ---------------------------------------------------------------------------
# The scan job
# ---------------------------------------------------------------------------

async def run_scan(
    scan_id: int, competitor_ids: Optional[list[int]] = None,
    *, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_posts: int = DEFAULT_MAX_POSTS_PER_ACCOUNT, score: bool = True,
) -> None:
    """Background job: fetch every selected competitor, then score what's new.

    Owns the scan row's terminal status and never raises — a failure is recorded
    so the UI's poll loop always terminates."""
    try:
        competitors = [
            c for c in await store.list_competitors(status="active")
            if competitor_ids is None or int(c["id"]) in set(competitor_ids)
        ]
        await store.update_scan(
            scan_id, status="fetching", phase="fetching",
            competitors_total=len(competitors),
            progress=f"reading {len(competitors)} account(s)…",
        )
        if not competitors:
            await store.update_scan(
                scan_id, status="done", phase="done",
                progress="no active competitors to scan — add some first",
            )
            return

        found = 0
        for i, comp in enumerate(competitors, start=1):
            await store.update_scan(
                scan_id, competitors_done=i - 1,
                progress=f"reading @{comp['username']} ({i}/{len(competitors)})…",
            )
            try:
                res = await scan_one(comp, lookback_days=lookback_days,
                                     max_posts=max_posts)
                found += int(res.get("posts") or 0)
            except ig.InstagramConfigError as exc:
                await store.update_scan(
                    scan_id, status="failed", phase="failed", error=str(exc),
                    progress="Instagram isn't configured",
                )
                return
            except Exception:  # noqa: BLE001
                log.exception("[trends] unexpected failure on @%s", comp["username"])
            await store.update_scan(scan_id, competitors_done=i, posts_found=found)

        if not score:
            await store.update_scan(
                scan_id, status="done", phase="done",
                progress=f"found {found} post(s)",
            )
            return

        # --- AI scoring -----------------------------------------------------
        # lazy imports keep the module graph acyclic
        from .trend_scoring import ScoringUnavailable, score_pending

        await store.update_scan(
            scan_id, status="scoring", phase="scoring",
            progress="working out which of these 42 could actually make…",
        )

        async def _progress(done: int, total: int) -> None:
            await store.update_scan(
                scan_id, posts_scored=done,
                progress=f"scoring {done}/{total} post(s)…",
            )

        try:
            scored = await score_pending(on_progress=_progress)
            note = f"found {found} post(s), scored {scored}"
        except ScoringUnavailable as exc:
            # The AI is a layer on top, not a dependency: the posts are stored
            # and still rank on outperformance, so this is a DONE scan with a
            # caveat rather than a failure.
            scored = 0
            note = f"found {found} post(s). {exc}"
            log.warning("[trends] scan #%d: %s", scan_id, exc)

        await store.update_scan(
            scan_id, status="done", phase="done", posts_scored=scored,
            progress=note,
        )
        log.info("[trends] scan #%d complete: %d found, %d scored", scan_id, found, scored)
    except Exception as exc:  # noqa: BLE001 — a scan must always terminate
        log.exception("[trends] scan #%d failed", scan_id)
        await store.update_scan(
            scan_id, status="failed", phase="failed", error=str(exc)[:500],
            progress="the scan failed",
        )


async def start_scan(
    competitor_ids: Optional[list[int]] = None, *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_posts: int = DEFAULT_MAX_POSTS_PER_ACCOUNT,
    trigger: str = "manual",
) -> dict:
    """QUEUE a scan and return immediately. One at a time.

    The scan is a DB row, not an in-flight task: a scan takes minutes, and an
    `asyncio.create_task` would neither survive a backend restart nor be safe
    from garbage collection. `workers/trend_scan_poller.py` picks the row up —
    which also means a scan resumes after a redeploy instead of vanishing.

    `trigger` is "manual" for the Scan buttons and "auto" for the poller's own
    daily scan, kept on the row so a scan nobody pressed is explainable."""
    await store.ensure_tables()
    if await store.scan_is_active():
        existing = await store.latest_scan() or {}
        raise TrendScanError(
            f"a scan is already running ({existing.get('progress') or existing.get('status')})"
        )
    active = await store.list_competitors(status="active")
    targets = ([c for c in active if int(c["id"]) in set(competitor_ids)]
               if competitor_ids is not None else active)
    if not targets:
        raise TrendScanError(
            "there are no active competitors to scan — add one, or re-include an "
            "excluded account, first"
        )
    return await store.create_scan(
        len(targets), metadata={"competitor_ids": competitor_ids,
                                "lookback_days": lookback_days,
                                "max_posts": max_posts,
                                "trigger": trigger},
    )
