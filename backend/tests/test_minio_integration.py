"""Integration: asset_storage against a REAL MinIO behind the REAL Caddy /media route.

Deselected by default (pytest.ini) and skipped unless MINIO_TEST_ENDPOINT is
set. Run it from a container on the same docker network as services named
`minio` (pgsty/minio) and `caddy` (caddy:2-alpine with web/Caddyfile.local):

    docker run --rm --network <net> -v <repo>/backend:/app/backend -w /app/backend \\
      -e MINIO_TEST_ENDPOINT=http://minio:9000 \\
      -e MINIO_TEST_PUBLIC_BASE=http://caddy/media \\
      -e MINIO_TEST_ACCESS_KEY=<user> -e MINIO_TEST_SECRET_KEY=<password> \\
      python:3.11-slim sh -c "pip install -q boto3 httpx pytest python-dotenv && \\
        python -m pytest -q -p no:cacheprovider -m integration tests/test_minio_integration.py"

Environment:
  MINIO_TEST_ENDPOINT     internal S3 endpoint the backend signs against (http://minio:9000)
  MINIO_TEST_PUBLIC_BASE  the Caddy /media base browsers use (http://caddy/media)
  MINIO_TEST_ACCESS_KEY / MINIO_TEST_SECRET_KEY   credentials with bucket rights
  MINIO_TEST_REGION       default us-east-1 (must equal the server's region)

Covers the verification bar's item 2 and the A7/A8 hardening: presigned GET
through /media, tampering, download disposition, unicode keys, Range,
anonymous public reads, private-bucket denial, no listing, no admin paths, no
writes, no anonymous response-* overrides, and an idempotent bootstrap.
"""
from __future__ import annotations

import asyncio
import http.client
import os
import secrets
from urllib.parse import urlsplit

import pytest

ENDPOINT = os.getenv("MINIO_TEST_ENDPOINT", "").rstrip("/")
PUBLIC = os.getenv("MINIO_TEST_PUBLIC_BASE", "").rstrip("/")
ACCESS = os.getenv("MINIO_TEST_ACCESS_KEY", "")
SECRET = os.getenv("MINIO_TEST_SECRET_KEY", "")
REGION = os.getenv("MINIO_TEST_REGION", "us-east-1")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (ENDPOINT and PUBLIC and ACCESS and SECRET),
        reason="set MINIO_TEST_ENDPOINT, MINIO_TEST_PUBLIC_BASE, MINIO_TEST_ACCESS_KEY "
               "and MINIO_TEST_SECRET_KEY to run against a real MinIO + Caddy",
    ),
]


def _run(coro):
    return asyncio.run(coro)


