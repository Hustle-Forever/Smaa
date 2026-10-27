"""yas_producer on the S3/MinIO storage backend.

Pins the router-side rules the S3 backend depends on:

  * a signed URL is never edited after signing (SigV4 signs every query
    parameter): downloads pass `download_name=` to url_for instead of appending
    `download=`, and Library tiles get no `?v=` buster on signed URLs, while
    public URLs may keep it;
  * `_media_bytes` falls through to the local copy when the bucket read fails;
  * server-side fetches of a url_for result use the internal origin, browser
    redirects the public one;
  * every producer_assets image writer stores a thumbnail derivative and records
    it in `attributes`, with an ASCII-only key;
  * cancelling a scheduled post deletes its media AND every carousel slide.

Offline: moto (via the conftest `s3_storage` fixture) or monkeypatched
transport, a fake DB connection, and a fake httpx client.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.responses import RedirectResponse

import app.routers.yas_producer as r
from app.config import config
from app.services import asset_storage

from conftest import S3_TEST_ENDPOINT, S3_TEST_PUBLIC_BASE

_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")


def _run(coro):
    return asyncio.run(coro)


def _png(size=(800, 600)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (30, 120, 200)).save(out, format="PNG")
    return out.getvalue()


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# --- fakes ------------------------------------------------------------------

class _FakeDB:
    """Stands in for app.db.get_conn: records every execute and answers
    fetchone/fetchall from a queue, in order."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls: list[tuple[str, tuple | None]] = []

    def get_conn(self, **_kw):
        db = self

        class _Cursor:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=None):
                db.calls.append((" ".join(sql.split()), params))

            async def fetchone(self):
                return db.rows.pop(0) if db.rows else None

            async def fetchall(self):
                return db.rows.pop(0) if db.rows else []

        class _Conn:
            def cursor(self):
                return _Cursor()

        @contextlib.asynccontextmanager
        async def _cm():
            yield _Conn()

        return _cm()

    def insert(self) -> tuple[str, tuple]:
        return next(c for c in self.calls if c[0].startswith("INSERT INTO producer_assets"))


def _fake_httpx(monkeypatch, *, content=b"", status_code=200) -> list[str]:
    """Replace httpx.AsyncClient; returns the list of URLs it was asked to GET."""
    import httpx

    seen: list[str] = []

    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.content = content

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, *a, **kw):
            seen.append(str(url))
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


def _no_ai(monkeypatch):
    """The save routes describe + embed the image; neither may touch Vertex."""
    async def _describe(*a, **kw):
        return "a blue test image"

    async def _embed(texts):
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(r, "gemini_multimodal", _describe)
    monkeypatch.setattr(r, "gemini_embed", _embed)


# --- D10: downloads are signed in, never appended ---------------------------

def _assert_signed_download(url: str, bucket: str, key: str, filename: str) -> None:
    assert url.startswith(f"{S3_TEST_PUBLIC_BASE}/{bucket}/{key}?"), url
    q = _query(url)
    assert "X-Amz-Signature" in q
    assert f'filename="{filename}"' in q["response-content-disposition"][0]
    assert q["response-content-disposition"][0].startswith("attachment")
    # Nothing was added after signing.
    assert "download" not in q and "v" not in q


def test_media_response_signs_the_download_name(s3_storage):
    b = config.producer_media_bucket
    s3_storage.client.put_object(Bucket=b, Key="poster/3.png", Body=b"x", ContentType="image/png")
    resp = _run(r._media_response(
        storage_bucket=b, storage_path="poster/3.png", local_path=None,
        mime="image/png", download_name="poster-3.png",
    ))
    assert isinstance(resp, RedirectResponse) and resp.status_code == 307
    loc = resp.headers["location"]
    _assert_signed_download(loc, b, "poster/3.png", "poster-3.png")
    # Exactly url_for's URL, byte for byte (served from the signed-URL cache).
    assert loc == _run(asset_storage.url_for(
        storage_bucket=b, storage_path="poster/3.png", local_path=None,
        expires=r._MEDIA_SIGN_TTL, download_name="poster-3.png",
    ))
    # A plain view carries no disposition.
    plain = _run(r._media_response(
        storage_bucket=b, storage_path="poster/3.png", local_path=None, mime="image/png",
    )).headers["location"]
    assert "response-content-disposition" not in _query(plain)


