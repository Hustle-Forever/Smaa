"""
Higgsfield API client for ByteDance Seedance 2.5.

Higgsfield resells Seedance 2.5 behind one asynchronous HTTP API: POST a
generation, get a request_id back straight away, then poll its status_url
until a terminal state. This module is provider plumbing only (no database),
so every branch is testable offline through httpx.MockTransport.

Facts encoded here were read from docs.higgsfield.ai and open.higgsfield.ai on
2026-09-29:
  - auth header  Authorization: Key <KEY_ID>:<KEY_SECRET>  (server side only)
  - five routes under /bytedance/seedance-2.5/, each declared with
    additionalProperties=false, so one stray field is a 422. build_payload()
    keeps only what a route accepts and validates the rest before we pay.
  - statuses: queued and in_progress are open; completed, failed, nsfw and
    canceled are terminal. failed / nsfw / canceled are not charged.
  - cancel works only while queued (202); 400 once processing has started.
  - outputs are kept at least seven days: download them into our own storage.
  - a generation POST may carry an Idempotency-Key header: repeating the same
    route and body with the same key returns the original request instead of
    starting (and charging for) a second one. Without a key a POST is never
    retried blindly.
  - every response carries X-Correlation-ID; we log it next to the request id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from typing import Any

import httpx

from ..config import config

log = logging.getLogger("archivist.higgsfield")

API_BASE = "https://api.higgsfield.ai"
MODEL_ID = "bytedance/seedance-2.5"
MODEL_PATH = f"/{MODEL_ID}"

ROUTE_TEXT = "text-to-video"
ROUTE_REFERENCE = "reference-to-video"
ROUTE_IMAGE = "image-to-video"
ROUTE_EDIT = "video-edit"
ROUTE_EXTEND = "video-extend"

# The request schema per route, as published on each route's API reference
# page. Anything outside these sets is dropped before sending because the
# schemas forbid extra fields.
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    ROUTE_TEXT: frozenset({
        "prompt", "duration", "resolution", "aspect_ratio", "bitrate_mode",
        "output_format", "generate_audio",
    }),
    ROUTE_REFERENCE: frozenset({
        "prompt", "duration", "resolution", "aspect_ratio", "bitrate_mode",
        "generate_audio", "image_urls", "video_urls", "audio_urls",
    }),
    ROUTE_IMAGE: frozenset({
        "prompt", "image_url", "end_image_url", "duration", "resolution",
        "bitrate_mode", "generate_audio",
    }),
    ROUTE_EDIT: frozenset({
        "prompt", "video_url", "image_urls", "video_urls", "audio_urls",
        "resolution", "bitrate_mode", "generate_audio",
    }),
    ROUTE_EXTEND: frozenset({
        "prompt", "video_url", "duration", "image_urls", "video_urls",
        "audio_urls", "resolution", "bitrate_mode", "generate_audio",
    }),
}
ROUTES: tuple[str, ...] = tuple(ALLOWED_FIELDS)

# Routes where the prompt is mandatory (reference- and image-to-video accept an
# empty prompt because the pictures carry the intent; we still always send one).
_PROMPT_REQUIRED = frozenset({ROUTE_TEXT, ROUTE_EDIT, ROUTE_EXTEND})
_DURATION_ROUTES = frozenset({ROUTE_TEXT, ROUTE_REFERENCE, ROUTE_IMAGE, ROUTE_EXTEND})

RESOLUTIONS: tuple[str, ...] = ("480p", "720p", "1080p")
ASPECTS: tuple[str, ...] = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9")
BITRATE_MODES: tuple[str, ...] = ("standard", "high")
OUTPUT_FORMATS: tuple[str, ...] = ("mp4", "mov")
DURATION_MIN = 4
DURATION_MAX = 30
MAX_IMAGE_URLS = 30
MAX_VIDEO_URLS = 10
MAX_AUDIO_URLS = 10

# Full list price per second of output (USD), from Higgsfield's Seedance 2.5
# pricing page on 2026-09-29. A 30% launch promo runs until PROMO_UNTIL.
PRICE_PER_SEC_USD: dict[str, float] = {"480p": 0.2056, "720p": 0.4622, "1080p": 1.1372}
PROMO_UNTIL = date(2026, 10, 1)
PROMO_DISCOUNT = 0.30

OPEN_STATUSES = frozenset({"queued", "in_progress"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "nsfw", "canceled"})

# Content types the presigned upload endpoint accepts.
UPLOAD_CONTENT_TYPES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
    "audio/wav", "audio/x-wav", "video/mp4",
})

SUBMIT_TIMEOUT_SEC = 60.0
STATUS_TIMEOUT_SEC = 30.0
UPLOAD_PUT_TIMEOUT_SEC = 180.0
_STATUS_ATTEMPTS = 3
# Module level so tests can zero it.
_RETRY_BACKOFF_SEC = 1.0

NOT_CONNECTED_MESSAGE = (
    "Seedance is not connected yet. Add HIGGSFIELD_API_KEY_ID and "
    "HIGGSFIELD_API_KEY_SECRET to the .env file at the project root and restart the backend. "
    "The Veo engine still works without them."
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HiggsfieldError(RuntimeError):
    """A Higgsfield call failed. `message` is written for the person using the
    app; `code` is for the caller's branching:

      not_connected  no API keys configured
      concurrency    account is already rendering its maximum (wait, do not retry)
      invalid        the request was rejected (400/422)
      auth           the keys were rejected (401)
      credits        the account has no credits (403)
      not_found      unknown request or model for this account (404)
      blocked        the model is blocked for this account (423)
      upstream       Higgsfield server error (5xx), retryable
      rate_limited   HTTP 429, retryable
      timeout        no answer in time; for a submit this means "unknown outcome"
      network        could not reach Higgsfield at all
      upload_failed  the presigned PUT did not succeed
      invalid_response  the answer did not have the documented shape
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "error",
        retryable: bool = False,
        correlation_id: str | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retryable = retryable
        self.correlation_id = correlation_id
        self.body = body


