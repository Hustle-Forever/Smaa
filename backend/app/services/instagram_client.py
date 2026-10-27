"""Instagram Graph API client (Business Discovery edge).

Reads ANOTHER public Business/Creator account's media using the token of an
account YOU control. This is Meta's official mechanism — no scraping.

What we can read via Business Discovery:
  - media list (paginated)
  - id, caption, media_type, media_product_type, media_url, permalink, timestamp
  - like_count, comments_count, view_count (videos/reels only)
  - thumbnail_url (videos)
  - children { id, media_type, media_url, thumbnail_url, permalink } for carousels
  - account-level: followers_count, follows_count, media_count, name, biography

What we CANNOT read via Business Discovery (only via owner-token /insights):
  - reach, impressions, plays, saved, profile visits
  - story metrics

Auth & token lifecycle:
  - INSTAGRAM_ACCESS_TOKEN must be a LONG-LIVED (60-day) token. Short-lived
    tokens 401 after ~1h. Exchange via:
      curl "https://graph.facebook.com/v21.0/oauth/access_token?\\
        grant_type=fb_exchange_token&client_id=<APP_ID>&\\
        client_secret=<APP_SECRET>&fb_exchange_token=<SHORT_TOKEN>"
    NOTE: this used to say grant_type=ig_exchange_token, which is the
    Instagram-Login flow and is WRONG for the "EAA…" Facebook User token this
    module actually uses — it fails with OAuthException. Corrected to match
    backend/.env.example.
  - To stop the 60-day rot for good: with the long-lived USER token call
    GET /me/accounts and use the linked Page's token instead — a Page token
    derived from a long-lived user token does not expire.
  - Tokens silently rot. Call ping() at startup to surface the issue early.
  - An expired token comes back as Graph code 190 inside a **400**, not a 401,
    so do not key expiry handling on the status code.

Rate limits: ~200 calls/hour per user token. The batched field expansion in
fetch_account_media() returns up to 100 fully-formed posts per call, so even
a multi-thousand-post backfill stays comfortably inside the budget. Media
file downloads go to a CDN and DON'T count against the Graph quota.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..config import config

log = logging.getLogger("archivist.instagram_client")


class InstagramConfigError(RuntimeError):
    """Token / account id missing."""


class InstagramAPIError(RuntimeError):
    """Graph API returned an error response."""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _base_url() -> str:
    return f"https://graph.facebook.com/{config.instagram_api_version}"


def _require_creds() -> tuple[str, str]:
    if not config.instagram_access_token:
        raise InstagramConfigError(
            "INSTAGRAM_ACCESS_TOKEN is not set. Add it to backend/.env and "
            "restart the backend. Use a long-lived (60-day) token; exchange a "
            "short-lived one via /oauth/access_token?grant_type=ig_exchange_token."
        )
    if not config.instagram_account_id:
        raise InstagramConfigError(
            "INSTAGRAM_ACCOUNT_ID is not set. This is the IG Business Account "
            "id attached to the token (i.e. an account you control), not the "
            "handle you're discovering."
        )
    return config.instagram_access_token, config.instagram_account_id


async def _get(path: str, params: dict[str, Any]) -> dict:
    """GET with retry/backoff on 429 + 5xx."""
    token, _ = _require_creds()
    params = {**params, "access_token": token}
    url = f"{_base_url()}{path}"

    last_exc: Exception | None = None
    for attempt, backoff in enumerate([0, 4, 16, 64], start=1):
        if backoff:
            await asyncio.sleep(backoff)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url, params=params)
        except httpx.HTTPError as e:
            last_exc = e
            log.warning("IG GET %s attempt %d failed (transport): %s", path, attempt, e)
            continue

        if r.status_code == 200:
            return r.json()

        # 429 or 5xx → retry
        if r.status_code in (429, 500, 502, 503, 504):
            log.warning(
                "IG GET %s attempt %d got %d; backing off",
                path, attempt, r.status_code,
            )
            last_exc = InstagramAPIError(
                f"HTTP {r.status_code}", status=r.status_code, payload=_safe_json(r),
            )
            continue

        # 4xx other than 429 → fail fast (token bad, malformed query, etc.)
        payload = _safe_json(r)
        msg = (
            (payload.get("error") or {}).get("message")
            if isinstance(payload, dict) else None
        ) or r.text[:300]
        raise InstagramAPIError(
            f"Graph API error ({r.status_code}): {msg}",
            status=r.status_code,
            payload=payload,
        )

    raise InstagramAPIError(
        f"Graph API failed after retries: {last_exc}",
        payload={"last_exception": str(last_exc)},
    )


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:500]}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Full field expansion — gets 100 posts AND their carousel children in ONE call.
# Children fields are limited (no like_count per child; engagement lives on parent).
_MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,permalink,"
    "timestamp,like_count,comments_count,view_count,thumbnail_url,"
    "children{id,media_type,media_url,thumbnail_url,permalink}"
)

# Lighter field set for metrics-only refresh
_METRICS_FIELDS = "id,like_count,comments_count,view_count"


async def fetch_account_media(
    username: str,
    *,
    after: str | None = None,
    page_size: int = 100,
) -> dict:
    """One page of media + account headers via business_discovery.

    Returns the raw `business_discovery` block. The caller paginates by
    following `business_discovery.media.paging.cursors.after`.

    Caveat: Meta silently returns an empty body when the target username is
    not a Business/Creator account or is not discoverable. Caller should
    detect and surface that case.
    """
    _, acct_id = _require_creds()

    media_args = f"limit({int(page_size)})"
    if after:
        # quote-escape the cursor for the field-expansion DSL
        media_args = f"after({after}).{media_args}"

    fields = (
        "business_discovery.username(" + username + "){"
        "followers_count,follows_count,media_count,name,username,biography,"
        "profile_picture_url,website,"
        "media." + media_args + "{" + _MEDIA_FIELDS + "}"
        "}"
    )
    log.info("IG fetch_account_media username=%s after=%s size=%d", username, after, page_size)
    data = await _get(f"/{acct_id}", {"fields": fields})
    bd = data.get("business_discovery")
    if not bd:
        raise InstagramAPIError(
            f"business_discovery returned empty for @{username}. The account "
            f"may not be a Business/Creator account, may have blocked discovery, "
            f"or may not exist. Raw response: {str(data)[:300]}",
            payload=data,
        )
    return bd


async def refresh_media_metrics(username: str, after: str | None = None, page_size: int = 100) -> dict:
    """Lighter call — just the fields that drift (likes / comments / views).

    Returns the same shape as fetch_account_media but with only the metric
    fields populated per media item. Used by the metrics_snapshot phase to
    update engagement without re-downloading anything.
    """
    _, acct_id = _require_creds()
    media_args = f"limit({int(page_size)})"
    if after:
        media_args = f"after({after}).{media_args}"
    fields = (
        "business_discovery.username(" + username + "){"
        "media." + media_args + "{" + _METRICS_FIELDS + "}"
        "}"
    )
    data = await _get(f"/{acct_id}", {"fields": fields})
    bd = data.get("business_discovery")
    if not bd:
        raise InstagramAPIError(
            f"business_discovery returned empty for @{username} (metrics refresh).",
            payload=data,
        )
    return bd


async def ping() -> dict:
    """Verify the token works. Returns {ok, username, expires_warning?}.

    Cheap call. Used at backend startup and by the Instagram tab health badge.
    """
    _, acct_id = _require_creds()
    data = await _get(f"/{acct_id}", {"fields": "id,username,name"})
    return {
        "ok": True,
        "id": data.get("id"),
        "username": data.get("username"),
        "name": data.get("name"),
    }


__all__ = [
    "InstagramConfigError",
    "InstagramAPIError",
    "fetch_account_media",
    "refresh_media_metrics",
    "ping",
]
