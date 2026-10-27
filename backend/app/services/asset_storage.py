"""Asset storage: an object store (S3/MinIO or Supabase) with a local-disk fallback.

The object store is chosen once per process by `backend_name()` (config
STORAGE_BACKEND, default `auto`): "s3" talks to MinIO or any S3 through
`s3_backend`, "supabase" uses the Supabase Storage REST API below, and "local"
keeps everything on the backend's disk. Uploads that fail on the object store
fall back to local disk rather than failing the request.

Where a file went is recorded per row, the same way in every mode:
  - storage_bucket + storage_path  → file lives in the object store
  - local_path                     → file lives on the backend's disk

`upload()` returns a dict with one of those shapes. `url_for()` and
`delete()` dispatch based on which is set.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import secrets
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

import httpx

from ..config import config
from .storage import first_existing

log = logging.getLogger("archivist.asset_storage")


class AssetStorageError(RuntimeError):
    pass


class AssetNotFoundError(AssetStorageError):
    """The store answered and the object is not there. Any other
    AssetStorageError may be transient (store down, circuit breaker open), so
    a caller that gives up on a missing file must catch this one only."""


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def backend_name() -> Literal["s3", "supabase", "local"]:
    """Which object store this process uses. `auto` prefers S3 when an endpoint
    is set, then Supabase when its service key is set, else local disk."""
    mode = (config.storage_backend or "auto").strip().lower()
    if mode in ("s3", "supabase", "local"):
        return mode  # type: ignore[return-value]
    if config.s3_endpoint:
        return "s3"
    if config.supabase_service_role_key:
        return "supabase"
    return "local"


def remote_enabled() -> bool:
    """True when uploads go to an object store (they may still fall back)."""
    return backend_name() in ("s3", "supabase")


def _s3():
    """The S3 transport, imported on first use so a machine on the Supabase or
    local backend never needs boto3."""
    from . import s3_backend
    return s3_backend


def public_hosting_configured() -> tuple[bool, str]:
    """(usable, reason): can media be put at a permanent public URL that
    Instagram can fetch? The reason is plain text, empty when usable, and never
    names a password or secret-key setting (Settings shows it)."""
    name = backend_name()
    if name == "s3":
        if not config.s3_endpoint:
            return False, "the media storage endpoint is not set (S3_ENDPOINT)"
        if not (config.s3_access_key and config.s3_secret_key):
            return False, "the media storage credentials are not set"
        if not config.s3_public_base_url:
            return False, "the media storage public URL is not set (S3_PUBLIC_BASE_URL)"
        if not config.approved_media_bucket:
            return False, "the public media bucket name is empty (APPROVED_MEDIA_BUCKET)"
        return True, ""
    if name == "supabase":
        if not config.supabase_url:
            return False, "SUPABASE_URL is not set in .env"
        if not config.supabase_service_role_key:
            return False, (
                "SUPABASE_SERVICE_ROLE_KEY is not set: needed to upload media to the "
                "public bucket (Supabase dashboard > Project Settings > API > service_role)"
            )
        return True, ""
    return False, (
        "no media storage is set up (files stay on the server's disk only), "
        "so media can't be hosted at a public URL"
    )


def _swap_origin(url: str, src: str, dst: str) -> str:
    if url.startswith(src + "/"):
        return dst + url[len(src):]
    parts = urlsplit(url)
    return dst + parts.path + (f"?{parts.query}" if parts.query else "")


def _to_public_origin(url: str) -> str:
    """Internal (S3_ENDPOINT) URL → the same URL through the public /media base.
    Unchanged when no public base is configured."""
    if not (config.s3_public_base_url and config.s3_endpoint):
        return url
    return _swap_origin(url, config.s3_endpoint, config.s3_public_base_url)


def internal_url(url: str) -> str:
    """S3 mode: a public /media URL → the same URL on S3_ENDPOINT, for
    server-side fetches that should not hairpin through the public IP. The path
    and query are kept byte for byte, so a signature stays valid. Any other URL,
    and every URL outside S3 mode, is returned unchanged."""
    if backend_name() != "s3" or not url:
        return url
    base, endpoint = config.s3_public_base_url, config.s3_endpoint
    if base and endpoint and url.startswith(base + "/"):
        return endpoint + url[len(base):]
    return url


def parse_public_url(url: str | None) -> tuple[str, str] | None:
    """(bucket, key) of a URL made by `public_url` (or a signed URL of the
    same object), in either shape:
      S3:        <S3_PUBLIC_BASE_URL or S3_ENDPOINT>/<bucket>/<key>
      Supabase:  .../storage/v1/object/public/<bucket>/<key>
    Query and fragment are dropped and the key is percent-decoded, because a
    DeleteObject on the still-encoded key answers 204 and deletes nothing.
    None for anything else."""
    if not url or not isinstance(url, str):
        return None
    rest = None
    for base in (config.s3_public_base_url, config.s3_endpoint):
        if base and url.startswith(base + "/"):
            rest = url[len(base) + 1:]
            break
    if rest is None:
        marker = "/storage/v1/object/public/"
        path = urlsplit(url).path
        if marker not in path:
            return None
        rest = path.split(marker, 1)[1]
    rest = rest.split("#", 1)[0].split("?", 1)[0]
    bucket, _, key = rest.partition("/")
    key = unquote(key)
    if not bucket or not key:
        return None
    return bucket, key


# ---------------------------------------------------------------------------
# Key helpers. New object keys are ASCII [A-Za-z0-9/_.-] only (A4): Arabic,
# spaces and '+' in a key break SigV4 through the /media proxy or get refused.
# ---------------------------------------------------------------------------

_MIME_EXTS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/svg+xml": ".svg", "image/avif": ".avif",
    "image/heic": ".heic", "video/mp4": ".mp4", "video/quicktime": ".mov",
    "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "application/pdf": ".pdf",
    "font/ttf": ".ttf", "font/otf": ".otf", "font/woff": ".woff", "font/woff2": ".woff2",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
_EXT_RE = re.compile(r"\.[a-z0-9]{1,5}")


def _ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def ascii_slug(text: str | None, fallback: str = "asset", max_len: int = 32) -> str:
    """Lower-case ASCII slug of `text` ([a-z0-9_], at most `max_len` chars).
    Accents fold to their base letter, other non-ASCII letters (Arabic
    included) are dropped, and each run of anything else becomes one '_'.
    Falls back to a slug of `fallback`, then to "asset". Callers add their own
    uniqueness suffix, e.g. f"{ascii_slug(name)}_{secrets.token_hex(3)}"."""
    for candidate in (text, fallback):
        slug = re.sub(r"[^a-z0-9]+", "_", _ascii_fold(candidate or "").lower())
        slug = slug.strip("_")[:max(1, int(max_len))].strip("_")
        if slug:
            return slug
    return "asset"


def safe_ext(filename: str | None, mime: str | None) -> str:
    """Extension for a new key, with the dot: the filename's own suffix when it
    is 1-5 of [a-z0-9] (so "photo.JPG copy" does not qualify), else one derived
    from `mime`, else ""."""
    if filename:
        suffix = _ascii_fold(Path(filename).suffix).lower()
        if _EXT_RE.fullmatch(suffix):
            return suffix
    if mime:
        m = mime.split(";")[0].strip().lower()
        ext = _MIME_EXTS.get(m) or (mimetypes.guess_extension(m) or "").lower()
        if _EXT_RE.fullmatch(ext):
            return ext
    return ""


# ---------------------------------------------------------------------------
# Supabase Storage REST helpers (used when service-role key is present)
# ---------------------------------------------------------------------------

def _bucket_base() -> str:
    """Supabase Storage REST base. We derive it from supabase_url so it works
    across project moves without a second env var."""
    if not config.supabase_url:
        raise AssetStorageError("SUPABASE_URL is not set in .env")
    base = config.supabase_url.rstrip("/")
    return f"{base}/storage/v1"


def _auth_headers() -> dict[str, str]:
    if not config.supabase_service_role_key:
        raise AssetStorageError(
            "SUPABASE_SERVICE_ROLE_KEY is not set — Supabase Storage uploads "
            "won't work until you add it to backend/.env (get it from "
            "Supabase dashboard → Project Settings → API → service_role)"
        )
    return {
        "Authorization": f"Bearer {config.supabase_service_role_key}",
        "apikey": config.supabase_service_role_key,
    }


# Default freshness for objects written to a UNIQUE-per-write path (a new
# generation = a new id = a new key). `private` because private-bucket objects
# are reachable only through signed URLs and must never enter a shared cache;
# the public bucket overrides this to `public` below.
CACHE_IMMUTABLE = "private, max-age=31536000, immutable"
# For paths that are UPSERTED in place (e.g. a Talking Head keyframe rewritten on
# regenerate). Blanket-immutable would pin the pre-regen image for a year.
CACHE_MUTABLE = "public, max-age=86400"
CACHE_PUBLIC_IMMUTABLE = "public, max-age=31536000, immutable"


def _s3_cache_control(bucket: str, cache_control: str | None) -> str:
    """S3 mode: only the anonymous-read Instagram bucket may enter shared
    caches. producer-media is private there (D5), unlike the Supabase rule in
    `_upload_supabase`."""
    if cache_control:
        return cache_control
    if bucket == config.approved_media_bucket:
        return CACHE_PUBLIC_IMMUTABLE
    return CACHE_IMMUTABLE


async def _upload_supabase(
    *, bucket: str, path: str, data: bytes, mime: str | None,
    cache_control: str | None = None,
) -> None:
    url = f"{_bucket_base()}/object/{bucket}/{path}"
    headers = _auth_headers()
    headers["Content-Type"] = mime or "application/octet-stream"
    # `x-upsert: true` so re-uploading the same path overwrites (idempotent)
    headers["x-upsert"] = "true"
    # Supabase persists this Cache-Control on the object and replays it on both
    # the public and the signed URL. Without it objects come back `no-cache` and
    # every view re-downloads the full original — which is exactly what was
    # happening to all 503 producer-assets objects.
    if cache_control:
        headers["Cache-Control"] = cache_control
    elif bucket == config.producer_media_bucket:
        # Public bucket → shared caches are fine and desirable (CDN).
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        headers["Cache-Control"] = CACHE_IMMUTABLE
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, content=data)
        if r.status_code >= 300:
            raise AssetStorageError(
                f"Supabase upload failed ({r.status_code}): {r.text[:300]}"
            )


async def _create_bucket_supabase(*, bucket: str, public: bool) -> None:
    """Create a Storage bucket. Treats 'already exists' as success (idempotent)."""
    url = f"{_bucket_base()}/bucket"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            url,
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"id": bucket, "name": bucket, "public": public},
        )
        if r.status_code >= 300:
            body = r.text.lower()
            if r.status_code == 409 or "already exist" in body or "duplicate" in body:
                return  # bucket is already there — fine
            raise AssetStorageError(
                f"Supabase create-bucket failed ({r.status_code}): {r.text[:300]}"
            )


async def _signed_url_supabase(
    *, bucket: str, path: str, expires: int, transform: dict | None = None,
) -> str:
    """Mint a signed URL. When `transform` is given, Supabase bakes the
    transformation INTO the token and returns a `/render/image/sign/...` URL that
    serves a resized/re-encoded (WebP) variant with no Authorization header.

    Critical: the transform MUST travel in the sign request. Appending
    ?width=/&quality= to a plain token is silently IGNORED — Supabase returns the
    full-resolution original with a 200. That is why the signed-URL cache key
    below carries a transform dimension.
    """
    url = f"{_bucket_base()}/object/sign/{bucket}/{path}"
    body: dict = {"expiresIn": int(expires)}
    if transform:
        body["transform"] = transform
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            url,
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json=body,
        )
        if r.status_code >= 300:
            raise AssetStorageError(
                f"Supabase sign URL failed ({r.status_code}): {r.text[:300]}"
            )
        signed = r.json().get("signedURL") or r.json().get("signedUrl")
        if not signed:
            raise AssetStorageError(f"Supabase sign URL: no URL in response: {r.text[:200]}")
        # The response gives a path-relative URL; absolutise it.
        if signed.startswith("/"):
            return f"{config.supabase_url.rstrip('/')}/storage/v1{signed}"
        return signed


# ---------------------------------------------------------------------------
# Bucket publicness. `config.producer_media_public` describes ONE bucket, but a
# row can name ANY bucket — so gating a public-CDN URL on that flag emitted
# /object/public/<private-bucket>/... URLs that Supabase answers with
# 400 {"error":"Bucket not found"}, which the frontend's <img onError> renders as
# "no preview". The correct predicate is per-bucket and directly queryable.
#
# Refreshed from async entry points (see `ensure_bucket_publicness`) and read
# SYNCHRONOUSLY by URL builders. TTL rather than startup-only because this is a
# SHARED Supabase project: a teammate flipping a bucket must not require every
# developer to restart. Unknown bucket ⇒ treated as PRIVATE ⇒ backend proxy,
# which is the safe direction (slow, never broken).
# ---------------------------------------------------------------------------
_BUCKET_PUBLIC: dict[str, bool] = {}
_BUCKET_PUBLIC_FETCHED_AT: float = 0.0
_BUCKET_PUBLIC_TTL = 300.0  # 5 min
_bucket_public_lock: "asyncio.Lock | None" = None


async def _fetch_bucket_publicness() -> dict[str, bool]:
    """GET /storage/v1/bucket → {bucket_id: is_public}. Read-only."""
    url = f"{_bucket_base()}/bucket"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url, headers=_auth_headers())
        if r.status_code >= 300:
            raise AssetStorageError(
                f"Supabase list-buckets failed ({r.status_code}): {r.text[:200]}"
            )
        return {b["id"]: bool(b.get("public")) for b in r.json() if b.get("id")}


async def ensure_bucket_publicness(*, force: bool = False) -> None:
    """Populate/refresh the publicness map. Never raises — on failure the map is
    left as-is (or empty), so URL builders degrade to the proxy rather than
    emitting URLs that 400. Call from any async request path before building
    media URLs; cheap after the first warm-up.
    """
    global _BUCKET_PUBLIC_FETCHED_AT, _bucket_public_lock
    if backend_name() == "s3":
        # Known from config, no lookup: only the Instagram bucket is anonymous-
        # readable, and PRODUCER_MEDIA_PUBLIC is ignored here (D5) because a
        # public producer-media would expose sequential keys past the login gate.
        _BUCKET_PUBLIC.clear()
        _BUCKET_PUBLIC.update({
            config.producer_assets_bucket: False,
            config.producer_media_bucket: False,
            config.brand_docs_bucket: False,
            config.approved_media_bucket: True,
        })
        _BUCKET_PUBLIC_FETCHED_AT = time.monotonic()
        return
    if not force and _BUCKET_PUBLIC and (
        time.monotonic() - _BUCKET_PUBLIC_FETCHED_AT < _BUCKET_PUBLIC_TTL
    ):
        return
    if not config.supabase_service_role_key:
        # Without the key every bucket reads as private and every tile silently
        # drops to the slow proxy — worth a loud log, not a failure.
        log.warning(
            "SUPABASE_SERVICE_ROLE_KEY absent — bucket publicness unknown, all "
            "media will be served through the backend proxy (slow path)"
        )
        return
    import asyncio as _asyncio
    if _bucket_public_lock is None:
        _bucket_public_lock = _asyncio.Lock()
    async with _bucket_public_lock:
        if not force and _BUCKET_PUBLIC and (
            time.monotonic() - _BUCKET_PUBLIC_FETCHED_AT < _BUCKET_PUBLIC_TTL
        ):
            return
        try:
            fresh = await _fetch_bucket_publicness()
        except Exception:  # noqa: BLE001
            log.exception("Bucket publicness refresh failed (keeping previous map)")
            return
        _BUCKET_PUBLIC.clear()
        _BUCKET_PUBLIC.update(fresh)
        _BUCKET_PUBLIC_FETCHED_AT = time.monotonic()
        log.info("Bucket publicness: %s", _BUCKET_PUBLIC)


def bucket_is_public(bucket: str | None) -> bool:
    """True only when `bucket` is KNOWN to be a public Storage bucket.

    Sync so the URL builders stay sync. Unknown/unfetched ⇒ False ⇒ proxy.
    """
    if not bucket:
        return False
    return _BUCKET_PUBLIC.get(bucket, False)


# ---------------------------------------------------------------------------
# Image-transformation capability. `/storage/v1/render/image/**` is a PAID
# Supabase add-on. On a project without it EVERY render URL — signed, public and
# authenticated alike — answers 403 {"code":"FeatureNotEnabled"}, while the very
# same object over `/object/...` returns 200.
#
# That asymmetry is what made this so hard to see: the dead URLs are handed to
# the BROWSER inside list JSON, so the failure surfaces as <img onError> on every
# tile and NEVER as an exception the backend could log. The Library, the
# Dashboard's recent work and the Assets grid all went blank together while the
# bytes were fine and every server-side log stayed clean (#37).
#
# Same shape as the bucket-publicness cache above, for the same reason: this is a
# SHARED project whose plan can change under a running process. Unknown ⇒ treated
# as UNAVAILABLE ⇒ plain /object/ URLs — the safe direction (bigger, always
# correct) rather than the fast-but-possibly-broken one.
# ---------------------------------------------------------------------------
_IMAGE_TRANSFORM_OK: "bool | None" = None   # None = never successfully probed
_IMAGE_TRANSFORM_CHECKED_AT: float = 0.0
_IMAGE_TRANSFORM_TTL = 900.0        # 15 min after a DEFINITIVE yes/no
_IMAGE_TRANSFORM_RETRY_TTL = 60.0   # sooner when the probe itself was unusable
_image_transform_lock: "asyncio.Lock | None" = None


def image_transform_enabled() -> bool:
    """True only when the render endpoint is KNOWN to work (or forced on).

    Sync so the URL builders stay sync, exactly like `bucket_is_public`.
    Always False on S3: MinIO has no image transformer (D13).
    """
    if backend_name() == "s3":
        return False
    mode = (config.supabase_image_transform or "auto").strip().lower()
    if mode == "on":
        return True
    if mode == "off":
        return False
    return _IMAGE_TRANSFORM_OK is True


async def ensure_image_transform_capability(
    sample: "tuple[str, str] | None" = None, *, force: bool = False,
) -> None:
    """Probe `/render/image` and cache the verdict. Never raises.

    `sample` is a REAL (bucket, path) to probe with, and it is required: Supabase
    resolves the object BEFORE checking the entitlement, so a made-up path answers
    400 NoSuchKey on every tenant and can never tell the two cases apart. Verified
    against the live project — that mistake makes the probe claim the feature
    works while every real URL 403s. Callers on the thumbnail paths always have a
    real object to hand; without one this stays "unknown", which reads as
    unavailable and therefore safe.

    An inconclusive probe (network error, 5xx, auth problem, a path that has since
    been deleted) is NOT treated as "disabled": it leaves the previous verdict
    alone and retries sooner. Latching a paid feature off because one request
    timed out would be its own outage.
    """
    global _IMAGE_TRANSFORM_OK, _IMAGE_TRANSFORM_CHECKED_AT, _image_transform_lock

    if backend_name() == "s3":
        return  # no transformer to discover
    if (config.supabase_image_transform or "auto").strip().lower() != "auto":
        return  # forced on/off — nothing to discover

    ttl = _IMAGE_TRANSFORM_TTL if _IMAGE_TRANSFORM_OK is not None else _IMAGE_TRANSFORM_RETRY_TTL
    if not force and time.monotonic() - _IMAGE_TRANSFORM_CHECKED_AT < ttl:
        return
    if not (config.supabase_url and config.supabase_service_role_key and sample):
        return  # nothing to probe with; stays unknown ⇒ plain object URLs

    import asyncio as _asyncio
    if _image_transform_lock is None:
        _image_transform_lock = _asyncio.Lock()

    async with _image_transform_lock:
        if not force and time.monotonic() - _IMAGE_TRANSFORM_CHECKED_AT < ttl:
            return
        bucket, path = sample
        base = config.supabase_url.rstrip("/")
        url = (
            f"{base}/storage/v1/render/image/authenticated/{bucket}/{path.lstrip('/')}"
            "?width=8&height=8&resize=contain"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=_auth_headers())
        except Exception:  # noqa: BLE001 — inconclusive, not "disabled"
            _IMAGE_TRANSFORM_CHECKED_AT = time.monotonic()
            log.warning("Image-transform probe failed to reach Supabase; verdict unchanged")
            return

        _IMAGE_TRANSFORM_CHECKED_AT = time.monotonic()
        body = (r.text or "")[:200]
        if r.status_code == 403 and "FeatureNotEnabled" in body:
            if _IMAGE_TRANSFORM_OK is not False:
                log.warning(
                    "Supabase Image Transformation is NOT enabled for this project "
                    "— serving full-size objects instead of resized thumbnails. "
                    "Enable the add-on, or set SUPABASE_IMAGE_TRANSFORM=off to silence this.",
                )
            _IMAGE_TRANSFORM_OK = False
        elif 200 <= r.status_code < 300:
            if _IMAGE_TRANSFORM_OK is not True:
                log.info("Supabase Image Transformation available — thumbnails will be resized")
            _IMAGE_TRANSFORM_OK = True
        else:
            # Includes 400 NoSuchKey (the sample was deleted between the DB read
            # and the probe) — says nothing about the entitlement either way.
            log.warning(
                "Image-transform probe inconclusive (HTTP %s: %s); verdict unchanged",
                r.status_code, body[:120],
            )


async def _delete_supabase(*, bucket: str, paths: list[str]) -> None:
    url = f"{_bucket_base()}/object/{bucket}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(
            "DELETE",
            url,
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"prefixes": paths},
        )
        if r.status_code >= 300:
            log.warning("Supabase delete failed (%d): %s", r.status_code, r.text[:300])


# ---------------------------------------------------------------------------
# Local-disk fallback
# ---------------------------------------------------------------------------

def _local_dest(path: str) -> Path:
    """Resolve a logical path (e.g. 'characters/emirati_male/foo.jpg') to the
    backend-relative local fallback root."""
    root = config.producer_assets_root
    root.mkdir(parents=True, exist_ok=True)
    p = (root / path).resolve()
    # Defence-in-depth: ensure no '..' escape outside the root
    try:
        p.relative_to(root.resolve())
    except ValueError:
        raise AssetStorageError(f"Refusing to write outside producer_assets_root: {path}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _local_to_db_path(local_abs: Path) -> str:
    """DB stores a project-relative path (mirrors services/storage.py's convention)."""
    try:
        backend_root = config.producer_assets_root.parent.parent  # backend/
        return str(local_abs.relative_to(backend_root))
    except Exception:
        return str(local_abs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

UploadResult = dict
"""
Shape:
  {
    "storage": "s3" | "supabase" | "local",
    "storage_bucket": str | None,
    "storage_path":   str | None,   # bucket key (when s3/supabase). On S3 it can
                                    # differ from the requested path: persist THIS.
    "local_path":     str | None,   # DB-relative path (when local)
    "bytes": int,
    "mime_type": str | None,
  }
"""


def make_slug(prefix: str = "asset") -> str:
    """Short, URL-safe, unique slug suitable as a bucket key prefix."""
    return f"{prefix}_{secrets.token_hex(4)}"


async def upload(
    *,
    data: bytes,
    path: str,                    # logical path INSIDE the bucket (or fallback root)
    mime: str | None = None,
    prefer: Literal["auto", "supabase", "local"] = "auto",
    bucket: str | None = None,    # defaults to the producer-assets bucket
    cache_control: str | None = None,
) -> UploadResult:
    """Upload bytes to the object store (if one is configured) or the local fallback.

    `prefer="auto"` (default): the configured backend (`backend_name()`).
    `prefer="supabase"` is a legacy alias for "the object store if there is
    one, else local"; it never picks the Supabase transport in S3 mode.
    `prefer="local"` forces local disk (useful for tests).
    `bucket` overrides the destination bucket (e.g. brand-docs).
    `cache_control` overrides object freshness — pass `CACHE_MUTABLE` when the
    path is UPSERTED in place rather than unique per write.

    S3 mode: an invalid key is sanitised rather than refused (A4 backstop) and
    the key actually written comes back in `storage_path`. Any S3 failure falls
    back to local disk, logged at WARNING.
    """
    dest_bucket = bucket or config.producer_assets_bucket
    target = "local" if prefer == "local" else backend_name()

    if target == "s3":
        s3 = _s3()
        key = path
        if not s3.valid_key(key):
            key = s3.sanitize_key(path)
            log.warning("upload: invalid S3 key %r stored as %r", path, key)
        try:
            await s3.put_bytes(
                dest_bucket, key, data, content_type=mime,
                cache_control=_s3_cache_control(dest_bucket, cache_control),
            )
            _signed_cache_evict(dest_bucket, key)
            # A key rewritten in place (a regenerated keyframe) must not keep
            # its old thumbnail; the next read makes a fresh one.
            await _drop_derivatives(dest_bucket, key)
            return {
                "storage": "s3",
                "storage_bucket": dest_bucket,
                "storage_path": key,
                "local_path": None,
                "bytes": len(data),
                "mime_type": mime,
            }
        except AssetStorageError as e:
            log.warning(
                "S3 upload of %s/%s failed, falling back to local disk: %s",
                dest_bucket, key, e,
            )
            # fall through to local

    if target == "supabase":
        try:
            await _upload_supabase(
                bucket=dest_bucket,
                path=path,
                data=data,
                mime=mime,
                cache_control=cache_control,
            )
            # An upsert to an existing key must not keep serving the OLD object's
            # cached signed URLs (previously only `delete()` evicted), or a
            # regenerated keyframe serves stale art for the rest of the TTL.
            _signed_cache_evict(dest_bucket, path)
            return {
                "storage": "supabase",
                "storage_bucket": dest_bucket,
                "storage_path": path,
                "local_path": None,
                "bytes": len(data),
                "mime_type": mime,
            }
        except AssetStorageError as e:
            log.warning("Supabase upload failed, falling back to local disk: %s", e)
            # fall through to local

    # Local fallback
    dest = _local_dest(path)
    dest.write_bytes(data)
    return {
        "storage": "local",
        "storage_bucket": None,
        "storage_path": None,
        "local_path": _local_to_db_path(dest),
        "bytes": len(data),
        "mime_type": mime,
    }


async def persist_local_file(
    *,
    abs_path: Path,
    bucket_key: str,
    mime: str = "video/mp4",
) -> tuple[str | None, str | None]:
    """Back up a file that already lives on local disk to the durable object
    store (default bucket), returning ``(storage_bucket, storage_path)``, or
    ``(None, None)`` when no object store is configured or the upload fails
    (caller keeps serving from local disk in that case).

    Used for render outputs (video-editor / Talking-Head compositions) that
    Remotion writes only to the ephemeral container disk: persisting them means
    a redeploy that wipes the disk can no longer 410 a finished video — the file
    routes recover from the bucket. Never raises; failures degrade to local-only.
    S3 streams the file (no read into RAM); local mode writes no second copy.
    """
    name = backend_name()
    if name == "local":
        return None, None
    if name == "s3":
        s3 = _s3()
        bucket = config.producer_assets_bucket
        key = bucket_key if s3.valid_key(bucket_key) else s3.sanitize_key(bucket_key)
        try:
            await s3.upload_file(
                bucket, key, abs_path, content_type=mime,
                cache_control=_s3_cache_control(bucket, None),
            )
        except AssetStorageError as e:
            log.warning("persist_local_file: S3 upload of %s/%s failed: %s", bucket, key, e)
            return None, None
        _signed_cache_evict(bucket, key)
        return bucket, key
    try:
        blob = abs_path.read_bytes()
    except Exception:  # noqa: BLE001
        log.warning("persist_local_file: cannot read %s", abs_path)
        return None, None
    try:
        result = await upload(data=blob, path=bucket_key, mime=mime)
    except Exception:  # noqa: BLE001
        log.exception("persist_local_file: bucket upload failed for %s", bucket_key)
        return None, None
    return result.get("storage_bucket"), result.get("storage_path")


# ---------------------------------------------------------------------------
# Signed-URL cache. Minting a signed URL is a blocking POST to Supabase's
# /object/sign endpoint — previously done on EVERY media request, so a Library
# grid of hundreds of tiles meant hundreds of serial sign round-trips. We cache
# the signed URL per (bucket, path, expires) and reuse it until shortly before it
# expires. Process-local (each worker warms its own) and bounded. Public-bucket
# media (producer-media) bypasses signing entirely, so this now mainly serves the
# still-private buckets (producer-assets, brand-docs).
#
# S3 presigning is offline, but the cache still matters: a new X-Amz-Date is a
# new URL, which would defeat browser caching and churn the list ETags on every
# poll. S3 entries are keyed (bucket, path, clamped_expires, transform_key,
# download_name) and hold the URL on the INTERNAL origin; the public origin is
# swapped in on the way out, so one entry serves both server and browser (A1).
# ---------------------------------------------------------------------------
_SIGNED_URL_CACHE: dict[tuple, tuple[str, float]] = {}
# Each object can now occupy several entries (full-res + one per transform tier),
# so the cap is raised proportionally.
_SIGNED_URL_CACHE_MAX = 20000
# Re-sign this many seconds BEFORE the URL actually expires, so a cached URL is
# never handed out close to death (clock skew + in-flight request slack).
_SIGNED_URL_SAFETY_MARGIN = 600


def transform_key(transform: dict | None) -> str:
    """Canonical, stable string for a transform spec — the 4th cache-key slot.

    Empty string means "no transform" (full-resolution). Sorted so two callers
    spelling the same spec in a different key order share one cache entry.
    """
    if not transform:
        return ""
    return ",".join(f"{k}:{transform[k]}" for k in sorted(transform))


def _signed_cache_get(key: tuple) -> str | None:
    hit = _SIGNED_URL_CACHE.get(key)
    if not hit:
        return None
    url, expiry = hit
    if time.monotonic() >= expiry:
        _SIGNED_URL_CACHE.pop(key, None)
        return None
    return url


def _signed_cache_put(key: tuple, url: str, ttl: float) -> None:
    if len(_SIGNED_URL_CACHE) >= _SIGNED_URL_CACHE_MAX:
        # Drop expired first; if still full, evict oldest-inserted (dicts keep
        # insertion order) until back under the cap.
        now = time.monotonic()
        for k in [k for k, (_, e) in _SIGNED_URL_CACHE.items() if e <= now]:
            _SIGNED_URL_CACHE.pop(k, None)
        while len(_SIGNED_URL_CACHE) >= _SIGNED_URL_CACHE_MAX:
            _SIGNED_URL_CACHE.pop(next(iter(_SIGNED_URL_CACHE)), None)
    _SIGNED_URL_CACHE[key] = (url, time.monotonic() + ttl)


def _signed_cache_evict(bucket: str, path: str) -> None:
    """Drop every cached signing of (bucket, path), across all expires tiers —
    called on delete and after every write, so a recycled or overwritten path
    never serves a dead/old signed URL."""
    for k in [k for k in _SIGNED_URL_CACHE if k[0] == bucket and k[1] == path]:
        _SIGNED_URL_CACHE.pop(k, None)


async def url_for(
    *,
    storage_bucket: str | None,
    storage_path: str | None,
    local_path: str | None,
    expires: int = 3600,
    transform: dict | None = None,
    download_name: str | None = None,
    internal: bool = False,
) -> str | None:
    """Return a URL the frontend can render the file from.

    For bucket rows: a signed URL valid `expires` seconds (cached and reused
    until shortly before expiry — see the signed-URL cache above). Pass
    `transform` to get a signed IMAGE-TRANSFORM URL instead of the original
    (Supabase only; S3 ignores it). Returns None when the object is missing or
    signing fails, so callers fall back to the local file or a clean 4xx.
    For local rows: returns None — caller should use the backend file proxy.

    `download_name` makes the browser save the file under that name. It is
    baked in here (S3: a signed response-content-disposition; Supabase: its
    `download=` parameter), so never append anything to the returned URL: on
    S3 every query parameter is signed and an extra one is a 403.

    `internal=True` (S3 only) returns the URL on S3_ENDPOINT for server-side
    fetches (httpx), which cannot reach the public origin from inside the
    stack. Never pass it for a URL that goes to a browser.
    """
    if not (storage_bucket and storage_path):
        return None
    if backend_name() == "s3":
        return await _s3_url_for(
            bucket=storage_bucket, path=storage_path, expires=expires,
            transform=transform, download_name=download_name, internal=internal,
        )
    cache_key = (storage_bucket, storage_path, int(expires), transform_key(transform))
    cached = _signed_cache_get(cache_key)
    if cached:
        return _supabase_download(cached, download_name)
    try:
        signed = await _signed_url_supabase(
            bucket=storage_bucket, path=storage_path, expires=expires,
            transform=transform,
        )
        # Reuse until SAFETY_MARGIN before expiry (min 30s for tiny TTLs).
        _signed_cache_put(
            cache_key, signed, max(30, int(expires) - _SIGNED_URL_SAFETY_MARGIN),
        )
        return _supabase_download(signed, download_name)
    except Exception as e:
        # ANY failure (AssetStorageError OR a transient httpx/network error
        # talking to Supabase) degrades to None so callers fall back to the
        # local file / a clean 4xx instead of bubbling up a 500.
        log.warning("Supabase sign URL failed for %s/%s: %s", storage_bucket, storage_path, e)
        return None


def _supabase_download(url: str, download_name: str | None) -> str:
    """Supabase signs only its token, so its `download=` flag can ride along."""
    if not download_name:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}download={quote(download_name, safe='')}"


async def _s3_url_for(
    *, bucket: str, path: str, expires: int, transform: dict | None,
    download_name: str | None, internal: bool,
) -> str | None:
    s3 = _s3()
    clamped = s3.clamp_expires(expires)
    cache_key = (bucket, path, clamped, transform_key(transform), download_name or "")
    signed = _signed_cache_get(cache_key)
    if not signed:
        try:
            # Presigning never checks existence, but callers rely on None for a
            # missing object to fall back to the local copy or a 410 (D9). HEAD
            # only on a cache miss; any failure other than 404 is also None.
            if not await s3.head(bucket, path):
                log.info("S3 object missing: %s/%s", bucket, path)
                return None
            signed = s3.presign_get(
                bucket, path, expires=clamped, download_name=download_name,
            )
        except Exception as e:  # noqa: BLE001 - degrade to None like Supabase
            log.warning("S3 sign URL failed for %s/%s: %s", bucket, path, e)
            return None
        _signed_cache_put(cache_key, signed, max(30, clamped - _SIGNED_URL_SAFETY_MARGIN))
    return signed if internal else _to_public_origin(signed)


async def object_exists(*, bucket: str, path: str) -> bool:
    """HEAD the object: True if present, False on 404. Raises AssetStorageError
    on any other failure, including when no object store is configured."""
    name = backend_name()
    if name == "s3":
        return await _s3().head(bucket, path)
    if name == "supabase":
        url = f"{_bucket_base()}/object/{bucket}/{path}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.head(url, headers=_auth_headers())
        except httpx.HTTPError as e:
            raise AssetStorageError(f"Supabase HEAD failed for {bucket}/{path}: {e}") from e
        if r.status_code < 300:
            return True
        # Supabase answers a missing object with 400 or 404 depending on version.
        if r.status_code in (400, 404):
            return False
        raise AssetStorageError(f"Supabase HEAD failed for {bucket}/{path} ({r.status_code})")
    raise AssetStorageError("no object store is configured (local disk mode)")


def local_to_abs(local_path: str) -> Path:
    """Resolve a DB-stored project-relative path back to absolute.

    Relative paths are backend/-relative here, but some writers store them
    relative to backend/data (Poster+Human renders); see first_existing.
    """
    p = Path(local_path)
    if p.is_absolute():
        return p
    backend_root = config.producer_assets_root.parent.parent  # backend/
    return first_existing(backend_root / p, config.asset_root.parent / p)


async def delete(
    *,
    storage_bucket: str | None,
    storage_path: str | None,
    local_path: str | None,
) -> None:
    """Best-effort delete from wherever the file lives."""
    if storage_bucket and storage_path:
        _signed_cache_evict(storage_bucket, storage_path)
        if backend_name() == "s3":
            try:
                await _s3().delete(storage_bucket, storage_path)
            except Exception as e:  # noqa: BLE001
                log.warning("S3 delete of %s/%s failed: %s", storage_bucket, storage_path, e)
            await _drop_derivatives(storage_bucket, storage_path)
        else:
            try:
                await _delete_supabase(bucket=storage_bucket, paths=[storage_path])
            except Exception:
                log.exception("Supabase delete failed")
    if local_path:
        try:
            p = local_to_abs(local_path)
            if p.exists():
                p.unlink()
        except Exception:
            log.exception("Local delete failed for %s", local_path)


async def delete_public_urls(urls: list[str] | None) -> None:
    """Best effort: delete every hosted object named by `urls` (results of
    `public_url`, either the S3 or the Supabase shape). Empty or unrecognised
    entries are logged and skipped. Never raises."""
    seen: set[tuple[str, str]] = set()
    for url in urls or []:
        try:
            parsed = parse_public_url(url)
            if not parsed:
                if url:
                    log.info("delete_public_urls: not a hosted media URL, skipped: %.200s", url)
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            await delete(storage_bucket=parsed[0], storage_path=parsed[1], local_path=None)
        except Exception:  # noqa: BLE001
            log.exception("delete_public_urls: failed for %.200s", url)


# ---------------------------------------------------------------------------
# PUBLIC bucket helpers (used by approved_media for Instagram publishing).
# A public bucket serves objects at a PERMANENT, unsigned URL that Instagram's
# Content Publishing API can fetch — no signing, no tunnel, no expiry.
# ---------------------------------------------------------------------------

# S3 buckets this process has already created/configured, as (bucket, public).
# approved_media ensures its bucket before every upload; after the first
# success that costs nothing.
_S3_BUCKETS_READY: set[tuple[str, bool]] = set()


async def _s3_ensure_bucket(bucket: str, public: bool) -> None:
    if (bucket, public) in _S3_BUCKETS_READY:
        return
    s3 = _s3()
    await s3.create_bucket(bucket)
    if public:
        await s3.set_public_read(bucket)
    _S3_BUCKETS_READY.add((bucket, public))


async def ensure_public_bucket(bucket: str) -> None:
    """Idempotently create `bucket` as a PUBLIC bucket.

    S3: anonymous read is GetObject only (never ListBucket). Supabase: requires
    SUPABASE_SERVICE_ROLE_KEY. Safe to call on every upload: an existing
    bucket is a no-op. Raises AssetStorageError on failure.
    """
    if backend_name() == "s3":
        await _s3_ensure_bucket(bucket, True)
        return
    await _create_bucket_supabase(bucket=bucket, public=True)


async def ensure_bucket(bucket: str, *, public: bool = False) -> None:
    """Idempotently create `bucket` (private by default). For internal files
    like brand documents that are served via signed URLs, not public links.
    Raises AssetStorageError on failure."""
    if backend_name() == "s3":
        await _s3_ensure_bucket(bucket, public)
        return
    await _create_bucket_supabase(bucket=bucket, public=public)


async def bootstrap_storage() -> None:
    """Startup: make sure the buckets exist and warm the publicness map, for
    whichever backend is configured. Idempotent and never raises; a failure is
    logged and the app starts anyway (uploads then fall back to local disk)."""
    name = backend_name()
    log.info("Storage backend: %s", name)
    if name == "s3":
        if not config.s3_public_base_url:
            log.warning(
                "S3_PUBLIC_BASE_URL is not set: browsers get media URLs on %s and "
                "Instagram hosting is off", config.s3_endpoint or "(no S3_ENDPOINT)",
            )
        wanted = [
            (config.producer_assets_bucket, False),
            (config.producer_media_bucket, False),
            (config.brand_docs_bucket, False),
            (config.approved_media_bucket, True),
        ]
        for bucket, public in wanted:
            if not bucket:
                continue
            try:
                await ensure_bucket(bucket, public=public)
            except Exception as e:  # noqa: BLE001
                log.warning("Bucket %s ensure failed (non-fatal): %s", bucket, e)
    elif name == "supabase":
        # Producer generation media (Create Poster / Animation). With
        # PRODUCER_MEDIA_PUBLIC the bucket is PUBLIC so the frontend loads media
        # straight from Supabase's CDN at permanent, browser-cacheable URLs.
        # ensure_bucket only sets publicness on CREATE, so an EXISTING private
        # bucket must be flipped to public once in the Supabase dashboard.
        try:
            await ensure_bucket(
                config.producer_media_bucket, public=config.producer_media_public,
            )
        except Exception:  # noqa: BLE001
            log.exception("producer-media bucket ensure failed (non-fatal)")
    # Warm the per-bucket publicness map. Media URL builders are sync and read
    # it from cache; unknown ⇒ private ⇒ backend proxy. On Supabase this is a
    # READ (GET /bucket) and must never write publicness: a teammate starting
    # without PRODUCER_MEDIA_PUBLIC would otherwise flip the SHARED bucket
    # private for everyone.
    try:
        await ensure_bucket_publicness(force=True)
    except Exception:  # noqa: BLE001
        log.exception("bucket publicness warm-up failed (non-fatal)")


async def download_bytes(*, storage_bucket: str, storage_path: str) -> bytes:
    """Fetch an object's bytes from the object store (private or public).

    Used to materialise a stored file into a local temp for the file-based
    ingest extractors. S3 reads through the internal endpoint. Raises
    AssetNotFoundError when the object is missing and AssetStorageError on any
    other failure."""
    if backend_name() == "s3":
        return await _s3().get_bytes(storage_bucket, storage_path)
    url = f"{_bucket_base()}/object/{storage_bucket}/{storage_path}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(url, headers=_auth_headers())
        if r.status_code >= 300:
            # Supabase answers a missing object with 400 or 404 depending on version.
            err = AssetNotFoundError if r.status_code in (400, 404) else AssetStorageError
            raise err(f"Supabase download failed ({r.status_code}): {r.text[:200]}")
        return r.content


def public_url(bucket: str, path: str) -> str:
    """Permanent public URL for an object in a PUBLIC bucket.

    S3: f"{S3_PUBLIC_BASE_URL}/{bucket}/{quoted key}". Raises AssetStorageError
    when the relevant base URL is not configured."""
    if backend_name() == "s3":
        if not config.s3_public_base_url:
            raise AssetStorageError("S3_PUBLIC_BASE_URL is not set in .env")
        return f"{config.s3_public_base_url}/{bucket}/{quote(path.lstrip('/'))}"
    if not config.supabase_url:
        raise AssetStorageError("SUPABASE_URL is not set in .env")
    base = config.supabase_url.rstrip("/")
    return f"{base}/storage/v1/object/public/{bucket}/{path.lstrip('/')}"


def public_render_url(
    bucket: str,
    path: str,
    *,
    width: int | None = None,
    height: int | None = None,
    quality: int | None = None,
    resize: str | None = None,
) -> str:
    """Supabase image-transformation URL for an object in a PUBLIC bucket.

    Serves a resized/re-encoded variant on the fly via Supabase's `/render/image`
    endpoint (Pro plan). Used for Library/grid thumbnails so we ship ~400px WebP
    instead of the full-size original. Format is content-negotiated (WebP/AVIF) by
    the CDN from the browser's `Accept` header — no explicit format param needed.
    Requires a PUBLIC bucket (same as `public_url`). Falls back gracefully: callers
    that can't transform (e.g. videos, local rows) just use `public_url`/the proxy.
    S3 has no transformer, so there this is exactly `public_url(bucket, path)`.
    """
    if backend_name() == "s3":
        return public_url(bucket, path)
    if not config.supabase_url:
        raise AssetStorageError("SUPABASE_URL is not set in .env")
    base = config.supabase_url.rstrip("/")
    url = f"{base}/storage/v1/render/image/public/{bucket}/{path.lstrip('/')}"
    params: list[str] = []
    if width:
        params.append(f"width={int(width)}")
    if height:
        params.append(f"height={int(height)}")
    if quality:
        params.append(f"quality={int(quality)}")
    if resize:
        params.append(f"resize={resize}")
    return f"{url}?{'&'.join(params)}" if params else url


# Transform tiers are a FIXED ALLOWLIST, not an open integer. Every extra tier
# multiplies billed origin transformations and signed-token cardinality, and an
# attacker-controlled ?w= on a signing route is both a billing and a DoS vector.
# Both tiers set height AND resize=contain. Width alone leaves Supabase on its
# default `resizing_type: fill`, which KEEPS THE SOURCE HEIGHT and slices the
# width — a 1024x1024 image comes back 1024 tall and cropped. `contain` scales the
# longest side into the box and preserves aspect ratio.
THUMB_TRANSFORM = {"width": 480, "height": 480, "resize": "contain", "quality": 70}
PREVIEW_TRANSFORM = {"width": 1400, "height": 1400, "resize": "contain", "quality": 80}

# Supabase's transformer handles raster images only. SVG and video would 400,
# which the grid would render as a broken tile — so callers must skip them.
_TRANSFORMABLE_MIME_PREFIXES = ("image/",)
_NON_TRANSFORMABLE_MIMES = {"image/svg+xml", "image/gif"}


def can_transform(mime: str | None) -> bool:
    """True when Supabase's image transformer can handle this object's type."""
    if not mime:
        return False
    m = mime.lower().split(";")[0].strip()
    if m in _NON_TRANSFORMABLE_MIMES:
        return False
    return m.startswith(_TRANSFORMABLE_MIME_PREFIXES)


# Cache-key marker for "plain signed token, served off the /render/image path".
# Must differ from "" (a full-resolution /object/sign URL) or a thumbnail request
# and a download request would share one cache entry.
_RENDER_TIER = "render"
# ...and it must also differ from the NON-transformed thumbnail, or a capability
# flip would keep serving whichever URL shape was cached first for up to the
# token's full lifetime (~24h). Keying on the capability makes the switch take
# effect on the next request instead.
_PLAIN_TIER = "object"


def _thumb_tier() -> str:
    return _RENDER_TIER if image_transform_enabled() else _PLAIN_TIER
# Paths per batch-sign request. 100-in-one intermittently drew
# "Server disconnected without sending a response" from Supabase; smaller chunks
# run concurrently (so wall-clock is still ~one round trip) and a failed chunk
# costs only its own tiles.
_BATCH_SIGN_CHUNK = 40


def _to_render_path(signed_url: str) -> str:
    """Rewrite an /object/sign/... signed URL onto the image-render path.

    A PLAIN signed token (no `transform` claim) is accepted by
    /render/image/sign/ and still gets re-encoded to WebP by the transformer:
    measured 1,370,135 B PNG -> 63,220 B WebP, a 21.7x reduction, with NO extra
    sign call and no extra transform tier. Explicit ?width= params on such a token
    are silently ignored, so the size is whatever the re-encode yields.

    Returns the URL UNCHANGED when the transformer is unavailable: a plain
    /object/sign/ URL is bigger but it actually loads, whereas the rewritten one
    would 403 in the browser and blank the tile (#37).
    """
    if not image_transform_enabled():
        return signed_url
    return signed_url.replace("/object/sign/", "/render/image/sign/", 1)


async def _batch_sign_supabase(
    *, bucket: str, paths: list[str], expires: int,
) -> dict[str, str]:
    """Sign MANY paths in ONE request. Returns {path: absolute_signed_url}.

    This is the difference between a fast list response and an unusable one:
    signing 100 objects one-at-a-time took ~26s (and started tripping
    "Server disconnected" under concurrency), while one batch call signs 100 in
    ~0.67s. Per-path failures come back as {"error": ...} and are simply omitted,
    so the caller falls back to the proxy for those.

    NOTE: the batch endpoint does NOT honour a `transform` body field — it always
    returns plain download tokens. That is fine: see `_to_render_path`.
    """
    url = f"{_bucket_base()}/object/sign/{bucket}"
    out: dict[str, str] = {}
    if not paths:
        return out
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            url,
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"expiresIn": int(expires), "paths": paths},
        )
        if r.status_code >= 300:
            raise AssetStorageError(
                f"Supabase batch sign failed ({r.status_code}): {r.text[:200]}"
            )
        base = config.supabase_url.rstrip("/")
        for entry in r.json():
            if entry.get("error") or not entry.get("signedURL"):
                continue
            signed = entry["signedURL"]
            if signed.startswith("/"):
                signed = f"{base}/storage/v1{signed}"
            out[entry.get("path")] = signed
    return out


