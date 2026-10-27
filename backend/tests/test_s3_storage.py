"""Offline tests for the S3 (MinIO) storage backend.

asset_storage dispatches on `backend_name()`; in S3 mode it talks to
`s3_backend` (boto3). These run against moto's in-process S3 (the `s3_storage`
fixture in conftest) or against monkeypatched transport functions (the
`s3_config` fixture), never the network. The real MinIO + Caddy checks live in
test_minio_integration.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from app.config import config
from app.services import asset_storage as a
from app.services import s3_backend as s3

ENDPOINT = "http://minio.test:9000"
PUBLIC = "https://studio.test/media"


def _run(coro):
    return asyncio.run(coro)


def _set(**values) -> None:
    # The autouse fixture in conftest restores every storage field afterwards.
    for name, value in values.items():
        object.__setattr__(config, name, value)


def _client_error(code: str, status: int, op: str = "HeadObject") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


class _FakeClient:
    """Stands in for a boto3 client: every listed method raises `exc`."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    def _boom(self, *args, **kwargs):
        self.calls += 1
        raise self.exc

    head_object = put_object = get_object = delete_object = create_bucket = _boom
    put_bucket_policy = upload_file = head_bucket = _boom


@pytest.fixture
def tmp_assets_root(tmp_path):
    """Point the local-disk fallback at a temp dir (config is frozen, so no
    monkeypatch.setattr)."""
    root = tmp_path / "data" / "assets_library"
    old = config.producer_assets_root
    object.__setattr__(config, "producer_assets_root", root)
    try:
        yield root
    finally:
        object.__setattr__(config, "producer_assets_root", old)


def _files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# --- backend selection and config --------------------------------------------

def test_backend_name_resolution():
    assert a.backend_name() == "local" and a.remote_enabled() is False
    _set(supabase_service_role_key="k")
    assert a.backend_name() == "supabase" and a.remote_enabled() is True
    _set(s3_endpoint=ENDPOINT)
    assert a.backend_name() == "s3", "auto prefers S3 when an endpoint is set"
    _set(storage_backend="supabase")
    assert a.backend_name() == "supabase"
    _set(storage_backend="local")
    assert a.backend_name() == "local" and a.remote_enabled() is False
    _set(storage_backend="s3", s3_endpoint="")
    assert a.backend_name() == "s3"


def test_config_reads_storage_env(monkeypatch):
    from app.config import Config

    monkeypatch.setenv("STORAGE_BACKEND", " S3 ")
    monkeypatch.setenv("S3_ENDPOINT", "http://minio:9000/")
    monkeypatch.setenv("S3_PUBLIC_BASE_URL", "https://api.example.test/media/")
    monkeypatch.setenv("S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("S3_SECRET_KEY", "sk")
    monkeypatch.delenv("S3_REGION", raising=False)
    cfg = Config.from_env()
    assert cfg.storage_backend == "s3"
    assert cfg.s3_endpoint == "http://minio:9000"
    assert cfg.s3_public_base_url == "https://api.example.test/media"
    assert (cfg.s3_access_key, cfg.s3_secret_key, cfg.s3_region) == ("ak", "sk", "us-east-1")
    # A typo must not select a backend nobody asked for.
    monkeypatch.setenv("STORAGE_BACKEND", "minio")
    assert Config.from_env().storage_backend == "auto"


def test_s3_backend_is_not_imported_outside_s3_mode():
    """Dev machines on Supabase/local must not need boto3."""
    code = (
        "import sys; sys.path.insert(0, '.');"
        "from app.services import asset_storage as a;"
        "a.clear_caches(); a.image_transform_enabled(); a.public_hosting_configured();"
        "print('app.services.s3_backend' in sys.modules, 'boto3' in sys.modules)"
    )
    backend = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k not in ("S3_ENDPOINT", "STORAGE_BACKEND")}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=backend, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "False False"


# --- key rules (A4) and slug helpers -----------------------------------------

@pytest.mark.parametrize("key", [
    "a.png", "poster/12.png", "yas/generations/7.webp", "thumbs/a/b.png.webp",
    "a-b_c.d/E-F", "..a/b..",
])
def test_valid_keys(key):
    assert s3.valid_key(key)
    assert s3.check_key(key) == key


@pytest.mark.parametrize("key", [
    "", "/leading.png", "trailing/", "a//b.png", "a/./b.png", "a/../b.png", ".", "..",
    "صورة.png", "with space.png", "a+b.png", "a%20b.png", "a\\b.png", "a\nb", None, 5,
])
def test_invalid_keys(key):
    assert not s3.valid_key(key)
    with pytest.raises(a.AssetStorageError):
        s3.check_key(key)


def test_sanitize_key_backstop():
    assert s3.sanitize_key("/x//a b+c/../d.png") == "x/a_b_c/d.png"
    assert s3.sanitize_key("./a/./b") == "a/b"
    assert s3.sanitize_key("صورة.png") == "____.png"
    assert s3.sanitize_key("a\\b.png") == "a/b.png"
    assert s3.sanitize_key("/../") == ""
    for raw in ("/x//a b+c/../d.png", "مجلد/صورة final.jpg", "a/%2e%2e/b"):
        assert s3.valid_key(s3.sanitize_key(raw))


def test_every_s3_operation_checks_the_key_before_any_io(monkeypatch, s3_config):
    def _no_client(kind):
        raise AssertionError("a client was requested for an invalid key")

    monkeypatch.setattr(s3, "_client", _no_client)
    bad = "a//b.png"
    for coro in (
        s3.put_bytes("producer-assets", bad, b"x"),
        s3.upload_file("producer-assets", bad, __file__),
        s3.get_bytes("producer-assets", bad),
        s3.head("producer-assets", bad),
        s3.delete("producer-assets", bad),
    ):
        with pytest.raises(a.AssetStorageError):
            _run(coro)
    with pytest.raises(a.AssetStorageError):
        s3.presign_get("producer-assets", bad, expires=60)
    with pytest.raises(a.AssetStorageError):
        s3.presign_get("Bad_Bucket", "a.png", expires=60)