def _fetch(url: str, method: str = "GET", headers: dict | None = None, body: bytes | None = None):
    """Raw HTTP so the URL goes out byte for byte (no client re-encoding)."""
    parts = urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port, timeout=20)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request(method, target, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def _from_minio(headers: dict) -> bool:
    # MinIO stamps every response; Caddy's own `respond 404` does not.
    return "x-amz-request-id" in headers


@pytest.fixture
def live():
    """S3 mode against the live MinIO, with the buckets bootstrapped."""
    import boto3
    from botocore.config import Config as BotoConfig

    from app.config import config
    from app.services import asset_storage

    for name, value in {
        "storage_backend": "s3", "s3_endpoint": ENDPOINT, "s3_public_base_url": PUBLIC,
        "s3_access_key": ACCESS, "s3_secret_key": SECRET, "s3_region": REGION,
    }.items():
        object.__setattr__(config, name, value)
    asset_storage.clear_caches()
    _run(asset_storage.bootstrap_storage())
    client = boto3.session.Session().client(
        "s3", endpoint_url=ENDPOINT, aws_access_key_id=ACCESS, aws_secret_access_key=SECRET,
        region_name=REGION,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
    )
    # The autouse conftest fixture restores the config and clears the caches.
    yield client


def _key(prefix: str, ext: str = "bin") -> str:
    return f"it/{prefix}_{secrets.token_hex(4)}.{ext}"


def _upload(data: bytes, key: str, mime: str, bucket: str | None = None) -> dict:
    from app.services import asset_storage

    res = _run(asset_storage.upload(data=data, path=key, mime=mime, bucket=bucket))
    assert res["storage"] == "s3", res
    return res


def _signed(key: str, bucket: str | None = None, **kw) -> str:
    from app.config import config
    from app.services import asset_storage

    url = _run(asset_storage.url_for(
        storage_bucket=bucket or config.producer_assets_bucket, storage_path=key,
        local_path=None, **kw,
    ))
    assert url, f"no signed URL for {key}"
    return url


# --- bootstrap ---------------------------------------------------------------

def test_bootstrap_is_idempotent_and_only_approved_is_public(live):
    import json

    from app.config import config
    from app.services import asset_storage

    _run(asset_storage.bootstrap_storage())
    asset_storage.clear_caches()
    _run(asset_storage.bootstrap_storage())
    names = {b["Name"] for b in live.list_buckets()["Buckets"]}
    for bucket in (config.producer_assets_bucket, config.producer_media_bucket,
                   config.brand_docs_bucket, config.approved_media_bucket):
        assert bucket in names
    stmts = json.loads(live.get_bucket_policy(Bucket=config.approved_media_bucket)["Policy"])["Statement"]
    actions = {a for s in stmts for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert actions == {"s3:GetObject"}, "anonymous access must be GetObject only (no ListBucket)"
    assert asset_storage.bucket_is_public(config.approved_media_bucket)
    assert not asset_storage.bucket_is_public(config.producer_media_bucket)


# --- private objects through /media --------------------------------------------

def test_private_presign_through_media(live):
    body = os.urandom(4096)
    key = _key("private", "png")
    _upload(body, key, "image/png")
    url = _signed(key)
    assert url.startswith(f"{PUBLIC}/")
    status, headers, data = _fetch(url)
    assert status == 200 and data == body
    assert headers["content-type"] == "image/png"
    assert headers.get("x-content-type-options", "").lower() == "nosniff"


def test_tampered_signed_url_is_403(live):
    key = _key("tamper")
    _upload(b"x" * 100, key, "application/octet-stream")
    status, headers, _ = _fetch(_signed(key) + "&v=1")
    assert status == 403 and _from_minio(headers)


def test_download_disposition_is_signed(live):
    from app.services import s3_backend

    key = _key("dl", "pdf")
    _upload(b"%PDF-1.4 test", key, "application/pdf")
    name = 'تقرير "final";2026.pdf'
    status, headers, _ = _fetch(_signed(key, download_name=name))
    assert status == 200
    assert headers["content-disposition"] == s3_backend.content_disposition(name)
    # Without a download name the PDF stays inline (the brand-doc iframe needs it).
    status, headers, _ = _fetch(_signed(key))
    assert status == 200 and "content-disposition" not in headers


def test_unicode_keys_round_trip(live):
    from app.config import config
    from app.services import asset_storage

    # Through upload(): the key is sanitised to ASCII and still round-trips.
    res = _upload(b"uni", f"it/صورة عربية a+b {secrets.token_hex(3)}.png", "image/png")
    assert res["storage_path"].isascii()
    status, _, data = _fetch(_signed(res["storage_path"]))
    assert status == 200 and data == b"uni"

    # A raw unicode key (written outside the app) still survives Caddy's path
    # handling and SigV4 when presigned the same way.
    raw_key = f"it/raw صورة x+y {secrets.token_hex(3)}.png"
    live.put_object(Bucket=config.producer_assets_bucket, Key=raw_key, Body=b"raw",
                    ContentType="image/png")
    internal = live.generate_presigned_url(
        "get_object", Params={"Bucket": config.producer_assets_bucket, "Key": raw_key},
        ExpiresIn=600,
    )
    status, _, data = _fetch(PUBLIC + internal[len(ENDPOINT):])
    assert status == 200 and data == b"raw"

    # Same for an anonymous public object at its quoted public_url.
    live.put_object(Bucket=config.approved_media_bucket, Key=raw_key, Body=b"pub",
                    ContentType="image/jpeg")
    url = asset_storage.public_url(config.approved_media_bucket, raw_key)
    status, _, data = _fetch(url)
    assert status == 200 and data == b"pub"
    assert asset_storage.parse_public_url(url + "?v=1") == (config.approved_media_bucket, raw_key)


def test_range_request_is_206(live):
    body = bytes(range(256)) * 64
    key = _key("range")
    _upload(body, key, "video/mp4")
    status, headers, data = _fetch(_signed(key), headers={"Range": "bytes=10-19"})
    assert status == 206
    assert data == body[10:20]
    assert headers["content-range"].startswith("bytes 10-19/")


def test_internal_url_is_served_directly(live):
    key = _key("internal")
    _upload(b"server-side", key, "application/octet-stream")
    url = _signed(key, internal=True)
    assert url.startswith(f"{ENDPOINT}/")
    status, _, data = _fetch(url)
    assert status == 200 and data == b"server-side"


def test_expires_is_clamped_to_the_sigv4_maximum(live):
    key = _key("expires")
    _upload(b"e", key, "application/octet-stream")
    url = _signed(key, expires=10 ** 7)
    assert "X-Amz-Expires=604800" in url
    assert _fetch(url)[0] == 200


def test_missing_object_gives_none(live):
    from app.config import config
    from app.services import asset_storage

    b = config.producer_assets_bucket
    assert _run(asset_storage.url_for(storage_bucket=b, storage_path="it/definitely-missing.png",
                                      local_path=None)) is None
    assert _run(asset_storage.object_exists(bucket=b, path="it/definitely-missing.png")) is False


def test_active_types_are_download_only(live):
    key = _key("active", "svg")
    _upload(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            key, "image/svg+xml")
    status, headers, _ = _fetch(_signed(key))
    assert status == 200
    assert headers["content-type"] == "image/svg+xml"
    assert headers["content-disposition"] == "attachment"
    assert headers.get("x-content-type-options", "").lower() == "nosniff"


def test_persist_local_file_and_download_bytes(live, tmp_path):
    from app.services import asset_storage

    body = os.urandom(9 * 1024 * 1024 + 123)  # multipart upload_file
    f = tmp_path / "render.mp4"
    f.write_bytes(body)
    bucket, key = _run(asset_storage.persist_local_file(abs_path=f, bucket_key=_key("render", "mp4")))
    assert bucket and key
    got = _run(asset_storage.download_bytes(storage_bucket=bucket, storage_path=key))
    assert got == body
    status, headers, data = _fetch(_signed(key, bucket=bucket),
                                   headers={"Range": f"bytes={len(body) - 10}-"})
    assert status == 206 and data == body[-10:]
    assert headers["content-type"] == "video/mp4"


def test_signed_render_urls_through_media(live):
    from app.config import config
    from app.services import asset_storage

    key = _key("grid", "webp")
    _upload(b"webp", key, "image/webp")
    out = _run(asset_storage.signed_render_urls(config.producer_assets_bucket, [key]))
    status, _, data = _fetch(out[key])
    assert status == 200 and data == b"webp"


# --- the public (Instagram) bucket ---------------------------------------------

def test_public_object_is_anonymously_readable(live):
    from app.config import config
    from app.services import asset_storage

    key = _key("ig", "jpg")
    url = _run(asset_storage.upload_public(bucket=config.approved_media_bucket, path=key,
                                           data=b"\xff\xd8jpeg", mime="image/jpeg"))
    assert url == f"{PUBLIC}/{config.approved_media_bucket}/{key}"
    status, headers, data = _fetch(url)
    assert status == 200 and data == b"\xff\xd8jpeg"
    assert headers["content-type"] == "image/jpeg"
    assert headers["cache-control"] == "public, max-age=31536000, immutable"
    assert _fetch(url + "?v=1")[0] == 200
    assert _fetch(url, "HEAD")[0] == 200


def test_anonymous_response_overrides_are_blocked(live):
    """A7: on the app's own origin nobody may choose how public bytes render."""
    from app.config import config
    from app.services import asset_storage

    key = _key("override", "jpg")
    url = _run(asset_storage.upload_public(bucket=config.approved_media_bucket, path=key,
                                           data=b"jpeg", mime="image/jpeg"))
    for q in ("response-content-type=text/html",
              "response-content-disposition=inline",
              "v=1&response-cache-control=no-store",
              # MinIO matches the key in any case, and only X-Amz-Credential
              # (exact case) puts it in presigned mode, so none of these may
              # pass as "signed".
              "Response-Content-Type=text/html",
              "response-content-type=text/html&X-Amz-Signature=x",
              "response-content-type=text/html&x-amz-credential=x"):
        status, headers, _ = _fetch(f"{url}?{q}")
        assert status == 404 and not _from_minio(headers), q


def test_encoded_response_overrides_are_dropped(live):
    """A7: MinIO also decodes percent-encoded keys, which a plain-text matcher
    cannot see. An unsigned request's query is dropped, so the stored type wins."""
    from app.config import config
    from app.services import asset_storage

    key = _key("encoded", "jpg")
    url = _run(asset_storage.upload_public(bucket=config.approved_media_bucket, path=key,
                                           data=b"jpeg", mime="image/jpeg"))
    for q in ("%72esponse-content-type=text/html", "response%2Dcontent%2Dtype=text/html"):
        status, headers, data = _fetch(f"{url}?{q}")
        assert status == 200 and data == b"jpeg", q
        assert headers["content-type"] == "image/jpeg", q
    # An encoded credential key reaches MinIO's presigned check and fails there.
    status, headers, _ = _fetch(f"{url}?X%2DAmz-Credential=x&response-content-type=text/html")
    assert status in (400, 403), status
    assert "text/html" not in headers.get("content-type", "")


def test_private_bucket_is_not_anonymous(live):
    from app.config import config

    key = _key("anon")
    _upload(b"secret-ish", key, "application/octet-stream")
    status, headers, _ = _fetch(f"{PUBLIC}/{config.producer_assets_bucket}/{key}")
    assert status == 403 and _from_minio(headers)


def test_bucket_listing_is_blocked(live):
    from app.config import config

    ab = config.approved_media_bucket
    for path in (f"/{ab}/", f"/{ab}", f"/{ab}/?list-type=2"):
        status, headers, _ = _fetch(PUBLIC + path)
        assert status == 404 and not _from_minio(headers), path
    # And MinIO itself refuses an anonymous listing even if a route reached it.
    status, headers, _ = _fetch(f"{ENDPOINT}/{ab}/")
    assert status == 403 and _from_minio(headers)


def test_minio_admin_paths_are_blocked(live):
    for path in ("/minio/health/live", "/minio/health/ready", "/minio/v2/metrics/cluster"):
        status, headers, _ = _fetch(PUBLIC + path)
        assert status == 404 and not _from_minio(headers), path


def test_writes_through_media_are_refused(live):
    from app.config import config
    from app.services import asset_storage

    key = _key("nowrite", "jpg")
    url = _run(asset_storage.upload_public(bucket=config.approved_media_bucket, path=key,
                                           data=b"original", mime="image/jpeg"))
    for method in ("PUT", "POST", "DELETE"):
        status, _, _ = _fetch(url, method, body=b"evil" if method != "DELETE" else None)
        assert status in (404, 405), method
    status, _, data = _fetch(url)
    assert status == 200 and data == b"original"


def test_delete_public_urls_removes_hosted_objects(live):
    from app.config import config
    from app.services import asset_storage

    ab = config.approved_media_bucket
    urls = [
        _run(asset_storage.upload_public(bucket=ab, path=_key(f"slide{i}", "jpg"),
                                         data=b"s", mime="image/jpeg"))
        for i in range(3)
    ]
    _run(asset_storage.delete_public_urls([urls[0], urls[1] + "?v=1", urls[2], "junk"]))
    for url in urls:
        bucket, key = asset_storage.parse_public_url(url)
        assert _run(asset_storage.object_exists(bucket=bucket, path=key)) is False