async def signed_render_urls(
    bucket: str, paths: list[str], *, expires: int = 86400,
) -> dict[str, str]:
    """Batch counterpart of `signed_render_url`: {path: render URL}.

    Cache-first (so a warm process makes zero network calls), then ONE batch sign
    for the misses. Never raises — on failure the misses are simply absent and the
    caller keeps its proxy URL, so a Supabase/quota problem degrades a tile to
    "slow", never to "blank".
    """
    if backend_name() == "s3":
        return await _s3_signed_urls(bucket, paths, expires)
    resolved: dict[str, str] = {}
    misses: list[str] = []
    for p in paths:
        hit = _signed_cache_get((bucket, p, int(expires), _thumb_tier()))
        if hit:
            resolved[p] = hit
        else:
            misses.append(p)
    if not misses:
        return resolved

    async def _chunk(paths: list[str]) -> dict[str, str]:
        """One batch call with a single retry. Supabase intermittently answers a
        large batch with 'Server disconnected without sending a response'; a
        retry after a short pause clears it. Failure returns {} so those tiles
        keep their proxy URL."""
        for attempt in (1, 2):
            try:
                return await _batch_sign_supabase(
                    bucket=bucket, paths=paths, expires=expires,
                )
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    log.info(
                        "Batch sign retry for %s (%d paths): %s",
                        bucket, len(paths), e,
                    )
                    import asyncio as _a
                    await _a.sleep(0.4)
                    continue
                log.warning(
                    "Batch sign failed for %s (%d paths): %s",
                    bucket, len(paths), e,
                )
        return {}

    # Chunked: one 100-path request proved unreliable, and a chunk that fails only
    # costs its own tiles rather than the whole grid. Chunks run concurrently, so
    # wall-clock stays ~one round trip.
    import asyncio as _asyncio
    chunks = [
        misses[i:i + _BATCH_SIGN_CHUNK]
        for i in range(0, len(misses), _BATCH_SIGN_CHUNK)
    ]
    for part in await _asyncio.gather(*(_chunk(c) for c in chunks)):
        for p, u in part.items():
            render = _to_render_path(u)
            _signed_cache_put(
                (bucket, p, int(expires), _thumb_tier()), render,
                max(30, int(expires) - _SIGNED_URL_SAFETY_MARGIN),
            )
            resolved[p] = render
    return resolved