def test_media_response_falls_back_to_local_when_the_object_is_missing(s3_storage, tmp_path):
    f = tmp_path / "3.png"
    f.write_bytes(b"local bytes")
    resp = _run(r._media_response(
        storage_bucket=config.producer_media_bucket, storage_path="poster/missing.png",
        local_path=str(f), mime="image/png", download_name="poster-3.png",
    ))
    assert not isinstance(resp, RedirectResponse)
    assert resp.headers["content-disposition"] == 'attachment; filename="poster-3.png"'


def test_a_redirect_never_takes_the_callers_immutable_header(s3_storage):
    """The 307 points at a signature that dies within 7 days, so it must stay
    revalidatable even when the caller passes the long header for local bytes."""
    b = config.producer_media_bucket
    s3_storage.client.put_object(Bucket=b, Key="animation/7.mp4", Body=b"mp4",
                                 ContentType="video/mp4")
    resp = _run(r._media_response(
        storage_bucket=b, storage_path="animation/7.mp4", local_path=None,
        mime="video/mp4", headers=r._MEDIA_CACHE_HEADERS,
    ))
    assert isinstance(resp, RedirectResponse) and resp.status_code == 307
    assert resp.headers["cache-control"] == r._REDIRECT_CACHE_HEADERS["Cache-Control"]
    assert "immutable" not in resp.headers["cache-control"]


def test_local_bytes_keep_the_callers_headers(tmp_path):
    f = tmp_path / "7.mp4"
    f.write_bytes(b"mp4")
    resp = _run(r._media_response(
        storage_bucket=None, storage_path=None, local_path=str(f),
        mime="video/mp4", headers=r._MEDIA_CACHE_HEADERS,
    ))
    assert not isinstance(resp, RedirectResponse)
    assert resp.headers["cache-control"] == r._MEDIA_CACHE_HEADERS["Cache-Control"]


@pytest.mark.parametrize("route,store", [
    ("animation_get_file", "anim_store"), ("general_video_get_file", "gv_store"),
])
def test_video_file_redirects_are_not_cached_for_a_year(s3_storage, monkeypatch, route, store):
    b = config.producer_media_bucket
    key = "animation/7.mp4"
    s3_storage.client.put_object(Bucket=b, Key=key, Body=b"mp4", ContentType="video/mp4")

    async def _get(_id):
        return {"id": 7, "video_storage_bucket": b, "video_storage_path": key,
                "video_local_path": None}

    monkeypatch.setattr(getattr(r, store), "get", _get)
    for download in (False, True):
        resp = _run(getattr(r, route)(7, download=download))
        assert resp.status_code == 307
        assert resp.headers["cache-control"] == r._REDIRECT_CACHE_HEADERS["Cache-Control"]


def test_media_response_supabase_download_is_baked_in_once(monkeypatch):
    object.__setattr__(config, "supabase_url", "https://sb.test")
    object.__setattr__(config, "supabase_service_role_key", "service-key")

    async def _sign(*, bucket, path, expires, transform=None):
        return f"https://sb.test/storage/v1/object/sign/{bucket}/{path}?token=abc"

    monkeypatch.setattr(asset_storage, "_signed_url_supabase", _sign)
    resp = _run(r._media_response(
        storage_bucket="producer-media", storage_path="poster/3.png", local_path=None,
        mime="image/png", download_name="poster-3.png",
    ))
    loc = resp.headers["location"]
    assert loc == (
        "https://sb.test/storage/v1/object/sign/producer-media/poster/3.png"
        "?token=abc&download=poster-3.png"
    )


def test_poster_human_file_download_is_signed(s3_storage, monkeypatch):
    b = config.producer_assets_bucket
    s3_storage.client.put_object(Bucket=b, Key="yas/generations/5.png", Body=b"x")
    db = _FakeDB([{"local_path": None, "storage_bucket": b,
                   "storage_path": "yas/generations/5.png", "mime_type": "image/png"}])
    monkeypatch.setattr(r, "get_conn", db.get_conn)
    resp = _run(r.get_generation_file(5, download=True))
    assert resp.status_code == 307
    _assert_signed_download(resp.headers["location"], b, "yas/generations/5.png", "poster-human-5.png")