class HiggsfieldNotConnected(HiggsfieldError):
    def __init__(self) -> None:
        super().__init__(NOT_CONNECTED_MESSAGE, code="not_connected")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _config_creds() -> tuple[str | None, str | None]:
    return config.higgsfield_api_key_id, config.higgsfield_api_key_secret


def split_combined_key(key_id: str, secret: str) -> tuple[str, str]:
    """Higgsfield's dashboard shows the credential as one string, "id:secret",
    and that is what tends to get pasted. Accept it in either variable: a
    secret that starts with "<id>:" loses the prefix; when one variable is
    empty and the other holds "id:secret", it is split in two."""
    key_id = (key_id or "").strip()
    secret = (secret or "").strip()
    if key_id and secret.startswith(key_id + ":"):
        secret = secret[len(key_id) + 1:].strip()
    elif not key_id and ":" in secret:
        key_id, secret = (part.strip() for part in secret.split(":", 1))
    elif not secret and ":" in key_id:
        key_id, secret = (part.strip() for part in key_id.split(":", 1))
    return key_id, secret


def _creds() -> tuple[str, str] | None:
    """Key id + secret, or None when either is missing. The environment wins
    over the frozen config so a key added at runtime (tests, a hot .env edit
    plus reload) is picked up without a process restart. Both come from the
    .env the backend loads: backend/.env when it exists, else the project
    root's."""
    cfg_id, cfg_secret = _config_creds()
    key_id, secret = split_combined_key(
        os.getenv("HIGGSFIELD_API_KEY_ID") or cfg_id or "",
        os.getenv("HIGGSFIELD_API_KEY_SECRET") or cfg_secret or "",
    )
    if key_id and secret:
        return key_id, secret
    return None


def connected() -> bool:
    return _creds() is not None


def _require_creds() -> tuple[str, str]:
    creds = _creds()
    if creds is None:
        raise HiggsfieldNotConnected()
    return creds


def _headers(creds: tuple[str, str]) -> dict[str, str]:
    return {"Authorization": f"Key {creds[0]}:{creds[1]}"}