def test_ascii_slug():
    assert a.ascii_slug("Café Crème  Brûlée!!") == "cafe_creme_brulee"
    assert a.ascii_slug("صورة final") == "final"
    assert a.ascii_slug("صورة", fallback="yas_12") == "yas_12"
    assert a.ascii_slug("", fallback="عربي") == "asset"
    assert a.ascii_slug(None) == "asset"
    assert a.ascii_slug("a" * 50) == "a" * 32
    assert a.ascii_slug("ab cd ef", max_len=3) == "ab"
    assert a.ascii_slug("x+y/z") == "x_y_z"


def test_safe_ext():
    assert a.safe_ext("photo.JPG", None) == ".jpg"
    assert a.safe_ext("photo.JPG copy", "image/png") == ".png"
    assert a.safe_ext("a.b+c", "image/webp") == ".webp"
    assert a.safe_ext("font.woff2", None) == ".woff2"
    assert a.safe_ext("clip.mp4", "image/png") == ".mp4"
    assert a.safe_ext(None, "video/mp4") == ".mp4"
    assert a.safe_ext("noext", "image/jpeg; charset=binary") == ".jpg"
    assert a.safe_ext("toolong.abcdef", None) == ""
    assert a.safe_ext(None, None) == ""


# --- content type, active types (A8), download names (A11) --------------------

def test_content_type_and_active_types():
    assert s3.content_type_for("a/b.webp", None) == "image/webp"
    assert s3.content_type_for("a/b.mp4", "") == "video/mp4"
    assert s3.content_type_for("a/b.png", "image/jpeg") == "image/jpeg"
    assert s3.content_type_for("a/b.unknownext", None) == "application/octet-stream"
    for ct in ("text/html", "text/html; charset=utf-8", "image/svg+xml", "application/xhtml+xml",
               "text/xml", "application/xml", "text/javascript", "application/javascript",
               "application/x-javascript", "text/xsl", "application/json", "", "nonsense"):
        assert s3.object_headers(ct, None)["ContentDisposition"] == "attachment", ct
    for ct in ("application/pdf", "image/png", "image/jpeg", "image/webp", "video/mp4",
               "video/quicktime", "audio/mpeg", "font/woff2", "text/plain",
               "text/plain; charset=utf-8", "IMAGE/PNG"):
        headers = s3.object_headers(ct, "c")
        assert "ContentDisposition" not in headers, ct
        assert headers["ContentType"] == ct, "a well-formed type is stored as given"


@pytest.mark.parametrize("ct", [
    # Browsers read a comma as a list of types and may render another entry.
    "image/png, text/html", "text/html,", "text/plain, text/html", "image/png,image/svg+xml",
    # Not a type at all.
    "image/", "/png", "image png", "image/png/x",
])
def test_a_malformed_or_listed_type_is_stored_as_a_download(ct):
    headers = s3.object_headers(ct, None)
    assert headers["ContentType"] == "application/octet-stream", ct
    assert headers["ContentDisposition"] == "attachment", ct
    assert s3.mime_essence(ct) == "" and s3.is_active_type(ct)


def test_download_name_sanitisation():
    cd = s3.content_disposition('my "report";\\x.pdf')
    assert cd.startswith('attachment; filename="my reportx.pdf"; filename*=UTF-8\'\'')
    assert cd.endswith(quote('my "report";\\x.pdf', safe=""))
    arabic = "تقرير 2026.pdf"
    cd = s3.content_disposition(arabic)
    assert 'filename="2026.pdf"' in cd
    assert cd.endswith("filename*=UTF-8''" + quote(arabic, safe=""))
    assert s3.ascii_filename("تقرير.pdf") == "download.pdf"
    assert s3.ascii_filename("") == "download"
    assert s3.ascii_filename("a\nb\x00.pdf") == "ab.pdf"
    assert "%0A" not in s3.content_disposition("a\nb.pdf")


def test_clamp_expires():
    assert s3.clamp_expires(604800) == 604800
    assert s3.clamp_expires(10 ** 9) == 604800
    assert s3.clamp_expires(0) == 1
    assert s3.clamp_expires(-5) == 1


# --- upload -------------------------------------------------------------------

def test_upload_writes_to_s3_with_type_and_cache_control(s3_storage):
    res = _run(a.upload(data=b"png", path="manual/a.png", mime="image/png"))
    assert res == {
        "storage": "s3", "storage_bucket": config.producer_assets_bucket,
        "storage_path": "manual/a.png", "local_path": None, "bytes": 3,
        "mime_type": "image/png",
    }
    head = s3_storage.client.head_object(Bucket=config.producer_assets_bucket, Key="manual/a.png")
    assert head["ContentType"] == "image/png"
    assert head["CacheControl"] == a.CACHE_IMMUTABLE
    assert "ContentDisposition" not in head

    # producer-media is PRIVATE on S3, so it must not get the public rule (A20).
    _run(a.upload(data=b"x", path="poster/1.png", mime="image/png",
                  bucket=config.producer_media_bucket))
    head = s3_storage.client.head_object(Bucket=config.producer_media_bucket, Key="poster/1.png")
    assert head["CacheControl"] == a.CACHE_IMMUTABLE

    _run(a.upload(data=b"x", path="k/mut.png", mime="image/png", cache_control=a.CACHE_MUTABLE))
    head = s3_storage.client.head_object(Bucket=config.producer_assets_bucket, Key="k/mut.png")
    assert head["CacheControl"] == a.CACHE_MUTABLE

    # No mime: guessed from the key, never left to S3's binary/octet-stream.
    _run(a.upload(data=b"x", path="k/t.webp"))
    head = s3_storage.client.head_object(Bucket=config.producer_assets_bucket, Key="k/t.webp")
    assert head["ContentType"] == "image/webp"


