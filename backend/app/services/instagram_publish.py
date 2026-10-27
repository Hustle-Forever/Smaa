"""
Instagram content publishing via the Graph API — Producer Roadmap, Step 5 (Approval).

SELF-GATED. ``publish_status()`` reports whether publishing is possible; ``publish()``
returns ``status="skipped"`` (with a human reason) unless a publish-capable token and
a business account id are configured — so there is ZERO risk of an accidental live
post during development.

Instagram's Content Publishing API fetches the media from a PUBLIC URL, so the caller
passes a publicly-reachable `media_url`. We host that on the object store's public
bucket at booking time (see services/approved_media.py), with no tunnel and no
PUBLIC_MEDIA_BASE_URL.

Flow:
  image  → POST /{ig_user}/media {image_url, caption}  → creation_id
  video  → POST /{ig_user}/media {media_type:REELS, video_url, caption} → creation_id
           → poll GET /{creation_id}?fields=status_code until FINISHED
  both   → POST /{ig_user}/media_publish {creation_id} → media_id
           → GET /{media_id}?fields=permalink
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import config

log = logging.getLogger("archivist.ig_publish")

_GRAPH = "https://graph.facebook.com"
_VIDEO_POLL_INTERVAL_SEC = 5
_VIDEO_POLL_MAX_TRIES = 24  # ~2 min for Reels processing

# media_publish can fail with a TRANSIENT "media not ready" (code 9007 /
# subcode 2207027, "Media ID is not available, please wait a moment") even after
# the container reports FINISHED — Instagram needs a short beat to settle before
# it will accept the publish. Retry the publish a few times before giving up.
_PUBLISH_RETRY_WAIT_SEC = 5
_PUBLISH_MAX_TRIES = 6  # ~25s of retries after the first attempt


def _token() -> str:
    return config.instagram_publish_token or config.instagram_access_token or ""


def publish_status() -> tuple[bool, str]:
    """(enabled, reason). reason is empty when enabled.

    The media URL is supplied by the caller (approved_media hosts it on a public
    bucket), so the only requirements here are a publish-capable token and the
    target account id.
    """
    if not _token():
        return False, (
            "Instagram publishing not configured: set INSTAGRAM_ACCESS_TOKEN (or "
            "INSTAGRAM_PUBLISH_TOKEN) with the instagram_content_publish permission."
        )
    if not config.instagram_account_id:
        return False, "Instagram publishing not configured: set INSTAGRAM_ACCOUNT_ID (your IG Business account id)."
    return True, ""


async def fetch_media_metrics(media_ids: list[str]) -> dict:
    """Sum like_count + comments_count across OUR published media ids.

    These are media WE own (published to ``config.instagram_account_id``), so a
    plain node read returns engagement — the same pattern publish() already uses
    to read a permalink. Best-effort: never raises; ids that error are skipped.

    Returns {"total_likes", "total_comments", "counted", "failed",
    "metrics_available"}. ``metrics_available`` is False when publishing isn't
    configured (no token / account) so the UI can show "—" instead of a fake 0.
    """
    ids = [str(m) for m in media_ids if m]
    enabled, _ = publish_status()
    if not enabled or not ids:
        return {
            "total_likes": 0, "total_comments": 0,
            "counted": 0, "failed": len(ids), "metrics_available": enabled,
        }

    ver = config.instagram_api_version
    token = _token()
    total_likes = total_comments = counted = failed = 0
    # Same sequential-loop problem as fetch_media_metrics_per_post; same fix and
    # the same conservative ceiling, for the same reason (shared publish token).
    sem = asyncio.Semaphore(4)

    async def _totals_one(client: "httpx.AsyncClient", media_id: str) -> tuple[int, int] | None:
        async with sem:
            try:
                r = await client.get(
                    f"{_GRAPH}/{ver}/{media_id}",
                    params={"fields": "like_count,comments_count", "access_token": token},
                )
                if r.status_code >= 400:
                    return None
                body = r.json() or {}
                return (
                    int(body.get("like_count") or 0),
                    int(body.get("comments_count") or 0),
                )
            except Exception:
                return None

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for res in await asyncio.gather(
                *(_totals_one(client, str(m)) for m in ids), return_exceptions=True,
            ):
                if not isinstance(res, tuple):
                    failed += 1
                    continue
                total_likes += res[0]
                total_comments += res[1]
                counted += 1
    except Exception:
        log.exception("[ig_publish] fetch_media_metrics failed")

    return {
        "total_likes": total_likes, "total_comments": total_comments,
        "counted": counted, "failed": failed, "metrics_available": True,
    }


# Does this token have insights access at all? Keyed by TOKEN VALUE, because the
# token is rotatable — a cache keyed on anything else would keep a stale verdict
# after a rotation. False means "permission-denied, stop asking": without this,
# every post paid TWO doomed insights requests (`views` then `reach`), which on a
# 60-post account is 120 pointless round trips per dashboard load.
_INSIGHTS_READABLE: dict[str, bool] = {}

# Graph error codes that mean "back off / stop", not "this post has no data":
#   4   application request limit reached
#   17  user request limit reached
#   32  page request limit reached
#   613 calls-per-second limit
_THROTTLE_CODES = {4, 17, 32, 613}


def _graph_error_code(body: dict) -> int | None:
    try:
        return int(((body or {}).get("error") or {}).get("code"))
    except (TypeError, ValueError):
        return None


async def _fetch_media_views(client: "httpx.AsyncClient", ver: str, media_id: str, token: str) -> int | None:
    """Best-effort play/view count via the insights edge. Reels expose ``views``;
    older media may only expose ``reach``. Returns None when neither is readable
    (e.g. the token lacks ``instagram_manage_insights``) so the UI can tell
    "no data" apart from a real zero."""
    # Token already proved it cannot read insights — skip both requests.
    if _INSIGHTS_READABLE.get(token) is False:
        return None
    for metric in ("views", "reach"):
        try:
            ri = await client.get(
                f"{_GRAPH}/{ver}/{media_id}/insights",
                params={"metric": metric, "access_token": token},
            )
            if ri.status_code >= 400:
                body = ri.json() if ri.content else {}
                code = _graph_error_code(body)
                if ri.status_code == 429 or code in _THROTTLE_CODES:
                    # Throttled: give up on insights for this whole run rather
                    # than hammering a rate-limited token that the PUBLISH path
                    # also depends on.
                    _INSIGHTS_READABLE[token] = False
                    return None
                if ri.status_code in (400, 403) and metric == "views":
                    # Likely a permissions problem rather than a per-post gap;
                    # let the `reach` attempt decide before memoising.
                    continue
                if ri.status_code == 403:
                    _INSIGHTS_READABLE[token] = False
                    return None
                continue
            data = (ri.json() or {}).get("data") or []
            if not data:
                continue
            values = data[0].get("values") or []
            if values:
                _INSIGHTS_READABLE[token] = True
                return int(values[0].get("value") or 0)
        except Exception:
            continue
    return None


async def fetch_media_metrics_per_post(media_ids: list[str]) -> dict:
    """Like ``fetch_media_metrics`` but keeps PER-MEDIA engagement so the
    dashboard can chart it. Returns ``{"per_media": {media_id: {"likes",
    "comments", "views"}}, "total_likes", "total_comments", "total_views",
    "counted", "failed", "metrics_available"}``. ``views`` is from the insights
    edge (None when not readable). Best-effort: never raises; ids that error are
    skipped. ``metrics_available`` is False when publishing isn't configured so
    the UI can fall back gracefully."""
    ids = [str(m) for m in media_ids if m]
    enabled, _ = publish_status()
    if not enabled or not ids:
        return {
            "per_media": {}, "total_likes": 0, "total_comments": 0,
            "total_views": 0, "counted": 0, "failed": len(ids),
            "metrics_available": enabled,
        }

    ver = config.instagram_api_version
    token = _token()
    per_media: dict[str, dict] = {}
    total_likes = total_comments = total_views = counted = failed = 0
    # Concurrency 4, deliberately low. This used to be a strictly SEQUENTIAL loop
    # of 1-3 awaited GETs per post — 40-180 round trips at ~285ms, i.e. 11-17s,
    # and the Dashboard was gated on it. Gathering it fixes that, but the ceiling
    # stays conservative because the PUBLISH path shares this access token:
    # throttling ourselves here would block actual posting, which is far worse
    # than a slow chart.
    sem = asyncio.Semaphore(4)

    async def _one(client: "httpx.AsyncClient", media_id: str) -> tuple[str, dict] | None:
        async with sem:
            try:
                r = await client.get(
                    f"{_GRAPH}/{ver}/{media_id}",
                    params={"fields": "like_count,comments_count", "access_token": token},
                )
                if r.status_code >= 400:
                    return None
                body = r.json() or {}
                likes = int(body.get("like_count") or 0)
                comments = int(body.get("comments_count") or 0)
                views = await _fetch_media_views(client, ver, media_id, token)
                return media_id, {
                    "likes": likes, "comments": comments, "views": views,
                }
            except Exception:
                return None

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(
                *(_one(client, str(m)) for m in ids), return_exceptions=True,
            )
        # Partial data is a valid answer: a post that errored or got throttled is
        # counted in `failed` and simply omitted, rather than failing the request.
        for res in results:
            if not isinstance(res, tuple):
                failed += 1
                continue
            mid, vals = res
            per_media[mid] = vals
            total_likes += vals["likes"]
            total_comments += vals["comments"]
            total_views += vals["views"] or 0
            counted += 1
    except Exception:
        log.exception("[ig_publish] fetch_media_metrics_per_post failed")

    return {
        "per_media": per_media, "total_likes": total_likes,
        "total_comments": total_comments, "total_views": total_views,
        "counted": counted, "failed": failed, "metrics_available": True,
    }


async def publish(
    *, media_url: str, caption: str, media_type: str,
    thumb_offset: int | None = None,
) -> dict:
    """Publish one image or video. Never raises — returns a status dict:
    {status: 'published'|'skipped'|'failed', ig_media_id, permalink, detail}.

    `thumb_offset` (milliseconds, video only) picks the Reel's cover frame —
    Instagram grabs the frame at that offset. Ignored for images."""
    enabled, reason = publish_status()
    if not enabled:
        return {"status": "skipped", "ig_media_id": None, "permalink": None, "detail": reason}

    ver = config.instagram_api_version
    base = f"{_GRAPH}/{ver}/{config.instagram_account_id}"
    token = _token()
    is_video = media_type == "video"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            # 1) Create the media container.
            create_data = {"caption": caption, "access_token": token}
            if is_video:
                create_data["media_type"] = "REELS"
                create_data["video_url"] = media_url
                # Cover frame: the millisecond offset whose frame becomes the
                # Reel thumbnail (the user scrubbed to it in Finalization).
                if thumb_offset is not None:
                    create_data["thumb_offset"] = int(thumb_offset)
            else:
                create_data["image_url"] = media_url
            r = await client.post(f"{base}/media", data=create_data)
            if r.status_code >= 400:
                return _fail(f"container create failed ({r.status_code}): {r.text[:300]}")
            creation_id = (r.json() or {}).get("id")
            if not creation_id:
                return _fail(f"no creation id in response: {r.text[:200]}")

            # 2) Video containers must finish processing before publish.
            if is_video:
                ready = await _await_container(client, ver, creation_id, token)
                if not ready:
                    return _fail("video container did not finish processing in time")

            # 3) Publish (retries the transient "media not ready" error).
            pub_json, err = await _publish_container(client, base, creation_id, token)
            if err:
                return _fail(err)
            media_id = (pub_json or {}).get("id")

            # 4) Fetch the permalink (best-effort).
            permalink = None
            if media_id:
                try:
                    pr = await client.get(
                        f"{_GRAPH}/{ver}/{media_id}",
                        params={"fields": "permalink", "access_token": token},
                    )
                    if pr.status_code < 400:
                        permalink = (pr.json() or {}).get("permalink")
                except Exception:
                    pass

            log.info("[ig_publish] published media_id=%s", media_id)
            return {"status": "published", "ig_media_id": media_id, "permalink": permalink, "detail": None}
    except Exception as exc:
        log.exception("[ig_publish] publish failed")
        return _fail(f"{type(exc).__name__}: {exc}")


async def publish_carousel(*, items: list[dict], caption: str) -> dict:
    """Publish a carousel (2–10 image/video items) as ONE Instagram post.

    Graph API 3-step: create each child container (is_carousel_item=true; wait for
    video children to finish), create a CAROUSEL parent with the child ids, then
    /media_publish the parent. `items` = [{media_url, media_type}]. Never raises —
    returns {status, ig_media_id, permalink, detail}."""
    enabled, reason = publish_status()
    if not enabled:
        return {"status": "skipped", "ig_media_id": None, "permalink": None, "detail": reason}

    media = [it for it in (items or []) if it.get("media_url")]
    if len(media) < 2:
        return _fail("carousel needs at least 2 items")
    media = media[:10]  # Instagram caps carousels at 10 items

    ver = config.instagram_api_version
    base = f"{_GRAPH}/{ver}/{config.instagram_account_id}"
    token = _token()
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            # 1) One child container per slide.
            child_ids: list[str] = []
            for idx, it in enumerate(media):
                is_video = (it.get("media_type") == "video")
                data = {"is_carousel_item": "true", "access_token": token}
                if is_video:
                    data["media_type"] = "VIDEO"
                    data["video_url"] = it["media_url"]
                else:
                    data["image_url"] = it["media_url"]
                r = await client.post(f"{base}/media", data=data)
                if r.status_code >= 400:
                    return _fail(f"carousel child {idx + 1} create failed ({r.status_code}): {r.text[:300]}")
                cid = (r.json() or {}).get("id")
                if not cid:
                    return _fail(f"carousel child {idx + 1}: no creation id ({r.text[:200]})")
                if is_video and not await _await_container(client, ver, cid, token):
                    return _fail(f"carousel video child {idx + 1} did not finish processing in time")
                child_ids.append(cid)

            # 2) Parent CAROUSEL container (caption lives here).
            pr = await client.post(
                f"{base}/media",
                data={
                    "media_type": "CAROUSEL",
                    "children": ",".join(child_ids),
                    "caption": caption,
                    "access_token": token,
                },
            )
            if pr.status_code >= 400:
                return _fail(f"carousel parent create failed ({pr.status_code}): {pr.text[:300]}")
            parent_id = (pr.json() or {}).get("id")
            if not parent_id:
                return _fail(f"carousel parent: no creation id ({pr.text[:200]})")
            await _await_container(client, ver, parent_id, token)  # best-effort settle

            # 3) Publish the parent (retries the transient "media not ready" error).
            pub_json, err = await _publish_container(client, base, parent_id, token)
            if err:
                return _fail(f"carousel {err}")
            media_id = (pub_json or {}).get("id")

            permalink = None
            if media_id:
                try:
                    pr2 = await client.get(
                        f"{_GRAPH}/{ver}/{media_id}",
                        params={"fields": "permalink", "access_token": token},
                    )
                    if pr2.status_code < 400:
                        permalink = (pr2.json() or {}).get("permalink")
                except Exception:
                    pass

            log.info("[ig_publish] published carousel media_id=%s (%d items)", media_id, len(child_ids))
            return {"status": "published", "ig_media_id": media_id, "permalink": permalink, "detail": None}
    except Exception as exc:
        log.exception("[ig_publish] carousel publish failed")
        return _fail(f"{type(exc).__name__}: {exc}")


def _is_media_not_ready(status_code: int, body: str) -> bool:
    """True when a media_publish response is the transient 'not ready yet' error
    (Graph code 9007 / subcode 2207027) rather than a permanent failure — the
    container exists but Instagram hasn't finished settling it, so a short wait
    and retry usually succeeds."""
    if status_code < 400:
        return False
    b = (body or "").lower()
    return (
        "2207027" in body
        or "9007" in body
        or "media id is not available" in b
        or "not ready for publishing" in b
        or "please wait" in b
    )


async def _publish_container(
    client: httpx.AsyncClient, base: str, creation_id: str, token: str
) -> tuple[dict | None, str | None]:
    """POST /media_publish, retrying the transient 'media not ready' error.

    Returns (response_json, None) on success or (None, detail) on a permanent
    failure / exhausted retries. Non-transient failures return immediately."""
    detail = "media_publish failed (no attempts made)"
    for attempt in range(_PUBLISH_MAX_TRIES):
        pub = await client.post(
            f"{base}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
        )
        if pub.status_code < 400:
            return (pub.json() or {}), None
        detail = f"media_publish failed ({pub.status_code}): {pub.text[:300]}"
        if not _is_media_not_ready(pub.status_code, pub.text):
            return None, detail  # permanent error — don't burn retries
        if attempt < _PUBLISH_MAX_TRIES - 1:
            log.info(
                "[ig_publish] media not ready (attempt %d/%d) — waiting %ds",
                attempt + 1, _PUBLISH_MAX_TRIES, _PUBLISH_RETRY_WAIT_SEC,
            )
            await asyncio.sleep(_PUBLISH_RETRY_WAIT_SEC)
    return None, detail


async def _await_container(client: httpx.AsyncClient, ver: str, creation_id: str, token: str) -> bool:
    for _ in range(_VIDEO_POLL_MAX_TRIES):
        try:
            r = await client.get(
                f"{_GRAPH}/{ver}/{creation_id}",
                params={"fields": "status_code", "access_token": token},
            )
            code = (r.json() or {}).get("status_code") if r.status_code < 400 else None
            if code == "FINISHED":
                return True
            if code == "ERROR":
                return False
        except Exception:
            pass
        await asyncio.sleep(_VIDEO_POLL_INTERVAL_SEC)
    return False


def _fail(detail: str) -> dict:
    log.warning("[ig_publish] %s", detail)
    return {"status": "failed", "ig_media_id": None, "permalink": None, "detail": detail}