def test_th_v2_composition_download_is_signed(s3_storage, monkeypatch):
    b = config.producer_assets_bucket
    key = "yas/compositions/talking-head-v2/7.mp4"
    s3_storage.client.put_object(Bucket=b, Key=key, Body=b"mp4", ContentType="video/mp4")

    async def _run_row(run_id):
        return {"id": run_id, "metadata": {
            "composition_status": r.th_v2_service.COMP_READY,
            "composition_storage_bucket": b, "composition_storage_path": key,
        }}

    monkeypatch.setattr(r.th_v2_service, "_fetch_run_row", _run_row)
    resp = _run(r.th_v2_composition_file(7, download=True))
    assert resp.status_code == 307
    _assert_signed_download(resp.headers["location"], b, key, "talking-head-7.mp4")
    inline = _run(r.th_v2_composition_file(7, download=False)).headers["location"]
    assert "response-content-disposition" not in _query(inline)


def test_video_editor_composition_download_is_signed(s3_storage, monkeypatch):
    b = config.producer_assets_bucket
    key = "yas/compositions/video-editor/3.mp4"
    s3_storage.client.put_object(Bucket=b, Key=key, Body=b"mp4", ContentType="video/mp4")

    async def _locator(project_id):
        return {"local": None, "storage_bucket": b, "storage_path": key}

    monkeypatch.setattr(r.ve_service, "composition_locator", _locator)
    resp = _run(r.ve_composition_file(3, download=True))
    assert resp.status_code == 307
    _assert_signed_download(resp.headers["location"], b, key, "video-3.mp4")


def test_download_routes_never_append_to_a_signed_url():
    """Source guard: the old post-sign appends must not come back."""
    src = inspect.getsource(r)
    assert "download={quote(" not in src
    assert '"?") + f"download=' not in src
    assert "_cache_bust(url, it)" not in src


# --- D10: Library tiles -------------------------------------------------------

def test_signed_library_thumbs_carry_no_cache_buster(s3_config):
    item = {
        "id": 1, "created_at": datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        "thumbnail_url": "/api/yas-producer/poster-human/1/file?v=2026-09-01T10%3A00%3A00%2B00%3A00",
        "_thumb_src": (config.producer_assets_bucket, "yas/generations/1.png"),
    }
    _run(r._apply_signed_thumbs([item]))
    url = item["thumbnail_url"]
    assert url.startswith(f"{S3_TEST_PUBLIC_BASE}/{config.producer_assets_bucket}/yas/generations/1.png?")
    q = _query(url)
    assert "X-Amz-Signature" in q and "v" not in q
    assert "_thumb_src" not in item
    # Same URL the batch signer hands out: nothing appended.
    signed = _run(asset_storage.signed_render_urls(
        config.producer_assets_bucket, ["yas/generations/1.png"], expires=r._SIGNED_THUMB_TTL,
    ))
    assert url == signed["yas/generations/1.png"]


def test_library_tiles_get_the_small_copy_once_it_exists(s3_storage):
    """MinIO cannot resize, so a tile is its 480px stored copy: the original
    (about 1.3 MB per poster) only on the first load, while the copy is made."""
    from app.services import thumbs

    bucket = config.producer_media_bucket
    buf = BytesIO()
    Image.new("RGB", (1080, 1350), (10, 20, 30)).save(buf, format="PNG")
    s3_storage.client.put_object(Bucket=bucket, Key="poster/5.png", Body=buf.getvalue())

    def _tile():
        return {"created_at": "2026-09-01T10:00:00+00:00", "_thumb_src": (bucket, "poster/5.png")}

    async def _go():
        first = _tile()
        await r._apply_signed_thumbs([first])
        while thumbs._tasks:
            await asyncio.gather(*list(thumbs._tasks))
        second = _tile()
        await r._apply_signed_thumbs([second])
        return first, second

    first, second = _run(_go())
    assert urlsplit(first["thumbnail_url"]).path == f"/media/{bucket}/poster/5.png"
    assert urlsplit(second["thumbnail_url"]).path == f"/media/{bucket}/thumbs/poster/5.png.webp"
    assert "X-Amz-Signature" in _query(second["thumbnail_url"])
    assert "_thumb_src" not in first and "_thumb_src" not in second