def test_upload_marks_active_types_as_attachments(s3_storage):
    _run(a.upload(data=b"<svg/>", path="k/x.svg", mime="image/svg+xml"))
    _run(a.upload(data=b"<p>", path="k/x.html", mime="text/html"))
    _run(a.upload(data=b"%PDF", path="k/x.pdf", mime="application/pdf"))
    _run(a.upload(data=b"<x/>", path="k/x.xsl", mime="text/xsl"))
    _run(a.upload(data=b"<p>", path="k/list.png", mime="image/png, text/html"))
    get = s3_storage.client.head_object
    b = config.producer_assets_bucket
    assert get(Bucket=b, Key="k/x.svg")["ContentDisposition"] == "attachment"
    assert get(Bucket=b, Key="k/x.html")["ContentDisposition"] == "attachment"
    assert "ContentDisposition" not in get(Bucket=b, Key="k/x.pdf")
    assert get(Bucket=b, Key="k/x.xsl")["ContentDisposition"] == "attachment"
    listed = get(Bucket=b, Key="k/list.png")
    assert listed["ContentType"] == "application/octet-stream"
    assert listed["ContentDisposition"] == "attachment"


def test_upload_sanitises_an_invalid_key(s3_storage):
    res = _run(a.upload(data=b"x", path="/x//صورة a+b/../c.png", mime="image/png"))
    key = res["storage_path"]
    assert res["storage"] == "s3"
    assert s3.valid_key(key) and key.startswith("x/") and key.endswith("/c.png")
    s3_storage.client.head_object(Bucket=config.producer_assets_bucket, Key=key)


def test_upload_falls_back_to_local_when_s3_fails(monkeypatch, s3_config, tmp_assets_root):
    async def _fail(*args, **kwargs):
        raise a.AssetStorageError("down")

    monkeypatch.setattr(s3, "put_bytes", _fail)
    res = _run(a.upload(data=b"abc", path="a/b.png", mime="image/png"))
    assert res["storage"] == "local"
    assert res["storage_bucket"] is None and res["storage_path"] is None
    assert res["local_path"] == str(Path("data/assets_library/a/b.png"))
    assert (tmp_assets_root / "a" / "b.png").read_bytes() == b"abc"


def test_prefer_semantics(monkeypatch, s3_storage, tmp_assets_root):
    async def _no_supabase(**kwargs):
        raise AssertionError("the Supabase transport must never run in S3 mode")

    monkeypatch.setattr(a, "_upload_supabase", _no_supabase)
    # 'supabase' is a legacy alias for "the object store", which here is S3.
    assert _run(a.upload(data=b"x", path="p/s.png", prefer="supabase"))["storage"] == "s3"
    assert _run(a.upload(data=b"x", path="p/a.png", prefer="auto"))["storage"] == "s3"
    assert _run(a.upload(data=b"x", path="p/l.png", prefer="local"))["storage"] == "local"


def test_prefer_supabase_in_local_mode_goes_straight_to_disk(monkeypatch, tmp_assets_root):
    async def _no_supabase(**kwargs):
        raise AssertionError("no remote store is configured")

    monkeypatch.setattr(a, "_upload_supabase", _no_supabase)
    res = _run(a.upload(data=b"x", path="p/s.png", prefer="supabase"))
    assert res["storage"] == "local"


def test_upload_public(s3_storage):
    bucket = config.approved_media_bucket
    url = _run(a.upload_public(bucket=bucket, path="poster/1_ig_ab.jpg", data=b"jpg",
                               mime="image/jpeg"))
    assert url == f"{PUBLIC}/{bucket}/poster/1_ig_ab.jpg"
    head = s3_storage.client.head_object(Bucket=bucket, Key="poster/1_ig_ab.jpg")
    assert head["ContentType"] == "image/jpeg"
    assert head["CacheControl"] == "public, max-age=31536000, immutable"


def test_upload_public_refuses_before_writing_without_a_public_base(s3_storage):
    _set(s3_public_base_url="")
    with pytest.raises(a.AssetStorageError):
        _run(a.upload_public(bucket=config.approved_media_bucket, path="p/x.jpg",
                             data=b"x", mime="image/jpeg"))
    listed = s3_storage.client.list_objects_v2(Bucket=config.approved_media_bucket)
    assert listed.get("KeyCount", 0) == 0


# --- url_for (D9, D10, D11, A1) ----------------------------------------------

def _put(s3_storage, key: str, body: bytes = b"data", bucket: str | None = None):
    s3_storage.client.put_object(Bucket=bucket or config.producer_assets_bucket, Key=key,
                                 Body=body, ContentType="image/png")


def _count_heads(monkeypatch) -> dict:
    calls = {"n": 0}
    real = s3.head

    async def _head(bucket, key):
        calls["n"] += 1
        return await real(bucket, key)

    monkeypatch.setattr(s3, "head", _head)
    return calls


def test_url_for_missing_object_is_none(s3_storage):
    assert _run(a.url_for(storage_bucket=config.producer_assets_bucket,
                          storage_path="nope/missing.png", local_path=None)) is None
    assert _run(a.url_for(storage_bucket=None, storage_path=None, local_path="x.png")) is None


def test_url_for_public_and_internal_share_one_cached_signature(monkeypatch, s3_storage):
    _put(s3_storage, "a/b.png")
    heads = _count_heads(monkeypatch)
    kw = dict(storage_bucket=config.producer_assets_bucket, storage_path="a/b.png", local_path=None)
    internal = _run(a.url_for(**kw, internal=True))
    public = _run(a.url_for(**kw))
    assert internal.startswith(f"{ENDPOINT}/{config.producer_assets_bucket}/a/b.png?")
    assert public.startswith(f"{PUBLIC}/{config.producer_assets_bucket}/a/b.png?")
    # A1: same signature, only the origin differs.
    assert internal[len(ENDPOINT):] == public[len(PUBLIC):]
    assert _run(a.url_for(**kw)) == public
    assert heads["n"] == 1, "HEAD only on a cache miss"
    assert "X-Amz-Signature=" in public