# ---------------------------------------------------------------------------
# Pure helpers: payloads, prices, status mapping
# ---------------------------------------------------------------------------

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_url_list(name: str, value: Any, cap: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of URLs")
    urls = [str(u).strip() for u in value]
    if any(not u for u in urls):
        raise ValueError(f"{name} contains an empty URL")
    if len(urls) > cap:
        raise ValueError(f"{name} holds {len(urls)} URLs; Seedance accepts at most {cap}")
    return urls


def build_payload(route: str, **fields: Any) -> dict[str, Any]:
    """Assemble a request body for `route`.

    Drops None values and empty lists, drops fields the route's schema does not
    list (logged at DEBUG so a mistaken `output_format` on reference-to-video is
    visible), then validates what is left. Raises ValueError naming the field,
    so a bad request is caught here rather than as a paid 422 round trip.
    """
    if route not in ALLOWED_FIELDS:
        raise ValueError(f"unknown Seedance route {route!r}; expected one of {', '.join(ROUTES)}")
    allowed = ALLOWED_FIELDS[route]
    payload: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in fields.items():
        if value is None or (isinstance(value, (list, tuple)) and len(value) == 0):
            continue
        if key not in allowed:
            dropped.append(key)
            continue
        payload[key] = value
    if dropped:
        log.debug("[higgsfield] %s: dropped fields not in schema: %s", route, ", ".join(sorted(dropped)))

    prompt = payload.get("prompt")
    if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()):
        raise ValueError("prompt must be a non-empty string")
    if route in _PROMPT_REQUIRED and prompt is None:
        raise ValueError(f"prompt is required for {route}")
    if prompt is not None:
        payload["prompt"] = prompt.strip()

    if "duration" in payload:
        d = payload["duration"]
        if not _is_int(d) or not (DURATION_MIN <= d <= DURATION_MAX):
            raise ValueError(f"duration must be a whole number from {DURATION_MIN} to {DURATION_MAX} seconds")
    if "resolution" in payload and payload["resolution"] not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    if "aspect_ratio" in payload and payload["aspect_ratio"] not in ASPECTS:
        raise ValueError(f"aspect_ratio must be one of {', '.join(ASPECTS)}")
    if "bitrate_mode" in payload and payload["bitrate_mode"] not in BITRATE_MODES:
        raise ValueError(f"bitrate_mode must be one of {', '.join(BITRATE_MODES)}")
    if "output_format" in payload and payload["output_format"] not in OUTPUT_FORMATS:
        raise ValueError(f"output_format must be one of {', '.join(OUTPUT_FORMATS)}")
    if "generate_audio" in payload and not isinstance(payload["generate_audio"], bool):
        raise ValueError("generate_audio must be true or false")

    for name, cap in (("image_urls", MAX_IMAGE_URLS), ("video_urls", MAX_VIDEO_URLS), ("audio_urls", MAX_AUDIO_URLS)):
        if name in payload:
            payload[name] = _check_url_list(name, payload[name], cap)
    for name in ("image_url", "end_image_url", "video_url"):
        if name in payload and (not isinstance(payload[name], str) or not payload[name].strip()):
            raise ValueError(f"{name} must be a URL")

    if route == ROUTE_REFERENCE and not any(k in payload for k in ("image_urls", "video_urls", "audio_urls")):
        raise ValueError("reference-to-video needs at least one of image_urls, video_urls or audio_urls")
    if route == ROUTE_IMAGE and "image_url" not in payload:
        raise ValueError("image-to-video needs image_url")
    if route in (ROUTE_EDIT, ROUTE_EXTEND) and "video_url" not in payload:
        raise ValueError(f"{route} needs video_url")
    return payload


def route_for(image_urls: list[str] | None) -> str:
    """Pick the generation route from what we have to attach."""
    return ROUTE_REFERENCE if image_urls else ROUTE_TEXT