def test_signed_library_thumbs_untouched_on_supabase_too(monkeypatch):
    async def _probe(*a, **kw):
        return None

    async def _sign(bucket, paths, *, expires=86400):
        return {p: f"https://sb.test/storage/v1/render/image/sign/{bucket}/{p}?token=t" for p in paths}

    monkeypatch.setattr(asset_storage, "ensure_image_transform_capability", _probe)
    monkeypatch.setattr(asset_storage, "signed_render_urls", _sign)
    item = {"created_at": "2026-09-01T10:00:00+00:00", "_thumb_src": ("producer-assets", "a.png")}
    _run(r._apply_signed_thumbs([item]))
    assert item["thumbnail_url"] == "https://sb.test/storage/v1/render/image/sign/producer-assets/a.png?token=t"


def test_public_urls_keep_the_buster_and_private_rows_get_signed(s3_config):
    _run(asset_storage.ensure_bucket_publicness())
    old = config.producer_media_public
    object.__setattr__(config, "producer_media_public", True)
    try:
        rec = {"created_at": "2026-09-01T10:00:00+00:00"}
        # producer-media is private on S3 whatever PRODUCER_MEDIA_PUBLIC says, so
        # the tile stays on the proxy and is marked for signing.
        proxy = r._public_media_url(
            rec, bucket=config.producer_media_bucket, path="poster/1.png",
            proxy_url="/api/yas-producer/poster/1/file", thumb=True,
        )
        assert proxy.startswith("/api/yas-producer/poster/1/file?v=")
        assert r._needs_signed_thumb(proxy, config.producer_media_bucket, "poster/1.png")
        # The anonymous bucket gets an absolute public URL, which may keep ?v=
        # and must not be re-signed.
        pub = r._public_media_url(
            rec, bucket=config.approved_media_bucket, path="poster/1_ig_ab.jpg",
            proxy_url="/api/x", thumb=True,
        )
        assert pub.startswith(
            f"{S3_TEST_PUBLIC_BASE}/{config.approved_media_bucket}/poster/1_ig_ab.jpg?v="
        )
        assert not r._needs_signed_thumb(pub, config.approved_media_bucket, "poster/1_ig_ab.jpg")
    finally:
        object.__setattr__(config, "producer_media_public", old)


# --- D15: _media_bytes falls through to the local copy --------------------------

@pytest.mark.parametrize("bucket_result", ["raises", "empty"])
def test_media_bytes_falls_back_to_local(monkeypatch, tmp_path, bucket_result):
    async def _download(*, storage_bucket, storage_path):
        if bucket_result == "raises":
            raise asset_storage.AssetStorageError("S3 get failed: NoSuchKey")
        return b""

    monkeypatch.setattr(asset_storage, "download_bytes", _download)
    f = tmp_path / "frame.png"
    f.write_bytes(b"local copy")
    got = _run(r._media_bytes(
        storage_bucket="producer-media", storage_path="animation/1-frame.png", local_path=str(f),
    ))
    assert got == b"local copy"
    # Neither copy: None, never an exception.
    assert _run(r._media_bytes(
        storage_bucket="producer-media", storage_path="x.png", local_path=str(tmp_path / "gone.png"),
    )) is None


def test_media_bytes_prefers_the_bucket(monkeypatch, tmp_path):
    async def _download(*, storage_bucket, storage_path):
        return b"bucket copy"

    monkeypatch.setattr(asset_storage, "download_bytes", _download)
    f = tmp_path / "frame.png"
    f.write_bytes(b"local copy")
    assert _run(r._media_bytes(
        storage_bucket="producer-media", storage_path="a.png", local_path=str(f),
    )) == b"bucket copy"


def test_media_bytes_missing_s3_object_uses_local(s3_storage, tmp_path):
    f = tmp_path / "p.png"
    f.write_bytes(b"on disk")
    assert _run(r._media_bytes(
        storage_bucket=config.producer_media_bucket, storage_path="poster/404.png",
        local_path=str(f),
    )) == b"on disk"


# --- A23: server-side fetches use the internal origin ---------------------------