async def _s3_signed_urls(bucket: str, paths: list[str], expires: int) -> dict[str, str]:
    """Offline presign per path with NO HEAD, because a grid would pay one round
    trip per tile. A missing key therefore still gets a URL (that 404s), which
    is why grids should only ask for objects known to exist, such as stored
    thumbnail derivatives (D9). Its own cache tier, so a URL minted here never
    lets url_for skip its HEAD. Never raises."""
    s3 = _s3()
    clamped = s3.clamp_expires(expires)
    tier = _thumb_tier()
    resolved: dict[str, str] = {}
    misses: list[str] = []
    for p in paths:
        hit = _signed_cache_get((bucket, p, clamped, tier, ""))
        if hit:
            resolved[p] = _to_public_origin(hit)
        else:
            misses.append(p)
    if not misses:
        return resolved

    def _sign_all() -> dict[str, str]:
        out: dict[str, str] = {}
        for p in misses:
            try:
                out[p] = s3.presign_get(bucket, p, expires=clamped)
            except Exception as e:  # noqa: BLE001
                log.info("S3 presign skipped for %s/%s: %s", bucket, p, e)
        return out

    try:
        # The first presign may build the client, which is slow enough (model
        # loading) to keep off the event loop.
        signed = await asyncio.to_thread(_sign_all)
    except Exception as e:  # noqa: BLE001
        log.warning("S3 batch presign failed for %s (%d paths): %s", bucket, len(misses), e)
        return resolved
    ttl = max(30, clamped - _SIGNED_URL_SAFETY_MARGIN)
    for p, u in signed.items():
        _signed_cache_put((bucket, p, clamped, tier, ""), u, ttl)
        resolved[p] = _to_public_origin(u)
    return resolved