def estimate_local_usd(duration_sec: int, resolution: str, *, on: date | None = None) -> dict[str, Any]:
    """Price a clip from the published per-second table (no network).

    Returns {"usd_list", "usd_promo", "promo_active"}. usd_promo is None once
    the launch promo has ended. The live estimate endpoint is authoritative
    when the account is connected; this is the number shown before that.
    """
    if resolution not in PRICE_PER_SEC_USD:
        raise ValueError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    if not _is_int(duration_sec) or duration_sec <= 0:
        raise ValueError("duration_sec must be a positive whole number")
    usd_list = round(duration_sec * PRICE_PER_SEC_USD[resolution], 4)
    today = on or date.today()
    promo_active = today < PROMO_UNTIL
    usd_promo = round(usd_list * (1.0 - PROMO_DISCOUNT), 4) if promo_active else None
    return {"usd_list": usd_list, "usd_promo": usd_promo, "promo_active": promo_active}


def map_status(body: dict[str, Any]) -> dict[str, Any]:
    """Normalise a status (or webhook) body.

    The polling endpoint puts `video` at the top level; the webhook envelope
    nests it under `payload`. Both are accepted so the same code can read a
    webhook later without changes.
    """
    status = str(body.get("status") or "").strip().lower()
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    video = body.get("video")
    if not isinstance(video, dict):
        video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
    return {
        "status": status,
        "terminal": status in TERMINAL_STATUSES,
        "video_url": video.get("url"),
        "content_type": video.get("content_type"),
        "error": body.get("error"),
        "request_id": body.get("request_id"),
        "raw": body,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _correlation_id(resp: httpx.Response) -> str | None:
    return resp.headers.get("x-correlation-id")


def _detail_text(resp: httpx.Response) -> str:
    """The human part of a FastAPI-style error body ({"detail": ...})."""
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "").strip()
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            bits = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in item.get("loc", []) if x not in ("body",))
                    msg = item.get("msg") or json.dumps(item)
                    bits.append(f"{loc}: {msg}" if loc else str(msg))
                else:
                    bits.append(str(item))
            return "; ".join(bits)
        if detail is not None:
            return json.dumps(detail)
    return (resp.text or "").strip()


def _error_from_response(resp: httpx.Response) -> HiggsfieldError:
    status = resp.status_code
    detail = _detail_text(resp)
    cid = _correlation_id(resp)
    body = (resp.text or "")[:500]
    kw: dict[str, Any] = {"status": status, "correlation_id": cid, "body": body}
    if status == 400 and "maximum number of concurrent requests" in detail.lower():
        return HiggsfieldError(
            "Higgsfield is already rendering the maximum number of videos for this "
            "account. Wait a minute and try again.",
            code="concurrency", **kw,
        )
    if status in (400, 422):
        return HiggsfieldError(f"Higgsfield rejected the request: {detail[:200]}", code="invalid", **kw)
    if status == 401:
        return HiggsfieldError(
            "Higgsfield rejected the API keys. Check HIGGSFIELD_API_KEY_ID and "
            "HIGGSFIELD_API_KEY_SECRET in the .env file at the project root, then restart the backend.",
            code="auth", **kw,
        )
    if status == 403:
        return HiggsfieldError("The Higgsfield account has no credits left.", code="credits", **kw)
    if status == 404:
        return HiggsfieldError(
            "Higgsfield does not know this request or model for this account.",
            code="not_found", **kw,
        )
    if status == 423:
        return HiggsfieldError("Seedance is blocked for this Higgsfield account right now.", code="blocked", **kw)
    if status == 429:
        return HiggsfieldError("Higgsfield asked us to slow down. Try again in a minute.", code="rate_limited", retryable=True, **kw)
    if status >= 500:
        return HiggsfieldError("Higgsfield had a server problem. Try again in a minute.", code="upstream", retryable=True, **kw)
    return HiggsfieldError(f"Higgsfield returned HTTP {status}: {detail[:200]}", code="error", **kw)


def _log_response(method: str, what: str, resp: httpx.Response) -> None:
    log.info("[higgsfield] %s %s -> %d cid=%s", method, what, resp.status_code, _correlation_id(resp))