def test_asset_refs_are_fetched_on_the_internal_origin(s3_storage, monkeypatch, tmp_path):
    b = config.producer_assets_bucket
    s3_storage.client.put_object(Bucket=b, Key="chars/a.png", Body=b"x", ContentType="image/png")
    db = _FakeDB([[{"id": 7, "storage_bucket": b, "storage_path": "chars/a.png",
                    "local_path": None, "mime_type": "image/png"}]])
    monkeypatch.setattr(r, "get_conn", db.get_conn)
    monkeypatch.setattr(r, "YAS_ROOT", tmp_path)
    seen = _fake_httpx(monkeypatch, content=b"ref bytes")
    paths = _run(r._resolve_asset_paths([7]))
    assert seen and seen[0].startswith(f"{S3_TEST_ENDPOINT}/{b}/chars/a.png?")
    assert "X-Amz-Signature" in _query(seen[0])
    assert paths[0].read_bytes() == b"ref bytes"


def test_feedback_parent_image_is_fetched_on_the_internal_origin(s3_storage, monkeypatch, tmp_path):
    b = config.producer_assets_bucket
    s3_storage.client.put_object(Bucket=b, Key="yas/generations/11.png", Body=b"x")
    db = _FakeDB([{"topic": "open day", "local_path": None, "storage_bucket": b,
                   "storage_path": "yas/generations/11.png", "mime_type": "image/png"}])
    monkeypatch.setattr(r, "get_conn", db.get_conn)
    monkeypatch.setattr(r, "YAS_ROOT", tmp_path)
    seen = _fake_httpx(monkeypatch, content=b"parent")
    got_images = []

    async def _multimodal(prompt, *, images, **kw):
        got_images.extend(images)
        return "brighten the headline"

    monkeypatch.setattr(r, "gemini_multimodal", _multimodal)
    assert _run(r._enhance_feedback("make it pop", 11)) == "brighten the headline"
    assert seen[0].startswith(f"{S3_TEST_ENDPOINT}/{b}/yas/generations/11.png?")
    assert got_images and got_images[0].read_bytes() == b"parent"


def test_save_to_assets_bucket_read_uses_internal_origin_and_stores_a_thumb(
    s3_storage, monkeypatch,
):
    b = config.producer_assets_bucket
    png = _png()
    s3_storage.client.put_object(Bucket=b, Key="yas/generations/5.png", Body=png)
    gen = {"id": 5, "topic": "صورة final", "status": r.STATUS_READY, "saved_asset_id": None,
           "prompt_used": "p", "local_path": None, "storage_bucket": b,
           "storage_path": "yas/generations/5.png", "mime_type": "image/png"}
    db = _FakeDB([gen, {"id": 99, "storage_bucket": None, "storage_path": None}])
    monkeypatch.setattr(r, "get_conn", db.get_conn)
    seen = _fake_httpx(monkeypatch, content=png)
    _no_ai(monkeypatch)

    out = _run(r.save_to_assets(5, r.SaveToAssetsRequest()))
    assert out["id"] == 99
    assert seen[0].startswith(f"{S3_TEST_ENDPOINT}/{b}/yas/generations/5.png?")
    _assert_writer_row(db, s3_storage, "instagram/img_poster_human/final_")


def test_save_to_assets_410_when_the_bucket_read_fails(s3_storage, monkeypatch):
    b = config.producer_assets_bucket
    s3_storage.client.put_object(Bucket=b, Key="yas/generations/6.png", Body=b"x")
    db = _FakeDB([{"id": 6, "topic": "t", "status": r.STATUS_READY, "saved_asset_id": None,
                   "prompt_used": "p", "local_path": None, "storage_bucket": b,
                   "storage_path": "yas/generations/6.png", "mime_type": "image/png"}])
    monkeypatch.setattr(r, "get_conn", db.get_conn)
    seen = _fake_httpx(monkeypatch, status_code=404)
    with pytest.raises(HTTPException) as e:
        _run(r.save_to_assets(6, r.SaveToAssetsRequest()))
    assert e.value.status_code == 410
    assert seen[0].startswith(S3_TEST_ENDPOINT)


# --- A17 + A4: producer_assets writers ------------------------------------------