async def signed_render_url(
    bucket: str,
    path: str,
    *,
    transform: dict,
    expires: int = 86400,
) -> str | None:
    """Signed IMAGE-TRANSFORM URL for an object in a PRIVATE bucket.

    The private-bucket counterpart of `public_render_url`: `/render/image/public`
    cannot serve a private bucket (400), but a token with the transform baked in
    can — `/render/image/sign/...` serves a ~20 kB WebP from a 1.4 MB PNG with no
    Authorization header, CDN-cached per URL.

    TTL note: signed transform responses carry NO `cache-control`, only `expires`
    — so BROWSER CACHEABILITY EQUALS THE TOKEN TTL. 24h is the deliberate default:
    a working day of cache hits, while bounding how long an auth-free bearer link
    to a private asset lives in list JSON and the DOM. Do not raise it to 7 days.
    Returns None on failure so callers fall back to the proxy.
    """
    return await url_for(
        storage_bucket=bucket, storage_path=path, local_path=None,
        expires=expires, transform=transform,
    )


async def upload_public(*, bucket: str, path: str, data: bytes, mime: str | None) -> str:
    """Upload bytes to a PUBLIC bucket and return the permanent public URL.

    No local fallback: raises AssetStorageError when the object store isn't
    configured or the upload fails. Callers should treat that as "not hosted".
    """
    if backend_name() == "s3":
        url = public_url(bucket, path)  # raises before writing if there is no public base
        await _s3().put_bytes(
            bucket, path, data, content_type=mime,
            cache_control=_s3_cache_control(bucket, None),
        )
        _signed_cache_evict(bucket, path)
        return url
    await _upload_supabase(bucket=bucket, path=path, data=data, mime=mime)
    return public_url(bucket, path)