async def submit(
    route: str,
    payload: dict[str, Any],
    *,
    webhook_url: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """POST one generation. Returns {request_id, status, status_url, cancel_url,
    correlation_id}. Never retried here. A timeout or network failure surfaces
    as code "timeout" / "network" (a 5xx as "upstream"): the outcome is unknown.
    A caller that passes `idempotency_key` may repeat the SAME route and payload
    with the SAME key after such an error and gets the original request back
    rather than a second billable job; without a key it must not retry.
    """
    if route not in ALLOWED_FIELDS:
        raise ValueError(f"unknown Seedance route {route!r}")
    creds = _require_creds()
    url = f"{API_BASE}{MODEL_PATH}/{route}"
    params = {"hf_webhook": webhook_url} if webhook_url else None
    headers = _headers(creds)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=SUBMIT_TIMEOUT_SEC) as client:
            resp = await client.post(url, json=payload, headers=headers, params=params)
    except httpx.TimeoutException as e:
        raise HiggsfieldError(
            "Higgsfield did not confirm the request in time. Check your Higgsfield "
            "dashboard before trying again; a video may already be rendering.",
            code="timeout",
        ) from e
    except httpx.HTTPError as e:
        raise HiggsfieldError(
            "Could not reach Higgsfield. Check the server's internet connection and try again.",
            code="network", retryable=True,
        ) from e
    _log_response("POST", route, resp)
    if resp.status_code >= 400:
        raise _error_from_response(resp)
    try:
        body = resp.json()
    except Exception:
        body = {}
    request_id = body.get("request_id") if isinstance(body, dict) else None
    if not request_id:
        raise HiggsfieldError(
            "Higgsfield accepted the request but sent back no request id.",
            status=resp.status_code, code="invalid_response",
            correlation_id=_correlation_id(resp), body=(resp.text or "")[:500],
        )
    return {
        "request_id": str(request_id),
        "status": str(body.get("status") or "queued"),
        "status_url": body.get("status_url") or f"{API_BASE}/requests/{request_id}/status",
        "cancel_url": body.get("cancel_url") or f"{API_BASE}/requests/{request_id}/cancel",
        "correlation_id": _correlation_id(resp),
    }


async def get_status(status_url: str) -> dict[str, Any]:
    """GET a request's status_url and return map_status() of the body plus
    `correlation_id`. Retries 5xx and network failures a few times with a
    short backoff; 401 and 404 are raised at once (the docs say stop)."""
    creds = _require_creds()
    last_exc: Exception | None = None
    for attempt in range(1, _STATUS_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=STATUS_TIMEOUT_SEC) as client:
                resp = await client.get(status_url, headers=_headers(creds))
        except httpx.HTTPError as e:
            last_exc = HiggsfieldError(
                "Could not reach Higgsfield to check the video.", code="network", retryable=True,
            )
            last_exc.__cause__ = e
        else:
            _log_response("GET", "status", resp)
            if resp.status_code < 400:
                try:
                    body = resp.json()
                except Exception as e:
                    raise HiggsfieldError(
                        "Higgsfield sent an unreadable status answer.",
                        status=resp.status_code, code="invalid_response",
                        correlation_id=_correlation_id(resp), body=(resp.text or "")[:500],
                    ) from e
                out = map_status(body if isinstance(body, dict) else {})
                out["correlation_id"] = _correlation_id(resp)
                return out
            err = _error_from_response(resp)
            if not err.retryable:
                raise err
            last_exc = err
        if attempt < _STATUS_ATTEMPTS:
            await asyncio.sleep(_RETRY_BACKOFF_SEC * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


async def cancel(cancel_url: str) -> bool:
    """Cancel a queued request. True when Higgsfield accepted (202), False when
    it had already started rendering (400). Anything else raises."""
    creds = _require_creds()
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT_SEC) as client:
            resp = await client.post(cancel_url, headers=_headers(creds))
    except httpx.HTTPError as e:
        raise HiggsfieldError("Could not reach Higgsfield to cancel.", code="network", retryable=True) from e
    _log_response("POST", "cancel", resp)
    if resp.status_code == 202 or resp.status_code == 200:
        return True
    if resp.status_code == 400:
        return False
    raise _error_from_response(resp)