def test_url_for_download_name_is_signed_not_appended(s3_storage):
    _put(s3_storage, "a/v.mp4")
    kw = dict(storage_bucket=config.producer_assets_bucket, storage_path="a/v.mp4", local_path=None)
    plain = _run(a.url_for(**kw))
    dl = _run(a.url_for(**kw, download_name='talking "head" 1.mp4'))
    assert dl != plain
    q = parse_qs(urlsplit(dl).query)
    assert q["response-content-disposition"] == [s3.content_disposition('talking "head" 1.mp4')]
    assert "download=" not in dl
    # The download variant is its own cache entry; the plain URL is unchanged.
    assert _run(a.url_for(**kw)) == plain


def test_url_for_clamps_expires(s3_storage):
    _put(s3_storage, "a/e.png")
    kw = dict(storage_bucket=config.producer_assets_bucket, storage_path="a/e.png", local_path=None)
    long = _run(a.url_for(**kw, expires=10 ** 7))
    assert parse_qs(urlsplit(long).query)["X-Amz-Expires"] == ["604800"]
    # The cache is keyed on the CLAMPED value, so both spellings share an entry.
    assert _run(a.url_for(**kw, expires=604800)) == long
    short = _run(a.url_for(**kw, expires=0))
    assert parse_qs(urlsplit(short).query)["X-Amz-Expires"] == ["1"]


def test_url_for_is_none_on_any_head_error(monkeypatch, s3_config):
    async def _head(bucket, key):
        raise a.AssetStorageError("403 AccessDenied")

    monkeypatch.setattr(s3, "head", _head)
    assert _run(a.url_for(storage_bucket="producer-assets", storage_path="a.png",
                          local_path=None)) is None


def test_url_for_is_none_for_an_invalid_key(s3_config):
    assert _run(a.url_for(storage_bucket="producer-assets", storage_path="a//b.png",
                          local_path=None)) is None


# --- cache eviction on every write (A2) ---------------------------------------

def _cached_paths(bucket: str) -> set[str]:
    return {k[1] for k in a._SIGNED_URL_CACHE if k[0] == bucket}


def test_overwrite_mints_a_new_url(monkeypatch, s3_config):
    """url_for -> overwrite the same key -> url_for returns a different URL."""
    counter = {"n": 0}

    def _presign(bucket, key, *, expires, download_name=None):
        counter["n"] += 1
        return f"{ENDPOINT}/{bucket}/{key}?sig={counter['n']}"

    async def _head(bucket, key):
        return True

    async def _put(*args, **kwargs):
        return None

    monkeypatch.setattr(s3, "presign_get", _presign)
    monkeypatch.setattr(s3, "head", _head)
    monkeypatch.setattr(s3, "put_bytes", _put)
    kw = dict(storage_bucket="producer-assets", storage_path="yas/th/1/main.png", local_path=None)
    first = _run(a.url_for(**kw))
    assert _run(a.url_for(**kw)) == first
    _run(a.upload(data=b"new", path="yas/th/1/main.png", mime="image/png"))
    assert _run(a.url_for(**kw)) != first


def test_every_s3_write_evicts_the_signed_url(s3_storage, tmp_path):
    b = config.producer_assets_bucket

    def _warm(key):
        _put(s3_storage, key)
        assert _run(a.url_for(storage_bucket=b, storage_path=key, local_path=None))
        assert key in _cached_paths(b)

    _warm("w/upload.png")
    _run(a.upload(data=b"x", path="w/upload.png", mime="image/png"))
    assert "w/upload.png" not in _cached_paths(b)

    _warm("w/persist.mp4")
    f = tmp_path / "render.mp4"
    f.write_bytes(b"mp4")
    assert _run(a.persist_local_file(abs_path=f, bucket_key="w/persist.mp4")) == (b, "w/persist.mp4")
    assert "w/persist.mp4" not in _cached_paths(b)

    _warm("w/put.png")
    _run(a.put_s3_object(b, "w/put.png", data=b"x", mime="image/png"))
    assert "w/put.png" not in _cached_paths(b)

    ab = config.approved_media_bucket
    _put(s3_storage, "w/pub.jpg", bucket=ab)
    assert _run(a.url_for(storage_bucket=ab, storage_path="w/pub.jpg", local_path=None))
    _run(a.upload_public(bucket=ab, path="w/pub.jpg", data=b"x", mime="image/jpeg"))
    assert "w/pub.jpg" not in _cached_paths(ab)

    _warm("w/gone.png")
    _run(a.delete(storage_bucket=b, storage_path="w/gone.png", local_path=None))
    assert "w/gone.png" not in _cached_paths(b)


# --- HEAD semantics (A3), object_exists, download, delete ---------------------

@pytest.mark.parametrize("code,status", [("404", 404), ("NoSuchKey", 404), ("NotFound", 404), ("", 404)])
def test_head_treats_404_as_missing(monkeypatch, s3_config, code, status):
    fake = _FakeClient(_client_error(code, status))
    monkeypatch.setattr(s3, "_client", lambda kind: fake)
    assert _run(s3.head("producer-assets", "a.png")) is False


def test_head_raises_on_other_errors(monkeypatch, s3_config):
    fake = _FakeClient(_client_error("AccessDenied", 403))
    monkeypatch.setattr(s3, "_client", lambda kind: fake)
    with pytest.raises(a.AssetStorageError):
        _run(s3.head("producer-assets", "a.png"))
    with pytest.raises(a.AssetStorageError):
        _run(a.object_exists(bucket="producer-assets", path="a.png"))