async def put_s3_object(
    bucket: str,
    key: str,
    *,
    path: Path | str | None = None,
    data: bytes | None = None,
    mime: str | None,
    cache_control: str = CACHE_IMMUTABLE,
) -> str:
    """Write one object to the S3 store, with NO local fallback (A15).

    Exactly one of `path` (streamed with upload_file, never read into RAM) or
    `data`. `key` must already be valid (see `s3_backend.valid_key`); it is not
    sanitised here. Content-Type is `mime`, else guessed from the key. Returns
    the key. Raises AssetStorageError on any failure, and when the configured
    backend is not "s3". For writers that must not leave a stray local copy:
    the backfill script and the thumbnail derivatives.
    """
    if backend_name() != "s3":
        raise AssetStorageError("S3 storage is not the configured backend")
    if (path is None) == (data is None):
        raise AssetStorageError("put_s3_object needs exactly one of path= or data=")
    s3 = _s3()
    if path is not None:
        await s3.upload_file(bucket, key, path, content_type=mime, cache_control=cache_control)
    else:
        await s3.put_bytes(bucket, key, data, content_type=mime, cache_control=cache_control)
    _signed_cache_evict(bucket, key)
    return key


# ---------------------------------------------------------------------------
# Stored derivatives (S3). services/thumbs.py makes small WebP copies of bucket
# images under these prefixes; this module only knows their keys, remembers
# which exist, and drops them with their original. Positive-only memory: a
# derivative that exists is never re-checked until its original changes here.
# ---------------------------------------------------------------------------
DERIVATIVE_PREFIXES = ("thumbs", "previews")
# Raster types that get derivatives. Keys carry their extension (safe_ext), so
# the key alone tells; SVG/GIF are left whole, as can_transform says.
DERIVABLE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tif", ".tiff")
_DERIVATIVES_KNOWN: set[tuple[str, str]] = set()