async def estimate(route: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Ask Higgsfield what `payload` would cost. Returns {"credits", "usd"} with
    usd as a float. Only meaningful when connected."""
    if route not in ALLOWED_FIELDS:
        raise ValueError(f"unknown Seedance route {route!r}")
    creds = _require_creds()
    url = f"{API_BASE}/estimate{MODEL_PATH}/{route}"
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT_SEC) as client:
            resp = await client.post(url, json=payload, headers=_headers(creds))
    except httpx.HTTPError as e:
        raise HiggsfieldError("Could not reach Higgsfield for a price.", code="network", retryable=True) from e
    _log_response("POST", "estimate", resp)
    if resp.status_code >= 400:
        raise _error_from_response(resp)
    body = resp.json()
    try:
        usd = float(body.get("usd"))
    except (TypeError, ValueError, AttributeError) as e:
        raise HiggsfieldError(
            "Higgsfield sent a price we could not read.",
            status=resp.status_code, code="invalid_response",
            correlation_id=_correlation_id(resp), body=(resp.text or "")[:500],
        ) from e
    credits = body.get("credits")
    try:
        credits = float(credits) if credits is not None else None
    except (TypeError, ValueError):
        credits = None
    return {"credits": credits, "usd": usd}


async def upload_file(data: bytes, content_type: str) -> str:
    """Put a file where Higgsfield can fetch it and return its public URL.

    Two steps: ask for a presigned upload URL for `content_type`, then PUT the
    bytes there with every header Higgsfield returned (the storage provider
    checks them). The API credentials never go to the storage URL.
    """
    ct = (content_type or "").strip().lower()
    if ct not in UPLOAD_CONTENT_TYPES:
        raise ValueError(
            f"Higgsfield uploads accept {', '.join(sorted(UPLOAD_CONTENT_TYPES))}; got {content_type!r}"
        )
    if not data:
        raise ValueError("upload_file got no bytes")
    creds = _require_creds()
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT_SEC) as client:
            resp = await client.post(
                f"{API_BASE}/files/generate-upload-url",
                json={"content_type": ct}, headers=_headers(creds),
            )
    except httpx.HTTPError as e:
        raise HiggsfieldError("Could not reach Higgsfield to upload a picture.", code="network", retryable=True) from e
    _log_response("POST", "generate-upload-url", resp)
    if resp.status_code >= 400:
        raise _error_from_response(resp)
    body = resp.json() if resp.content else {}
    upload_url = body.get("upload_url") if isinstance(body, dict) else None
    public_url = body.get("public_url") if isinstance(body, dict) else None
    if not upload_url or not public_url:
        raise HiggsfieldError(
            "Higgsfield did not return an upload address.",
            status=resp.status_code, code="invalid_response",
            correlation_id=_correlation_id(resp), body=(resp.text or "")[:500],
        )
    headers = {str(k): str(v) for k, v in (body.get("upload_headers") or {}).items()}
    headers.setdefault("Content-Type", ct)
    try:
        async with httpx.AsyncClient(timeout=UPLOAD_PUT_TIMEOUT_SEC) as client:
            put = await client.put(upload_url, content=data, headers=headers)
    except httpx.HTTPError as e:
        raise HiggsfieldError("The picture upload to Higgsfield was interrupted.", code="upload_failed", retryable=True) from e
    if put.status_code >= 300:
        raise HiggsfieldError(
            f"The picture upload to Higgsfield failed (HTTP {put.status_code}).",
            status=put.status_code, code="upload_failed", body=(put.text or "")[:300],
        )
    return str(public_url)


# ---------------------------------------------------------------------------
# Thin route wrappers (build + submit). Only the first two are used today; the
# edit and extend wrappers exist so a later feature does not touch the plumbing.
# ---------------------------------------------------------------------------

async def submit_text_to_video(**fields: Any) -> dict[str, Any]:
    return await submit(ROUTE_TEXT, build_payload(ROUTE_TEXT, **fields))


async def submit_reference_to_video(**fields: Any) -> dict[str, Any]:
    return await submit(ROUTE_REFERENCE, build_payload(ROUTE_REFERENCE, **fields))


async def submit_video_edit(**fields: Any) -> dict[str, Any]:
    return await submit(ROUTE_EDIT, build_payload(ROUTE_EDIT, **fields))


async def submit_video_extend(**fields: Any) -> dict[str, Any]:
    return await submit(ROUTE_EXTEND, build_payload(ROUTE_EXTEND, **fields))
