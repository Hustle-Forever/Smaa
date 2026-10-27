"""S3 transport for asset_storage (MinIO in the compose stack, any S3 later).

asset_storage owns the policy (which bucket, cache rules, local fallback, the
signed-URL cache). This module owns the wire: boto3 clients, key checks, and
turning every boto3/botocore failure into AssetStorageError, because upload()
only falls back to local disk on that one exception type (D8).

Two clients (A18). The FAST one serves request paths (HEAD, small puts,
deletes, presign) with short timeouts and a single attempt, so a dead MinIO
costs a tile one short timeout instead of several 60 s ones. The BULK one
(retries, 60 s reads) moves big objects: large puts, upload_file, downloads.

A connection-level failure opens a 30 s circuit breaker. Until it closes every
network call fails fast with AssetStorageError (url_for -> None, upload ->
local disk) instead of stalling each request on the same dead endpoint.
Presigning is offline and is never blocked by it.

Public functions are async (they run boto3 in a worker thread) except the
offline helpers: presign_get, the key checks and the header builders.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError

from ..config import config
from .asset_storage import AssetNotFoundError, AssetStorageError

log = logging.getLogger("archivist.s3_backend")

# SigV4 caps a presigned URL at 7 days; MinIO answers 400 just above it (D11).
MAX_PRESIGN_EXPIRES = 604800
# Puts at or above this size go through the retrying bulk client.
SMALL_PUT_BYTES = 8 * 1024 * 1024
BREAKER_SECONDS = 30.0

# A4: ASCII segments joined by single slashes. Anything else either breaks the
# signature through Caddy (a leading or doubled slash is rewritten by its path
# cleaning) or is refused by MinIO ('.' / '..' segments).
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
_BAD_KEY_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")

# /media shares the app's origin, so nothing a browser could open as a page
# (HTML, SVG, XML, XSL, ...) may render inline there (A8). Only these passive
# types are served inline; every other type, and any value that is not exactly
# one well-formed type, is stored with Content-Disposition: attachment. An
# allowlist, because the types a browser runs as a document are too many to
# list. PDFs stay inline for the brand-doc viewer.
_INLINE_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif", "image/avif",
    "application/pdf", "text/plain",
}
_INLINE_PREFIXES = ("video/", "audio/", "font/")
# type/subtype in RFC 6838 restricted-name characters.
_ESSENCE_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")
# mimetypes reads the Windows registry and misses some of these there.
_TYPES_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4",
    ".mov": "video/quicktime", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".wav": "audio/wav", ".pdf": "application/pdf", ".json": "application/json",
}

_COMMON_CONFIG = {
    "signature_version": "s3v4",
    "s3": {"addressing_style": "path"},
    # MinIO and older S3 clones reject the newer default checksum trailers.
    "request_checksum_calculation": "when_required",
    "response_checksum_validation": "when_required",
}
# total_max_attempts=1 is one try and no retry. botocore reads a plain
# max_attempts=1 as "one retry", which doubles the wait on a dead endpoint.
_FAST_CONFIG = {"connect_timeout": 2, "read_timeout": 10,
                "retries": {"total_max_attempts": 1, "mode": "standard"}}
_BULK_CONFIG = {"connect_timeout": 5, "read_timeout": 60,
                "retries": {"max_attempts": 3, "mode": "standard"}}

_clients: dict[tuple, object] = {}
_clients_lock = threading.Lock()
_breaker_until = 0.0


def reset() -> None:
    """Drop cached clients and close the breaker (tests, config changes)."""
    global _breaker_until
    with _clients_lock:
        _clients.clear()
    _breaker_until = 0.0


# ---------------------------------------------------------------------------
# Keys, buckets and headers (pure, no I/O)
# ---------------------------------------------------------------------------

def valid_key(key: object) -> bool:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        return False
    return all(seg not in (".", "..") for seg in key.split("/"))


def check_key(key: object) -> str:
    """Return `key` unchanged, or raise AssetStorageError before any I/O."""
    if not valid_key(key):
        raise AssetStorageError(
            f"invalid object key {key!r}: use A-Z a-z 0-9 _ . - with single '/' separators"
        )
    return key  # type: ignore[return-value]


def sanitize_key(key: str) -> str:
    """Nearest valid key: drop empty, '.' and '..' segments and replace every
    other character with '_'. May return "" when nothing usable is left."""
    segs = []
    for seg in str(key or "").replace("\\", "/").split("/"):
        seg = _BAD_KEY_CHARS.sub("_", seg)
        if seg and seg not in (".", ".."):
            segs.append(seg)
    return "/".join(segs)


def check_bucket(bucket: object) -> str:
    if not isinstance(bucket, str) or not _BUCKET_RE.fullmatch(bucket):
        raise AssetStorageError(f"invalid bucket name {bucket!r}")
    return bucket


def clamp_expires(expires: int | float) -> int:
    return max(1, min(MAX_PRESIGN_EXPIRES, int(expires)))


def content_type_for(key: str, mime: str | None) -> str:
    """`mime` when given, else a guess from the key, else octet-stream. Always
    sent, because MinIO answers with nosniff and replays the stored type."""
    if mime and mime.strip():
        return mime.strip()
    ext = Path(key).suffix.lower()
    return _TYPES_BY_EXT.get(ext) or mimetypes.guess_type(key)[0] or "application/octet-stream"


def mime_essence(content_type: str | None) -> str:
    """Lower-cased type/subtype without parameters, or "" unless the value is
    exactly one well-formed type. Browsers read a comma as a list of types
    and may act on any entry, so a list never qualifies."""
    value = (content_type or "").strip().lower()
    if "," in value:
        return ""
    essence = value.split(";", 1)[0].strip()
    return essence if _ESSENCE_RE.fullmatch(essence) else ""


def is_inline_type(content_type: str | None) -> bool:
    essence = mime_essence(content_type)
    return essence in _INLINE_TYPES or essence.startswith(_INLINE_PREFIXES)


def is_active_type(content_type: str | None) -> bool:
    """True for every type not on the inline allowlist (A8), unknown and
    malformed values included."""
    return not is_inline_type(content_type)


def object_headers(content_type: str, cache_control: str | None) -> dict:
    """ExtraArgs for put_object / upload_file. A value that is not exactly one
    well-formed type is stored as application/octet-stream, so MinIO never
    replays it to a browser."""
    if not mime_essence(content_type):
        content_type = "application/octet-stream"
    args = {"ContentType": content_type}
    if cache_control:
        args["CacheControl"] = cache_control
    if is_active_type(content_type):
        args["ContentDisposition"] = "attachment"
    return args


def ascii_filename(name: str) -> str:
    """A11: the quoted `filename=` fallback. Printable ASCII only, minus the
    characters that would break the quoted-string or the header."""
    out = "".join(
        ch for ch in (name or "")
        if 0x20 <= ord(ch) < 0x7F and ch not in '"\\;'
    ).strip()
    if not out:
        return "download"
    # An Arabic name leaves only its extension behind.
    return f"download{out}" if out.startswith(".") else out


def content_disposition(name: str) -> str:
    """`attachment` with an ASCII fallback plus the exact UTF-8 name (RFC 6266)."""
    utf8 = "".join(ch for ch in (name or "") if ch.isprintable()).strip() or "download"
    return (
        f'attachment; filename="{ascii_filename(name)}"; '
        f"filename*=UTF-8''{quote(utf8, safe='')}"
    )


def public_read_policy(bucket: str) -> str:
    """Anonymous s3:GetObject on the bucket's objects and nothing else: no
    ListBucket, so keys cannot be enumerated (D5)."""
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "PublicReadObjects",
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{bucket}/*"],
        }],
    })


# ---------------------------------------------------------------------------
# Clients, breaker, error wrapping
# ---------------------------------------------------------------------------

def _client(kind: str):
    """Cached client for `kind` ('fast' or 'bulk'). Keyed on the S3 settings so
    a config change (tests) builds a fresh one."""
    if not config.s3_endpoint:
        raise AssetStorageError("S3_ENDPOINT is not set")
    ident = (kind, config.s3_endpoint, config.s3_access_key, config.s3_secret_key,
             config.s3_region)
    client = _clients.get(ident)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(ident)
        if client is None:
            tuning = _FAST_CONFIG if kind == "fast" else _BULK_CONFIG
            # A private Session per client: the default session is not safe to
            # build clients from concurrently, and these run in worker threads.
            client = boto3.session.Session().client(
                "s3",
                endpoint_url=config.s3_endpoint,
                # None (not "") lets boto3 use its normal credential chain, e.g.
                # an instance role against real S3.
                aws_access_key_id=config.s3_access_key or None,
                aws_secret_access_key=config.s3_secret_key or None,
                region_name=config.s3_region or "us-east-1",
                config=BotoConfig(**_COMMON_CONFIG, **tuning),
            )
            _clients[ident] = client
    return client


def breaker_open() -> bool:
    return time.monotonic() < _breaker_until


def _trip_breaker(exc: Exception) -> None:
    global _breaker_until
    if not breaker_open():
        log.warning(
            "S3 endpoint %s unreachable (%s); failing fast for %ds",
            config.s3_endpoint, exc, int(BREAKER_SECONDS),
        )
    _breaker_until = time.monotonic() + BREAKER_SECONDS


def _error_code(e: ClientError) -> str:
    return str((e.response or {}).get("Error", {}).get("Code") or "")


def _http_status(e: ClientError) -> int:
    return int((e.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)


def _is_not_found(e: ClientError) -> bool:
    # HEAD has no body, so a missing key comes back as a bare "404", never as
    # NoSuchKey (A3). Check the status too.
    return _error_code(e) in {"404", "NoSuchKey", "NotFound"} or _http_status(e) == 404


def _call(kind: str, op: str, bucket: str, key: str, fn):
    """Run `fn(client)` with the breaker check and the error mapping."""
    if breaker_open():
        raise AssetStorageError(f"S3 {op} {bucket}/{key}: storage unreachable, retrying shortly")
    try:
        return fn(_client(kind))
    except AssetStorageError:
        raise
    except ClientError as e:
        msg = (e.response or {}).get("Error", {}).get("Message") or ""
        raise AssetStorageError(
            f"S3 {op} {bucket}/{key} failed: {_error_code(e) or _http_status(e)} {msg}".rstrip()
        ) from e
    except (BotoConnectionError, HTTPClientError) as e:
        _trip_breaker(e)
        raise AssetStorageError(f"S3 {op} {bucket}/{key}: endpoint unreachable ({e})") from e
    except Exception as e:  # noqa: BLE001 - boto3 raises many types; all mean "failed"
        raise AssetStorageError(f"S3 {op} {bucket}/{key} failed: {e}") from e


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

async def put_bytes(
    bucket: str, key: str, data: bytes, *,
    content_type: str | None = None, cache_control: str | None = None,
) -> None:
    check_bucket(bucket)
    check_key(key)
    extra = object_headers(content_type_for(key, content_type), cache_control)
    kind = "fast" if len(data) < SMALL_PUT_BYTES else "bulk"

    def _put(c):
        c.put_object(Bucket=bucket, Key=key, Body=data, **extra)

    await asyncio.to_thread(_call, kind, "put", bucket, key, _put)


async def upload_file(
    bucket: str, key: str, path: Path | str, *,
    content_type: str | None = None, cache_control: str | None = None,
) -> None:
    """Stream a local file (multipart when large) without reading it into RAM."""
    check_bucket(bucket)
    check_key(key)
    extra = object_headers(content_type_for(key, content_type), cache_control)

    def _upload(c):
        c.upload_file(Filename=str(path), Bucket=bucket, Key=key, ExtraArgs=extra)

    await asyncio.to_thread(_call, "bulk", "upload_file", bucket, key, _upload)


async def get_bytes(bucket: str, key: str) -> bytes:
    """The object's bytes. AssetNotFoundError when the key (or the bucket) is
    missing, AssetStorageError on anything else, which may be transient."""
    check_bucket(bucket)
    check_key(key)

    def _get(c):
        try:
            body = c.get_object(Bucket=bucket, Key=key)["Body"]
        except ClientError as e:
            if _is_not_found(e):
                raise AssetNotFoundError(f"S3 get {bucket}/{key}: no such object") from e
            raise
        try:
            return body.read()
        finally:
            body.close()

    return await asyncio.to_thread(_call, "bulk", "get", bucket, key, _get)


async def head(bucket: str, key: str) -> bool:
    """True when the object exists, False on 404, AssetStorageError otherwise."""
    check_bucket(bucket)
    check_key(key)

    def _head(c):
        try:
            c.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            if _is_not_found(e):
                return False
            raise

    return await asyncio.to_thread(_call, "fast", "head", bucket, key, _head)


async def delete(bucket: str, key: str) -> None:
    check_bucket(bucket)
    check_key(key)

    def _delete(c):
        c.delete_object(Bucket=bucket, Key=key)

    await asyncio.to_thread(_call, "fast", "delete", bucket, key, _delete)


async def list_keys(bucket: str, prefix: str = "") -> list[str]:
    """Every key in the bucket (under `prefix`). For maintenance scripts only:
    request paths never list."""
    check_bucket(bucket)

    def _list(c):
        keys: list[str] = []
        for page in c.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        return keys

    return await asyncio.to_thread(_call, "bulk", "list", bucket, prefix or "*", _list)


async def create_bucket(bucket: str) -> None:
    """Idempotent. 'Already owned' is success, and so is AccessDenied when the
    bucket is already there: a least-privilege app user may lack CreateBucket
    once minio-init has made the buckets (A9)."""
    check_bucket(bucket)
    region = config.s3_region or "us-east-1"

    def _create(c):
        kwargs = {"Bucket": bucket}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            c.create_bucket(**kwargs)
        except ClientError as e:
            code = _error_code(e)
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                return
            if code == "AccessDenied" or _http_status(e) == 403:
                try:
                    c.head_bucket(Bucket=bucket)
                    return
                except ClientError:
                    pass
            raise

    await asyncio.to_thread(_call, "fast", "create_bucket", bucket, "", _create)


def grants_public_get(policy_json: str, bucket: str) -> bool:
    """True when the policy lets anyone GetObject every key in `bucket`."""
    try:
        statements = json.loads(policy_json).get("Statement") or []
    except (ValueError, AttributeError):
        return False
    if isinstance(statements, dict):
        statements = [statements]

    def _as_list(v):
        return v if isinstance(v, list) else [v]

    for st in statements:
        principal = st.get("Principal")
        if isinstance(principal, dict):
            principal = principal.get("AWS")
        if (st.get("Effect") == "Allow"
                and "*" in _as_list(principal)
                and "s3:GetObject" in _as_list(st.get("Action"))
                and f"arn:aws:s3:::{bucket}/*" in _as_list(st.get("Resource"))):
            return True
    return False


async def set_public_read(bucket: str) -> None:
    """Attach the GetObject-only anonymous policy (replaces any other policy).
    If the app user may not set policies but one granting anonymous GetObject
    is already there (minio-init sets it, A9), that counts as done."""
    check_bucket(bucket)
    policy = public_read_policy(bucket)

    def _put_policy(c):
        try:
            c.put_bucket_policy(Bucket=bucket, Policy=policy)
        except ClientError as e:
            if not (_error_code(e) == "AccessDenied" or _http_status(e) == 403):
                raise
            try:
                current = c.get_bucket_policy(Bucket=bucket).get("Policy") or ""
            except ClientError:
                raise e from None
            if not grants_public_get(current, bucket):
                raise

    await asyncio.to_thread(_call, "fast", "put_bucket_policy", bucket, "", _put_policy)


def presign_get(
    bucket: str, key: str, *, expires: int, download_name: str | None = None,
) -> str:
    """Offline SigV4 GET URL against the INTERNAL endpoint (S3_ENDPOINT).

    asset_storage swaps the origin for the public one afterwards; the signature
    survives because Caddy strips /media and sends Host: minio:9000 (D4). The
    URL must never be changed after this: every query parameter is signed.
    """
    check_bucket(bucket)
    check_key(key)
    params = {"Bucket": bucket, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = content_disposition(download_name)
    try:
        return _client("fast").generate_presigned_url(
            "get_object", Params=params, ExpiresIn=clamp_expires(expires),
        )
    except AssetStorageError:
        raise
    except Exception as e:  # noqa: BLE001
        raise AssetStorageError(f"S3 presign {bucket}/{key} failed: {e}") from e


__all__ = [
    "MAX_PRESIGN_EXPIRES",
    "SMALL_PUT_BYTES",
    "BREAKER_SECONDS",
    "reset",
    "valid_key",
    "check_key",
    "sanitize_key",
    "check_bucket",
    "clamp_expires",
    "content_type_for",
    "mime_essence",
    "is_inline_type",
    "is_active_type",
    "object_headers",
    "ascii_filename",
    "content_disposition",
    "public_read_policy",
    "grants_public_get",
    "breaker_open",
    "put_bytes",
    "upload_file",
    "get_bytes",
    "head",
    "delete",
    "create_bucket",
    "set_public_read",
    "presign_get",
]