def derivative_keys(storage_path: str) -> list[str]:
    """Every derivative key an original can have (thumbs.py builds the same)."""
    return [f"{prefix}/{storage_path}.webp" for prefix in DERIVATIVE_PREFIXES]


def derivative_known(bucket: str, key: str) -> bool:
    return (bucket, key) in _DERIVATIVES_KNOWN


def mark_derivative(bucket: str, key: str) -> None:
    _DERIVATIVES_KNOWN.add((bucket, key))


async def _drop_derivatives(bucket: str, storage_path: str) -> None:
    """Delete an original's derivatives after it was rewritten or deleted.
    S3 only, best effort, never raises. A derivative's own key has none, and
    neither does a video or a document."""
    if (
        backend_name() != "s3"
        or not storage_path.lower().endswith(DERIVABLE_EXTS)
        or storage_path.split("/", 1)[0] in DERIVATIVE_PREFIXES
    ):
        return
    for key in derivative_keys(storage_path):
        _DERIVATIVES_KNOWN.discard((bucket, key))
        _signed_cache_evict(bucket, key)
        try:
            await _s3().delete(bucket, key)
        except Exception as e:  # noqa: BLE001
            log.warning("S3 delete of derivative %s/%s failed: %s", bucket, key, e)


def clear_caches() -> None:
    """Forget every in-process storage cache: signed URLs, bucket publicness,
    the transform verdict, ensured buckets, known derivatives, and the S3
    clients + breaker. Tests call it between cases; also needed after changing
    storage config at runtime."""
    global _BUCKET_PUBLIC_FETCHED_AT, _bucket_public_lock
    global _IMAGE_TRANSFORM_OK, _IMAGE_TRANSFORM_CHECKED_AT, _image_transform_lock
    _SIGNED_URL_CACHE.clear()
    _DERIVATIVES_KNOWN.clear()
    _BUCKET_PUBLIC.clear()
    _BUCKET_PUBLIC_FETCHED_AT = 0.0
    _bucket_public_lock = None
    _IMAGE_TRANSFORM_OK = None
    _IMAGE_TRANSFORM_CHECKED_AT = 0.0
    _image_transform_lock = None
    _S3_BUCKETS_READY.clear()
    # Only if already imported: clearing must not pull in boto3.
    s3 = sys.modules.get(f"{__package__}.s3_backend")
    if s3 is not None:
        s3.reset()


__all__ = [
    "AssetStorageError",
    "AssetNotFoundError",
    "backend_name",
    "remote_enabled",
    "public_hosting_configured",
    "internal_url",
    "parse_public_url",
    "ascii_slug",
    "safe_ext",
    "make_slug",
    "upload",
    "persist_local_file",
    "url_for",
    "object_exists",
    "local_to_abs",
    "delete",
    "delete_public_urls",
    "ensure_public_bucket",
    "ensure_bucket",
    "bootstrap_storage",
    "download_bytes",
    "public_url",
    "public_render_url",
    "signed_render_url",
    "signed_render_urls",
    "upload_public",
    "put_s3_object",
    "clear_caches",
    "DERIVATIVE_PREFIXES",
    "DERIVABLE_EXTS",
    "derivative_keys",
    "derivative_known",
    "mark_derivative",
    "bucket_is_public",
    "ensure_bucket_publicness",
    "image_transform_enabled",
    "ensure_image_transform_capability",
    "can_transform",
    "transform_key",
    "THUMB_TRANSFORM",
    "PREVIEW_TRANSFORM",
    "CACHE_IMMUTABLE",
    "CACHE_MUTABLE",
    "CACHE_PUBLIC_IMMUTABLE",
]