def _assert_writer_row(db: _FakeDB, s3_storage, key_prefix: str) -> str:
    """The INSERT names `attributes`, has one placeholder per param, stores an
    ASCII key in producer-assets, and records a thumbnail that really exists."""
    sql, params = db.insert()
    assert "attributes" in sql
    assert sql.count("%s") == len(params)
    bucket, key = params[3], params[4]
    assert bucket == config.producer_assets_bucket
    assert key.startswith(key_prefix) and key.endswith(".png"), key
    assert _KEY_RE.match(key), key
    attrs = params[-1].obj
    assert attrs == {"_thumb_path": f"thumbs/{key}.webp"}
    head = s3_storage.client.head_object(Bucket=bucket, Key=attrs["_thumb_path"])
    assert head["ContentType"] == "image/webp"
    s3_storage.client.head_object(Bucket=bucket, Key=key)  # the original too
    return key


def _local_png(tmp_path) -> str:
    f = tmp_path / "src.png"
    f.write_bytes(_png())
    return str(f)


def _poster_case(monkeypatch, tmp_path):
    async def _get(gen_id):
        return {"id": gen_id, "mode": r.POSTER_MODE, "status": r.STATUS_READY,
                "topic": "صورة final", "prompt_used": "p", "mime_type": "image/png",
                "storage_bucket": None, "storage_path": None, "local_path": _local_png(tmp_path)}

    async def _update(*a, **kw):
        return None

    monkeypatch.setattr(r.ystore, "get", _get)
    monkeypatch.setattr(r.ystore, "update", _update)
    return (lambda: r.poster_save(12, r.PosterSaveRequest())), "instagram/img_poster/final_"


def _human_character_case(monkeypatch, tmp_path):
    async def _get(gen_id):
        return {"id": gen_id, "mode": r.HUMAN_CHARACTER_MODE, "status": r.STATUS_READY,
                "topic": "t", "prompt_used": "p", "mime_type": "image/png",
                "storage_bucket": None, "storage_path": None, "local_path": _local_png(tmp_path)}

    async def _update(*a, **kw):
        return None

    monkeypatch.setattr(r.ystore, "get", _get)
    monkeypatch.setattr(r.ystore, "update", _update)
    body = r.PosterSaveRequest(display_name="Ahmed + Sara / lab")
    return (lambda: r.human_character_save(4, body)), "instagram/img_human_character/ahmed_sara_lab_"


def _animation_case(monkeypatch, tmp_path):
    async def _get(anim_id):
        return {"id": anim_id, "saved_asset_id": None, "status": "frame_ready",
                "prompt": "مرحبا", "image_prompt_used": "p", "mime_type": "video/mp4",
                "frame_storage_bucket": None, "frame_storage_path": None,
                "frame_local_path": _local_png(tmp_path)}

    async def _update(*a, **kw):
        return None

    monkeypatch.setattr(r.anim_store, "get", _get)
    monkeypatch.setattr(r.anim_store, "update", _update)
    # An all-Arabic prompt keeps nothing ASCII, so the fallback names the key.
    return (lambda: r.animation_save(9, r.PosterSaveRequest())), "instagram/img_poster/animation_9_"


def _general_still_case(monkeypatch, tmp_path):
    async def _get(gen_id):
        return {"id": gen_id, "prompt": "Café scene", "mime_type": "image/png",
                "frame_storage_bucket": None, "frame_storage_path": None,
                "frame_local_path": _local_png(tmp_path)}

    monkeypatch.setattr(r.gv_store, "get", _get)
    return (lambda: r.general_video_save_still(2, r.PosterSaveRequest())), "instagram/img_general_still/cafe_scene_"


@pytest.mark.parametrize("case", [
    _poster_case, _human_character_case, _animation_case, _general_still_case,
], ids=["poster", "human_character", "animation", "general_still"])
def test_image_writers_store_a_thumbnail_and_an_ascii_key(s3_storage, monkeypatch, tmp_path, case):
    call, prefix = case(monkeypatch, tmp_path)
    db = _FakeDB([{"id": 42, "storage_bucket": None, "storage_path": None}])
    monkeypatch.setattr(r, "get_conn", db.get_conn)
    _no_ai(monkeypatch)
    out = _run(call())
    assert out["id"] == 42
    _assert_writer_row(db, s3_storage, prefix)