def test_object_exists(s3_storage):
    _put(s3_storage, "o/here.png")
    b = config.producer_assets_bucket
    assert _run(a.object_exists(bucket=b, path="o/here.png")) is True
    assert _run(a.object_exists(bucket=b, path="o/not-here.png")) is False


def test_object_exists_without_an_object_store_raises():
    with pytest.raises(a.AssetStorageError):
        _run(a.object_exists(bucket="producer-assets", path="a.png"))


def test_download_bytes(s3_storage):
    _put(s3_storage, "d/x.bin", body=b"\x00\x01payload")
    b = config.producer_assets_bucket
    assert _run(a.download_bytes(storage_bucket=b, storage_path="d/x.bin")) == b"\x00\x01payload"
    with pytest.raises(a.AssetNotFoundError):
        _run(a.download_bytes(storage_bucket=b, storage_path="d/missing.bin"))
    with pytest.raises(a.AssetNotFoundError):
        _run(a.download_bytes(storage_bucket="no-such-bucket", storage_path="d/x.bin"))


@pytest.mark.parametrize("exc", [
    EndpointConnectionError(endpoint_url=ENDPOINT),
    _client_error("AccessDenied", 403, "GetObject"),
    _client_error("InternalError", 500, "GetObject"),
])
def test_download_bytes_failure_is_not_a_missing_object(monkeypatch, s3_config, exc):
    """Only a real 404 may read as "gone": callers fail a campaign item for
    good on that, and a store that is down must be retried instead."""
    monkeypatch.setattr(s3, "_client", lambda kind: _FakeClient(exc))
    with pytest.raises(a.AssetStorageError) as info:
        _run(a.download_bytes(storage_bucket="producer-media", storage_path="poster/1.png"))
    assert not isinstance(info.value, a.AssetNotFoundError)


def test_download_bytes_with_the_breaker_open_is_not_a_missing_object(monkeypatch, s3_config):
    s3._trip_breaker(RuntimeError("down"))
    with pytest.raises(a.AssetStorageError) as info:
        _run(a.download_bytes(storage_bucket="producer-media", storage_path="poster/1.png"))
    assert not isinstance(info.value, a.AssetNotFoundError)


@pytest.mark.parametrize("status,missing", [(404, True), (400, True), (500, False), (403, False)])
def test_supabase_download_bytes_tells_missing_from_failed(monkeypatch, status, missing):
    import httpx

    _set(storage_backend="supabase", supabase_url="https://proj.supabase.co",
         supabase_service_role_key="k")

    class _Resp:
        status_code = status
        text = "nope"
        content = b""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(a.AssetStorageError) as info:
        _run(a.download_bytes(storage_bucket="producer-media", storage_path="poster/1.png"))
    assert isinstance(info.value, a.AssetNotFoundError) is missing


def test_delete_removes_the_object_and_never_raises(monkeypatch, s3_storage):
    _put(s3_storage, "del/x.png")
    b = config.producer_assets_bucket
    _run(a.delete(storage_bucket=b, storage_path="del/x.png", local_path=None))
    assert _run(a.object_exists(bucket=b, path="del/x.png")) is False

    async def _fail(bucket, key):
        raise a.AssetStorageError("down")

    monkeypatch.setattr(s3, "delete", _fail)
    _run(a.delete(storage_bucket=b, storage_path="del/y.png", local_path=None))


# --- buckets, policy, bootstrap (D5, D7) --------------------------------------

def _policy(client, bucket):
    return json.loads(client.get_bucket_policy(Bucket=bucket)["Policy"])


def test_bootstrap_creates_buckets_and_only_the_approved_one_is_public(s3_config):
    moto = pytest.importorskip("moto")
    import boto3

    _set(producer_media_public=True)  # must be ignored on S3 (D5)
    try:
        with moto.mock_aws():
            _run(a.bootstrap_storage())
            _run(a.bootstrap_storage())  # idempotent
            a.clear_caches()
            _run(a.bootstrap_storage())  # idempotent against the store itself
            client = boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1",
                                  aws_access_key_id="x", aws_secret_access_key="y")
            names = {b["Name"] for b in client.list_buckets()["Buckets"]}
            assert names == {config.producer_assets_bucket, config.producer_media_bucket,
                             config.brand_docs_bucket, config.approved_media_bucket}
            pol = _policy(client, config.approved_media_bucket)
            (stmt,) = pol["Statement"]
            assert stmt["Effect"] == "Allow"
            assert stmt["Action"] == ["s3:GetObject"]
            assert stmt["Resource"] == [f"arn:aws:s3:::{config.approved_media_bucket}/*"]
            for private in (config.producer_assets_bucket, config.producer_media_bucket,
                            config.brand_docs_bucket):
                with pytest.raises(ClientError):
                    client.get_bucket_policy(Bucket=private)
    finally:
        _set(producer_media_public=False)
    assert a.bucket_is_public(config.approved_media_bucket) is True
    assert a.bucket_is_public(config.producer_media_bucket) is False
    assert a.bucket_is_public(config.producer_assets_bucket) is False


def test_bootstrap_never_raises_and_still_fills_the_publicness_map(monkeypatch, s3_config):
    async def _fail(bucket):
        raise a.AssetStorageError("endpoint unreachable")

    monkeypatch.setattr(s3, "create_bucket", _fail)
    _run(a.bootstrap_storage())
    assert a.bucket_is_public(config.approved_media_bucket) is True


def test_bootstrap_in_local_mode_touches_no_store(monkeypatch):
    async def _no_store(**kwargs):
        raise AssertionError("local mode must not create buckets")

    monkeypatch.setattr(a, "_create_bucket_supabase", _no_store)
    _run(a.bootstrap_storage())


def test_ensure_public_bucket_is_idempotent(s3_storage):
    b = config.approved_media_bucket
    _run(a.ensure_public_bucket(b))
    a.clear_caches()
    _run(a.ensure_public_bucket(b))
    assert _policy(s3_storage.client, b)["Statement"][0]["Action"] == ["s3:GetObject"]


def test_create_bucket_tolerates_access_denied_when_the_bucket_exists(monkeypatch, s3_config):
    class _Client:
        def create_bucket(self, **kw):
            raise _client_error("AccessDenied", 403, "CreateBucket")

        def head_bucket(self, **kw):
            return {}

    monkeypatch.setattr(s3, "_client", lambda kind: _Client())
    _run(s3.create_bucket("producer-assets"))


def test_grants_public_get():
    ok = s3.public_read_policy("approved-instagram")
    assert s3.grants_public_get(ok, "approved-instagram")
    assert not s3.grants_public_get(ok, "producer-assets")
    string_form = json.dumps({"Statement": [{"Effect": "Allow", "Principal": "*",
                                             "Action": "s3:GetObject",
                                             "Resource": "arn:aws:s3:::approved-instagram/*"}]})
    assert s3.grants_public_get(string_form, "approved-instagram")
    deny = json.loads(ok)
    deny["Statement"][0]["Effect"] = "Deny"
    assert not s3.grants_public_get(json.dumps(deny), "approved-instagram")
    assert not s3.grants_public_get("not json", "approved-instagram")


@pytest.mark.parametrize("existing,ok", [
    (s3.public_read_policy("approved-instagram"), True),
    (json.dumps({"Statement": []}), False),
])
def test_set_public_read_accepts_an_existing_policy_when_denied(monkeypatch, s3_config, existing, ok):
    """A least-privilege app user may not set policies; minio-init already did."""
    class _Client:
        def put_bucket_policy(self, **kw):
            raise _client_error("AccessDenied", 403, "PutBucketPolicy")

        def get_bucket_policy(self, **kw):
            return {"Policy": existing}

    monkeypatch.setattr(s3, "_client", lambda kind: _Client())
    if ok:
        _run(s3.set_public_read("approved-instagram"))
    else:
        with pytest.raises(a.AssetStorageError):
            _run(s3.set_public_read("approved-instagram"))


# --- URL shapes in S3 mode (D13, A21) -----------------------------------------

def test_s3_mode_has_no_image_transforms(s3_config):
    _set(supabase_image_transform="on", supabase_url="https://proj.supabase.co")
    assert a.image_transform_enabled() is False
    assert a._thumb_tier() == a._PLAIN_TIER
    signed = f"{ENDPOINT}/producer-assets/a.png?X-Amz-Signature=x"
    assert a._to_render_path(signed) == signed
    assert a.public_render_url("approved-instagram", "p/1.jpg", width=480) == \
        a.public_url("approved-instagram", "p/1.jpg")
    _run(a.ensure_image_transform_capability(("producer-assets", "a.png"), force=True))
    assert a._IMAGE_TRANSFORM_OK is None, "no probe on S3"


def test_public_url_shape(s3_config):
    assert a.public_url("approved-instagram", "ig/a b+c.jpg") == \
        f"{PUBLIC}/approved-instagram/ig/a%20b%2Bc.jpg"
    assert a.public_url("approved-instagram", "/ig/x.jpg") == f"{PUBLIC}/approved-instagram/ig/x.jpg"
    _set(s3_public_base_url="")
    with pytest.raises(a.AssetStorageError):
        a.public_url("approved-instagram", "ig/x.jpg")


def test_ensure_bucket_publicness_comes_from_config(s3_config):
    _run(a.ensure_bucket_publicness(force=True))
    assert a._BUCKET_PUBLIC == {
        config.producer_assets_bucket: False, config.producer_media_bucket: False,
        config.brand_docs_bucket: False, config.approved_media_bucket: True,
    }


# --- parse_public_url, delete_public_urls, internal_url (A6, D4) --------------

def test_parse_public_url_round_trips(s3_config):
    for key in ("poster/1_ig_ab.jpg", "ig/a b+c.jpg", "ig/صورة.jpg"):
        url = a.public_url("approved-instagram", key)
        assert a.parse_public_url(url) == ("approved-instagram", key)
        assert a.parse_public_url(url + "?v=1") == ("approved-instagram", key)
        assert a.parse_public_url(url + "#frag") == ("approved-instagram", key)
    assert a.parse_public_url(f"{ENDPOINT}/producer-assets/a/b.png?X-Amz-Date=1") == \
        ("producer-assets", "a/b.png")
    supa = "https://proj.supabase.co/storage/v1/object/public/approved-instagram/ig/a%20b.jpg?v=2"
    assert a.parse_public_url(supa) == ("approved-instagram", "ig/a b.jpg")
    for junk in (None, "", "https://elsewhere.test/x/y.jpg", f"{PUBLIC}/only-bucket",
                 f"{PUBLIC}/", "/api/yas-producer/poster/1/file"):
        assert a.parse_public_url(junk) is None, junk


def test_parse_public_url_supabase_shape_outside_s3_mode():
    supa = "https://proj.supabase.co/storage/v1/object/public/approved-instagram/poster/1.jpg"
    assert a.parse_public_url(supa) == ("approved-instagram", "poster/1.jpg")


def test_delete_public_urls(s3_storage):
    ab = config.approved_media_bucket
    urls = [
        _run(a.upload_public(bucket=ab, path=key, data=b"x", mime="image/jpeg"))
        for key in ("c/one.jpg", "c/two_slide.jpg", "c/three-3.mp4")
    ]
    urls[2] += "?v=1"
    _run(a.delete_public_urls(urls + [None, "", "https://elsewhere.test/x.jpg", urls[0]]))
    assert s3_storage.client.list_objects_v2(Bucket=ab).get("KeyCount", 0) == 0