def test_image_writer_on_local_disk_records_empty_attributes(monkeypatch, tmp_path):
    """Local mode: no bucket, so no derivative and an empty attributes object."""
    old_root = config.producer_assets_root
    object.__setattr__(config, "producer_assets_root", tmp_path / "data" / "assets_library")
    try:
        call, _ = _poster_case(monkeypatch, tmp_path)
        db = _FakeDB([{"id": 42}])
        monkeypatch.setattr(r, "get_conn", db.get_conn)
        _no_ai(monkeypatch)
        _run(call())
        sql, params = db.insert()
        assert params[3] is None and params[4] is None and params[5]
        assert params[-1].obj == {}
    finally:
        object.__setattr__(config, "producer_assets_root", old_root)


def test_asset_slug_is_ascii_and_unique():
    s = r._asset_slug("صورة final", "yas_1")
    assert re.fullmatch(r"final_[0-9a-f]{6}", s)
    assert re.fullmatch(r"yas_1_[0-9a-f]{6}", r._asset_slug("صورة", "yas_1"))
    assert re.fullmatch(r"yas_1_[0-9a-f]{6}", r._asset_slug(None, "yas_1"))
    long = r._asset_slug("x" * 200, "f")
    assert len(long.rsplit("_", 1)[0]) <= 32
    assert r._asset_slug("same", "f") != r._asset_slug("same", "f")


def test_no_unicode_slug_builder_is_left():
    assert ".isalnum()" not in inspect.getsource(r)


# --- A19: cancel frees the media and every carousel slide -----------------------

def _patch_sched(monkeypatch, row):
    from app.services import scheduled_posts

    cancelled = []

    async def _get(post_id):
        return row

    async def _cancel(post_id):
        cancelled.append(post_id)
        return row

    monkeypatch.setattr(scheduled_posts, "get", _get)
    monkeypatch.setattr(scheduled_posts, "cancel", _cancel)
    return cancelled


def test_cancel_deletes_every_carousel_slide_on_s3(s3_storage, monkeypatch):
    b = config.approved_media_bucket
    # approved_media's key shape: {tool}/{ref_id}_ig_{hex}.{ext}
    keys = ["poster/1_ig_aa.jpg", "animation/2_ig_bb.mp4", "poster_human/3_ig_cc.jpg"]
    for k in keys:
        s3_storage.client.put_object(Bucket=b, Key=k, Body=b"j", ContentType="image/jpeg")
    s3_storage.client.put_object(Bucket=b, Key="poster/keep_ig_dd.jpg", Body=b"j")
    urls = [asset_storage.public_url(b, k) for k in keys]
    row = {"id": 5, "status": "scheduled", "media_url": urls[0], "metadata": {
        "is_carousel": True,
        "carousel_items": [{"media_url": urls[0]}, {"media_url": urls[1]},
                           {"media_url": urls[2] + "?v=1"}],
    }}
    cancelled = _patch_sched(monkeypatch, row)
    assert _run(r.roadmap_cancel(5)) == {"ok": True}
    assert cancelled == [5]
    left = {o["Key"] for o in s3_storage.client.list_objects_v2(Bucket=b).get("Contents", [])}
    assert left == {"poster/keep_ig_dd.jpg"}


def test_cancel_understands_the_supabase_url_shape(monkeypatch):
    deleted = []

    async def _delete(*, storage_bucket, storage_path, local_path):
        deleted.append((storage_bucket, storage_path))

    monkeypatch.setattr(asset_storage, "delete", _delete)
    row = {"id": 6, "status": "scheduled",
           "media_url": "https://x.supabase.co/storage/v1/object/public/approved-instagram/poster/6_ig_ab.jpg",
           "metadata": {"carousel_items": [{"media_url": "/api/yas-producer/poster/6/file"}, "junk"]}}
    _patch_sched(monkeypatch, row)
    assert _run(r.roadmap_cancel(6)) == {"ok": True}
    assert deleted == [("approved-instagram", "poster/6_ig_ab.jpg")]


def test_cancel_survives_a_storage_failure(monkeypatch):
    async def _boom(urls):
        raise RuntimeError("storage down")

    monkeypatch.setattr(asset_storage, "delete_public_urls", _boom)
    _patch_sched(monkeypatch, {"id": 7, "status": "scheduled", "media_url": "https://h/m.jpg"})
    assert _run(r.roadmap_cancel(7)) == {"ok": True}