def test_delete_public_urls_never_raises(monkeypatch, s3_config):
    async def _boom(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(a, "delete", _boom)
    _run(a.delete_public_urls([f"{PUBLIC}/approved-instagram/x.jpg"]))


def test_internal_url(s3_config):
    url = f"{PUBLIC}/producer-assets/a/b.png?X-Amz-Signature=abc"
    assert a.internal_url(url) == f"{ENDPOINT}/producer-assets/a/b.png?X-Amz-Signature=abc"
    assert a.internal_url("/api/x/file") == "/api/x/file"
    _set(storage_backend="local")
    assert a.internal_url(url) == url


# --- signed_render_urls (batch, no HEAD) --------------------------------------

def test_signed_render_urls_presigns_without_head(monkeypatch, s3_storage):
    async def _no_head(bucket, key):
        raise AssertionError("the batch path must not HEAD each tile")

    monkeypatch.setattr(s3, "head", _no_head)
    b = config.producer_assets_bucket
    _put(s3_storage, "g/1.png")
    out = _run(a.signed_render_urls(b, ["g/1.png", "g/missing.png", "bad//key"], expires=86400))
    assert set(out) == {"g/1.png", "g/missing.png"}
    for p, u in out.items():
        assert u.startswith(f"{PUBLIC}/{b}/{p}?"), u
    # Cached on the internal origin, returned on the public one.
    assert all(v[0].startswith(ENDPOINT) for v in a._SIGNED_URL_CACHE.values())

    calls = {"n": 0}
    real = s3.presign_get

    def _count(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(s3, "presign_get", _count)
    assert _run(a.signed_render_urls(b, ["g/1.png"], expires=86400)) == {"g/1.png": out["g/1.png"]}
    assert calls["n"] == 0


def test_batch_urls_never_let_url_for_skip_its_head(s3_storage):
    b = config.producer_assets_bucket
    _run(a.signed_render_urls(b, ["g/missing.png"], expires=3600))
    assert _run(a.url_for(storage_bucket=b, storage_path="g/missing.png", local_path=None,
                          expires=3600)) is None


# --- persist_local_file and put_s3_object -------------------------------------

def test_persist_local_file_streams_to_s3(monkeypatch, s3_storage, tmp_path):
    used = {"upload_file": 0}
    real = s3.upload_file

    async def _spy(*args, **kwargs):
        used["upload_file"] += 1
        return await real(*args, **kwargs)

    async def _no_put(*args, **kwargs):
        raise AssertionError("persist_local_file must stream, not read into RAM")

    monkeypatch.setattr(s3, "upload_file", _spy)
    monkeypatch.setattr(s3, "put_bytes", _no_put)
    f = tmp_path / "final.mp4"
    f.write_bytes(b"\x00" * 1000)
    got = _run(a.persist_local_file(abs_path=f, bucket_key="yas/compositions/video-editor/7.mp4"))
    assert got == (config.producer_assets_bucket, "yas/compositions/video-editor/7.mp4")
    assert used["upload_file"] == 1
    head = s3_storage.client.head_object(Bucket=got[0], Key=got[1])
    assert head["ContentType"] == "video/mp4" and head["ContentLength"] == 1000
    # A missing file degrades to (None, None).
    assert _run(a.persist_local_file(abs_path=tmp_path / "nope.mp4", bucket_key="x/y.mp4")) == \
        (None, None)


def test_persist_local_file_in_local_mode_writes_no_copy(tmp_path, tmp_assets_root):
    f = tmp_path / "final.mp4"
    f.write_bytes(b"mp4")
    assert _run(a.persist_local_file(abs_path=f, bucket_key="yas/c/1.mp4")) == (None, None)
    assert _files_under(tmp_assets_root) == []


def test_put_s3_object(s3_storage, tmp_path):
    b = config.producer_assets_bucket
    assert _run(a.put_s3_object(b, "p/data.png", data=b"png", mime="image/png")) == "p/data.png"
    f = tmp_path / "frame.png"
    f.write_bytes(b"frame")
    assert _run(a.put_s3_object(b, "p/file.png", path=f, mime=None)) == "p/file.png"
    head = s3_storage.client.head_object(Bucket=b, Key="p/file.png")
    assert head["ContentType"] == "image/png" and head["CacheControl"] == a.CACHE_IMMUTABLE
    for bad in (dict(), dict(data=b"x", path=f)):
        with pytest.raises(a.AssetStorageError):
            _run(a.put_s3_object(b, "p/x.png", mime="image/png", **bad))
    with pytest.raises(a.AssetStorageError):
        _run(a.put_s3_object(b, "p//bad.png", data=b"x", mime="image/png"))


def test_put_s3_object_has_no_local_fallback(monkeypatch, s3_config, tmp_assets_root):
    async def _fail(*args, **kwargs):
        raise a.AssetStorageError("down")

    monkeypatch.setattr(s3, "put_bytes", _fail)
    with pytest.raises(a.AssetStorageError):
        _run(a.put_s3_object("producer-assets", "p/x.png", data=b"x", mime="image/png"))
    assert _files_under(tmp_assets_root) == []


def test_put_s3_object_outside_s3_mode_raises():
    with pytest.raises(a.AssetStorageError):
        _run(a.put_s3_object("producer-assets", "p/x.png", data=b"x", mime="image/png"))


# --- two clients and the circuit breaker (A18) --------------------------------

def test_fast_and_bulk_client_settings(s3_config):
    fast = s3._client("fast").meta.config
    bulk = s3._client("bulk").meta.config
    assert (fast.connect_timeout, fast.read_timeout) == (2, 10)
    assert fast.retries["total_max_attempts"] == 1, "request paths never retry"
    assert (bulk.connect_timeout, bulk.read_timeout) == (5, 60)
    assert bulk.retries["total_max_attempts"] > 1
    assert fast.s3["addressing_style"] == "path" and fast.signature_version == "s3v4"
    assert s3._client("fast") is s3._client("fast"), "clients are cached"


def test_operations_pick_the_right_client(monkeypatch, s3_config, tmp_path):
    kinds = []

    class _Ok:
        def __getattr__(self, name):
            def _call(*args, **kwargs):
                if name == "get_object":
                    import io
                    from botocore.response import StreamingBody
                    return {"Body": StreamingBody(io.BytesIO(b"x"), 1)}
                return {}
            return _call

    def _client(kind):
        kinds.append(kind)
        return _Ok()

    monkeypatch.setattr(s3, "_client", _client)
    monkeypatch.setattr(s3, "SMALL_PUT_BYTES", 10)
    f = tmp_path / "f.bin"
    f.write_bytes(b"x")
    _run(s3.put_bytes("producer-assets", "a.bin", b"small"))
    _run(s3.put_bytes("producer-assets", "b.bin", b"x" * 10))
    _run(s3.upload_file("producer-assets", "c.bin", f))
    _run(s3.get_bytes("producer-assets", "d.bin"))
    _run(s3.head("producer-assets", "e.bin"))
    _run(s3.delete("producer-assets", "f.bin"))
    _run(s3.create_bucket("producer-assets"))
    assert kinds == ["fast", "bulk", "bulk", "bulk", "fast", "fast", "fast"]


@pytest.mark.parametrize("exc", [
    EndpointConnectionError(endpoint_url=ENDPOINT),
    ReadTimeoutError(endpoint_url=ENDPOINT),
])
def test_breaker_fails_fast_after_a_connection_error(monkeypatch, s3_config, tmp_assets_root, exc):
    fake = _FakeClient(exc)
    monkeypatch.setattr(s3, "_client", lambda kind: fake)
    with pytest.raises(a.AssetStorageError):
        _run(s3.head("producer-assets", "a.png"))
    assert fake.calls == 1 and s3.breaker_open()

    # While open: no network call at all, and the callers degrade.
    with pytest.raises(a.AssetStorageError):
        _run(s3.head("producer-assets", "a.png"))
    assert _run(a.url_for(storage_bucket="producer-assets", storage_path="a.png",
                          local_path=None)) is None
    assert _run(a.upload(data=b"x", path="a/b.png", mime="image/png"))["storage"] == "local"
    assert fake.calls == 1

    # Presigning is offline and not blocked.
    monkeypatch.setattr(s3, "_client", lambda kind: _Presigner())
    assert s3.presign_get("producer-assets", "a.png", expires=60) == "signed"

    # After the window the endpoint is tried again.
    monkeypatch.setattr(s3, "_client", lambda kind: fake)
    now = s3.time.monotonic()
    monkeypatch.setattr(s3.time, "monotonic", lambda: now + s3.BREAKER_SECONDS + 1)
    with pytest.raises(a.AssetStorageError):
        _run(s3.head("producer-assets", "a.png"))
    assert fake.calls == 2


class _Presigner:
    def generate_presigned_url(self, *args, **kwargs):
        return "signed"


def test_a_server_error_does_not_open_the_breaker(monkeypatch, s3_config):
    fake = _FakeClient(_client_error("InternalError", 500, "PutObject"))
    monkeypatch.setattr(s3, "_client", lambda kind: fake)
    with pytest.raises(a.AssetStorageError):
        _run(s3.put_bytes("producer-assets", "a.png", b"x"))
    assert not s3.breaker_open()


# --- public_hosting_configured (D16) ------------------------------------------

def test_public_hosting_configured():
    ok, reason = a.public_hosting_configured()
    assert ok is False and reason
    _set(storage_backend="s3", s3_endpoint=ENDPOINT)
    assert a.public_hosting_configured() == (False, "the media storage credentials are not set")
    _set(s3_access_key="ak", s3_secret_key="sk")
    ok, reason = a.public_hosting_configured()
    assert ok is False and "S3_PUBLIC_BASE_URL" in reason
    _set(s3_public_base_url=PUBLIC)
    assert a.public_hosting_configured() == (True, "")
    _set(storage_backend="s3", s3_endpoint="")
    assert "S3_ENDPOINT" in a.public_hosting_configured()[1]
    _set(storage_backend="supabase", supabase_url="https://proj.supabase.co")
    ok, reason = a.public_hosting_configured()
    assert ok is False and "SUPABASE_SERVICE_ROLE_KEY" in reason
    _set(supabase_service_role_key="k")
    assert a.public_hosting_configured() == (True, "")


def test_hosting_reasons_never_name_a_secret():
    reasons = []
    for values in (
        {"storage_backend": "local"},
        {"storage_backend": "s3", "s3_endpoint": ""},
        {"storage_backend": "s3", "s3_endpoint": ENDPOINT},
        {"storage_backend": "s3", "s3_access_key": "a", "s3_secret_key": "b"},
    ):
        _set(**values)
        reasons.append(a.public_hosting_configured()[1].lower())
    for r in reasons:
        for word in ("secret", "password", "token"):
            assert word not in r, r


# --- Supabase mode keeps its own download parameter ---------------------------

def test_supabase_url_for_bakes_the_download_param(monkeypatch):
    _set(storage_backend="supabase", supabase_url="https://proj.supabase.co",
         supabase_service_role_key="k")
    calls = {"n": 0}

    async def _sign(*, bucket, path, expires, transform=None):
        calls["n"] += 1
        return f"https://proj.supabase.co/storage/v1/object/sign/{bucket}/{path}?token=t"

    monkeypatch.setattr(a, "_signed_url_supabase", _sign)
    kw = dict(storage_bucket="producer-assets", storage_path="a.mp4", local_path=None)
    plain = _run(a.url_for(**kw, internal=True))
    assert plain.endswith("?token=t")
    dl = _run(a.url_for(**kw, download_name="my clip.mp4"))
    assert dl == plain + "&download=my%20clip.mp4"
    assert calls["n"] == 1, "one signature serves both"