def test_a_cancelled_row_thumbnails_from_the_proxy():
    """Cancel deletes the hosted copy but keeps media_url on the row, so the
    Scheduled queue must not render that dead URL."""
    hosted = f"{S3_TEST_PUBLIC_BASE}/approved-instagram/poster/3_ig_ab.jpg"
    row = {"id": 1, "tool": "poster", "ref_id": 3, "media_type": "image",
           "media_url": hosted, "status": "cancelled"}
    out = r._serialize_scheduled(row)
    assert out["display_url"] == "/api/yas-producer/poster/3/file"
    assert out["media_url"] == hosted
    for status in ("pending", "reminded", "published", "failed"):
        assert r._serialize_scheduled({**row, "status": status})["display_url"] == hosted
    # Every bookable tool has a proxy, B-roll included (it is a Video-Editor project).
    from app.services.scheduled_posts import VALID_TOOLS
    assert set(VALID_TOOLS) <= set(r._ROADMAP_FILE_PATH)
    assert r._serialize_scheduled({**row, "tool": "broll"})["display_url"] == (
        "/api/yas-producer/video-editor/3/composition/file")
    assert r._serialize_scheduled({**row, "tool": "general_video"})["display_url"] == (
        "/api/yas-producer/general-video/3/file")
    # No proxy for this tool: keep what the row has.
    assert r._serialize_scheduled({**row, "tool": "mystery"})["display_url"] == hosted


@pytest.mark.parametrize("error,status", [
    (FileNotFoundError("animation #7: file not ready"), 410),
    (asset_storage.AssetStorageError("S3 get producer-media/animation/7.mp4: storage unreachable"), 503),
])
def test_open_in_editor_tells_a_missing_file_from_a_store_outage(monkeypatch, error, status):
    from app.services import approved_media

    async def _get(_id):
        return {"id": 7, "status": r.STATUS_READY, "video_storage_path": "animation/7.mp4"}

    async def _resolve(tool, ref_id):
        raise error

    monkeypatch.setattr(r.anim_store, "get", _get)
    monkeypatch.setattr(approved_media, "_resolve_media", _resolve)
    with pytest.raises(HTTPException) as info:
        _run(r.animation_open_in_editor(7))
    assert info.value.status_code == status


def test_hosted_media_urls_collects_media_and_slides():
    # Cancel uses the one definition scheduled_posts.delete_by_ref also uses.
    from app.services import scheduled_posts

    row = {"media_url": "u1", "metadata": {"carousel_items": [
        {"media_url": "u1"}, {"media_url": "u2"}, {"tool": "poster"}, None]}}
    assert scheduled_posts.hosted_media_urls(row) == ["u1", "u1", "u2"]
    assert scheduled_posts.hosted_media_urls({"media_url": None, "metadata": None}) == []
    assert "hosted_media_urls" in inspect.getsource(r._delete_hosted_media)


# --- IG approve path wording -----------------------------------------------------

def test_instagram_paths_do_not_assume_supabase():
    for fn in (r.roadmap_schedule, r.roadmap_approve):
        assert "Supabase" not in inspect.getsource(fn), fn.__name__
    # Cancel leaves URL parsing to asset_storage, which knows both shapes.
    for fn in (r._delete_hosted_media, r.roadmap_cancel):
        assert "/object/public/" not in inspect.getsource(fn), fn.__name__


def test_approve_skip_detail_is_plain(monkeypatch):
    from app.services import approved_media, scheduled_posts

    async def _get(post_id):
        return {"id": post_id, "status": "scheduled", "tool": "poster", "ref_id": 3,
                "media_type": "image", "media_url": None, "metadata": {}}

    async def _upload(**kw):
        raise asset_storage.AssetStorageError("media storage is not set up")

    monkeypatch.setattr(scheduled_posts, "get", _get)
    monkeypatch.setattr(approved_media, "upload_for_post", _upload)
    out = _run(r.roadmap_approve(1))
    assert out["status"] == "skipped"
    assert out["detail"] == (
        "Media isn't hosted yet. Couldn't upload it to public media storage: "
        "media storage is not set up"
    )
