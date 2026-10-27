"""Media-pipeline call sites on the S3 (MinIO) backend.

Covers the producer_assets router (ASCII keys, a stored thumbnail for every
image writer, the D9 grid rule, /file falling through to the bucket, font
previews, derivative cleanup on delete), the video-edit agent's sticker
thumbnail, the server-side bucket fetches that must sign on the internal origin
(A23), and the composition backups that deletes must remove.

Offline: moto's in-process S3 (the `s3_storage` fixture) or plain local disk,
with a fake database connection and fake httpx clients.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from PIL import Image
from psycopg.errors import UniqueViolation

from app.config import config
from app.routers import producer_assets as pa
from app.services import asset_storage, thumbs
from app.services import yas_talking_head as th1
from app.services import yas_talking_head_v2 as th2
from app.services import yas_video_edit_agent as agent
from app.services import yas_video_editor as ve
from app.workers import yas_th_composition_poller as th1_poller
from app.workers import yas_th_v2_composition_poller as th2_poller
from app.workers import yas_video_edit_composition_poller as ve_poller


_SLUG_RE = r"[a-z0-9_]+_[0-9a-f]{6}"


def _run(coro):
    return asyncio.run(coro)


def _png(size=(900, 600)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeDB:
    """A stand-in for db.get_conn. `results` holds (sql_fragment, value) pairs,
    each consumed by the first statement containing the fragment; `raise_on`
    maps a fragment to an exception raised by that statement."""

    def __init__(self):
        self.log: list[tuple[str, object]] = []
        self.results: list[tuple[str, object]] = []
        self.raise_on: dict[str, Exception] = {}

    def _answer(self, sql: str):
        for needle, exc in self.raise_on.items():
            if needle in sql:
                raise exc
        for i, (needle, value) in enumerate(self.results):
            if needle in sql:
                del self.results[i]
                return value
        return None

    def get_conn(self):
        db = self

        class _Cursor:
            def __init__(self):
                self._last = None

            async def execute(self, sql, params=None):
                sql = " ".join(str(sql).split())
                db.log.append((sql, params))
                self._last = db._answer(sql)

            async def fetchone(self):
                if isinstance(self._last, list):
                    return self._last[0] if self._last else None
                return self._last

            async def fetchall(self):
                if isinstance(self._last, list):
                    return self._last
                return [] if self._last is None else [self._last]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Conn:
            def cursor(self):
                return _Cursor()

        @asynccontextmanager
        async def _get_conn(*_args, **_kwargs):
            yield _Conn()

        return _get_conn

    def params_of(self, fragment: str):
        for sql, params in self.log:
            if fragment in sql:
                return params
        raise AssertionError(f"no statement containing {fragment!r}")


def _fake_http(seen: list, *, body: bytes = b"bytes", error: Exception | None = None):
    """Factory for httpx.AsyncClient: records every GET url."""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            seen.append(url)
            if error is not None:
                raise error
            return SimpleNamespace(status_code=200, content=body)

    return _Client


@pytest.fixture
def assets_root(tmp_path):
    """Local-disk roots in a temp tree: backend/data/assets_library and
    backend/data/assets (config is frozen, so set and restore by hand)."""
    data = tmp_path / "backend" / "data"
    old = (config.producer_assets_root, config.asset_root)
    object.__setattr__(config, "producer_assets_root", data / "assets_library")
    object.__setattr__(config, "asset_root", data / "assets")
    try:
        yield data / "assets_library"
    finally:
        object.__setattr__(config, "producer_assets_root", old[0])
        object.__setattr__(config, "asset_root", old[1])


def _keys(client, bucket: str) -> list[str]:
    return [o["Key"] for o in client.list_objects_v2(Bucket=bucket).get("Contents", [])]


# ---------------------------------------------------------------------------
# producer_assets: keys and shared thumbnail helpers
# ---------------------------------------------------------------------------

def test_slugify_builds_ascii_keys():
    for name in ("صورة final", "Café Latte+Milk", "  ", "صورة", "../etc/passwd", "a" * 80):
        assert re.fullmatch(_SLUG_RE, pa._slugify(name, "ig_1_f3")), name
    assert pa._slugify("صورة final", "x").startswith("final_")
    assert pa._slugify("صورة", "ig_12_f3").startswith("ig_12_f3_")
    assert pa._slugify("Café Latte+Milk", "x").startswith("cafe_latte_milk_")
    assert len(pa._slugify("a" * 80, "x")) == 32 + 7


@pytest.mark.parametrize("filename,mime,ext", [
    ("photo.JPG", "image/jpeg", "jpg"),
    ("photo.JPG copy", "image/png", "png"),
    ("صورة.png", None, "png"),
    ("font.woff2", "application/octet-stream", "woff2"),
    (None, "video/mp4", "mp4"),
    ("noext", None, "bin"),
    (None, None, "bin"),
])
def test_ext_from_never_passes_a_raw_suffix_through(filename, mime, ext):
    assert pa._ext_from(filename, mime) == ext


def test_producer_assets_uses_the_shared_thumbnail_helpers():
    assert pa.ensure_thumb_object is thumbs.ensure_thumb_object
    assert pa._thumb_object_path is thumbs.thumb_object_path
    assert pa._make_thumb_bytes is thumbs.make_thumb_bytes
    assert (pa._THUMB_PREFIX, pa._THUMB_BOX, pa._THUMB_WEBP_QUALITY) == (
        thumbs.THUMB_PREFIX, thumbs.THUMB_BOX, thumbs.THUMB_WEBP_QUALITY,
    )


# ---------------------------------------------------------------------------
# producer_assets: the grid (D9)
# ---------------------------------------------------------------------------

def _grid_rows() -> list[dict]:
    b = config.producer_assets_bucket
    return [
        {"id": 1, "asset_kind": "image", "mime_type": "image/png", "storage_bucket": b,
         "storage_path": "manual/a.png", "attributes": {"_thumb_path": "thumbs/manual/a.png.webp"}},
        {"id": 2, "asset_kind": "image", "mime_type": "image/jpeg", "storage_bucket": b,
         "storage_path": "manual/b.jpg", "attributes": {}},
        {"id": 3, "asset_kind": "image", "mime_type": "image/png", "storage_bucket": None,
         "storage_path": None, "local_path": "data/assets_library/c.png",
         "attributes": {"_thumb_path": "thumbs/stale.webp"}},
        {"id": 4, "asset_kind": "video", "mime_type": "video/mp4", "storage_bucket": b,
         "storage_path": "videos/d.mp4", "attributes": {}},
    ]


def test_s3_grid_signs_only_stored_thumbnails(s3_config):
    rows = _grid_rows()
    _run(pa._attach_thumb_urls(rows))
    by_id = {r["id"]: r for r in rows}
    url = by_id[1]["thumb_url"]
    assert url.startswith(
        f"{s3_config.public_base}/{config.producer_assets_bucket}/thumbs/manual/a.png.webp?"
    )
    assert "X-Amz-Signature=" in url
    # No derivative: no thumb_url, never the full original (D9).
    for i in (2, 3, 4):
        assert "thumb_url" not in by_id[i], i


def test_other_backends_still_fall_back_to_the_original(monkeypatch):
    async def _sign(bucket, paths, *, expires=86400):
        return {p: f"https://signed.example/{bucket}/{p}" for p in paths}

    monkeypatch.setattr(asset_storage, "signed_render_urls", _sign)
    rows = _grid_rows()
    _run(pa._attach_thumb_urls(rows))
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["thumb_url"].endswith("/thumbs/manual/a.png.webp")
    assert by_id[2]["thumb_url"].endswith("/manual/b.jpg")
    assert "thumb_url" not in by_id[3] and "thumb_url" not in by_id[4]


# ---------------------------------------------------------------------------
# producer_assets: /{id}/file (A22)
# ---------------------------------------------------------------------------

def _serve_file(monkeypatch, row: dict):
    db = _FakeDB()
    db.results.append(("FROM producer_assets", row))
    monkeypatch.setattr(pa, "get_conn", db.get_conn())
    return _run(pa.get_asset_file(1))


def _file_row(**kw) -> dict:
    row = {"storage_bucket": None, "storage_path": None, "local_path": None,
           "mime_type": "image/png", "slug": "s"}
    row.update(kw)
    return row


def test_file_route_falls_through_to_the_bucket_when_the_disk_copy_is_gone(
    monkeypatch, s3_storage, assets_root,
):
    b = config.producer_assets_bucket
    s3_storage.client.put_object(Bucket=b, Key="manual/a.png", Body=b"x", ContentType="image/png")
    resp = _serve_file(monkeypatch, _file_row(
        local_path="data/assets_library/manual/a.png", storage_bucket=b, storage_path="manual/a.png",
    ))
    assert isinstance(resp, RedirectResponse) and resp.status_code == 307
    assert resp.headers["location"].startswith(f"{s3_storage.public_base}/{b}/manual/a.png?")


def test_file_route_prefers_the_disk_copy(monkeypatch, s3_storage, assets_root):
    b = config.producer_assets_bucket
    local = assets_root / "manual" / "a.png"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"local")
    resp = _serve_file(monkeypatch, _file_row(
        local_path="data/assets_library/manual/a.png", storage_bucket=b, storage_path="manual/a.png",
    ))
    assert isinstance(resp, FileResponse) and resp.path == str(local)


def test_file_route_errors_only_when_neither_copy_exists(monkeypatch, s3_storage, assets_root):
    b = config.producer_assets_bucket
    with pytest.raises(HTTPException) as gone:
        _serve_file(monkeypatch, _file_row(
            local_path="data/assets_library/manual/a.png", storage_bucket=b, storage_path="manual/a.png",
        ))
    assert gone.value.status_code == 410
    with pytest.raises(HTTPException) as missing:
        _serve_file(monkeypatch, _file_row(storage_bucket=b, storage_path="manual/a.png"))
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as local_only:
        _serve_file(monkeypatch, _file_row(local_path="data/assets_library/manual/a.png"))
    assert local_only.value.status_code == 410


# ---------------------------------------------------------------------------
# producer_assets: writers
# ---------------------------------------------------------------------------

def _patch_analysis(monkeypatch):
    monkeypatch.setattr(
        pa, "describe_font", lambda path: ("A display font", {"family": "Test"}, b"preview-png"),
    )

    async def _embed(texts):
        return [[0.0, 0.0, 0.0]]

    monkeypatch.setattr(pa, "gemini_embed", _embed)


def _upload_font(monkeypatch) -> tuple[_FakeDB, tuple]:
    _patch_analysis(monkeypatch)
    db = _FakeDB()
    db.results.append(("INSERT INTO producer_assets", {"id": 5}))
    monkeypatch.setattr(pa, "get_conn", db.get_conn())
    _run(pa._process_asset_bytes(
        raw=b"font-bytes", filename="Brand Font.ttf", mime="font/ttf", asset_kind="font",
        category=None, archetype=None, display_name=None, hint=None,
    ))
    return db, db.params_of("INSERT INTO producer_assets")


def _preview_route(monkeypatch, row: dict):
    db = _FakeDB()
    db.results.append(("FROM producer_assets", row))
    monkeypatch.setattr(pa, "get_conn", db.get_conn())
    return _run(pa.get_asset_preview(5))


def test_local_font_preview_is_found_by_the_preview_route(monkeypatch, assets_root):
    _db, params = _upload_font(monkeypatch)
    # INSERT order: slug, kind, category, archetype, name, bucket, key, local,
    # mime, bytes, width, height, duration, description, attributes, ...
    assert params[5] is None and params[6] is None
    font_local = params[7].replace("\\", "/")
    assert re.fullmatch(rf"data/assets_library/fonts/{_SLUG_RE}\.ttf", font_local)
    attrs = params[14].obj
    preview = attrs["_preview_path"]
    assert re.fullmatch(r"fonts/_preview/brand_font_ttf_[0-9a-f]{6}\.png", preview)
    assert (assets_root / preview).read_bytes() == b"preview-png"
    assert pa._preview_local_abs(preview) == assets_root / preview

    resp = _preview_route(monkeypatch, {
        "asset_kind": "font", "attributes": attrs,
        "storage_bucket": None, "storage_path": None, "local_path": params[7],
    })
    assert isinstance(resp, FileResponse) and resp.path == str(assets_root / preview)


def test_s3_font_and_preview_share_the_bucket(monkeypatch, s3_storage):
    _db, params = _upload_font(monkeypatch)
    b = config.producer_assets_bucket
    assert params[5] == b and re.fullmatch(rf"fonts/{_SLUG_RE}\.ttf", params[6])
    preview = params[14].obj["_preview_path"]
    assert re.fullmatch(r"fonts/_preview/brand_font_ttf_[0-9a-f]{6}\.png", preview)
    head = s3_storage.client.head_object(Bucket=b, Key=preview)
    assert head["ContentType"] == "image/png"


def test_font_preview_stays_beside_a_font_that_fell_back_to_disk(monkeypatch, s3_config, assets_root):
    from app.services import s3_backend

    puts = []

    async def _failing_put(bucket, key, data, **kwargs):
        puts.append(key)
        raise asset_storage.AssetStorageError("store down")

    monkeypatch.setattr(s3_backend, "put_bytes", _failing_put)
    _db, params = _upload_font(monkeypatch)
    # Only the font tried the bucket; its preview went straight to disk beside it.
    assert len(puts) == 1 and puts[0].startswith("fonts/brand_font_")
    preview = params[14].obj["_preview_path"]
    assert (assets_root / preview).read_bytes() == b"preview-png"


def test_s3_preview_route_fetches_on_the_internal_origin(monkeypatch, s3_storage):
    b = config.producer_assets_bucket
    key = "fonts/_preview/brand_font_abc123.png"
    s3_storage.client.put_object(Bucket=b, Key=key, Body=b"png", ContentType="image/png")
    seen: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_http(seen, body=b"PNGDATA"))
    resp = _preview_route(monkeypatch, {
        "asset_kind": "font", "attributes": {"_preview_path": key},
        "storage_bucket": b, "storage_path": "fonts/brand_font_abc123.ttf", "local_path": None,
    })
    assert isinstance(resp, Response) and resp.body == b"PNGDATA"
    assert seen and seen[0].startswith(f"{s3_storage.endpoint}/{b}/{key}?")


def test_preview_route_falls_back_to_disk_when_the_bucket_fetch_fails(
    monkeypatch, s3_storage, assets_root,
):
    b = config.producer_assets_bucket
    key = "fonts/_preview/brand_font_abc123.png"
    s3_storage.client.put_object(Bucket=b, Key=key, Body=b"png", ContentType="image/png")
    local = assets_root / key
    local.parent.mkdir(parents=True)
    local.write_bytes(b"png")
    seen: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_http(seen, error=httpx.ConnectError("down")))
    resp = _preview_route(monkeypatch, {
        "asset_kind": "font", "attributes": {"_preview_path": key},
        "storage_bucket": b, "storage_path": "fonts/x.ttf", "local_path": None,
    })
    assert seen and isinstance(resp, FileResponse) and resp.path == str(local)


def _frame_request(monkeypatch, tmp_path, db: _FakeDB):
    from app.services import storage as storage_mod

    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (1280, 720), (200, 40, 40)).save(frame, format="JPEG")
    monkeypatch.setattr(storage_mod, "resolve_local", lambda rel: frame)

    async def _embed(texts):
        return [[0.0, 0.0, 0.0]]

    monkeypatch.setattr(pa, "gemini_embed", _embed)
    monkeypatch.setattr(pa, "get_conn", db.get_conn())
    db.results.append(("FROM video_frames", {
        "frame_path": "frames/1.jpg", "timestamp_sec": 2.5, "permalink": None,
        "caption": "كابشن", "post_content_category": None,
    }))
    return pa.FromFrameRequest(post_id=1, asset_id=2, frame_id=3, content_category="img_intro")


def test_create_from_frame_stores_a_thumbnail(monkeypatch, tmp_path, s3_storage):
    db = _FakeDB()
    body = _frame_request(monkeypatch, tmp_path, db)
    db.results.append(("INSERT INTO producer_assets", {"id": 9}))
    _run(pa.create_from_frame(body))
    params = db.params_of("INSERT INTO producer_assets")
    b, key = params[4], params[5]
    assert b == config.producer_assets_bucket
    # An Arabic caption must not reach the key.
    assert re.fullmatch(r"instagram/img_intro/ig_1_f3_[0-9a-f]{6}\.jpg", key)
    assert params[12].obj["_thumb_path"] == f"thumbs/{key}.webp"
    head = s3_storage.client.head_object(Bucket=b, Key=f"thumbs/{key}.webp")
    assert head["ContentType"] == "image/webp"


def test_create_from_frame_race_loser_leaves_no_orphans(monkeypatch, tmp_path, s3_storage):
    db = _FakeDB()
    body = _frame_request(monkeypatch, tmp_path, db)
    db.raise_on["INSERT INTO producer_assets"] = UniqueViolation("duplicate source_frame_id")
    db.results.append(("WHERE source_frame_id", {"id": 3, "display_name": "first"}))
    with pytest.raises(HTTPException) as err:
        _run(pa.create_from_frame(body))
    assert err.value.status_code == 409
    assert err.value.detail["existing_asset_id"] == 3
    assert _keys(s3_storage.client, config.producer_assets_bucket) == []


# ---------------------------------------------------------------------------
# producer_assets: delete
# ---------------------------------------------------------------------------

def _delete(monkeypatch, row: dict) -> None:
    db = _FakeDB()
    db.results.append(("SELECT storage_bucket", row))
    monkeypatch.setattr(pa, "get_conn", db.get_conn())
    assert _run(pa.delete_asset(1)) == {"deleted": 1}


def test_delete_removes_the_stored_thumbnail(monkeypatch, s3_storage):
    b = config.producer_assets_bucket
    for key in ("manual/a.png", "thumbs/manual/a.png.webp"):
        s3_storage.client.put_object(Bucket=b, Key=key, Body=b"x")
    _delete(monkeypatch, {
        "storage_bucket": b, "storage_path": "manual/a.png", "local_path": None,
        "attributes": {"_thumb_path": "thumbs/manual/a.png.webp"}, "asset_kind": "image",
    })
    assert _keys(s3_storage.client, b) == []


def test_delete_skips_a_stale_thumbnail_key_on_a_local_row(monkeypatch, assets_root):
    calls = []

    async def _record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(asset_storage, "delete", _record)
    _delete(monkeypatch, {
        "storage_bucket": None, "storage_path": None, "local_path": "data/assets_library/c.png",
        "attributes": {"_thumb_path": "thumbs/stale.webp"}, "asset_kind": "image",
    })
    assert calls == [{"storage_bucket": None, "storage_path": None,
                      "local_path": "data/assets_library/c.png"}]


def test_delete_removes_a_local_font_preview(monkeypatch, assets_root):
    key = "fonts/_preview/brand_font_abc123.png"
    local = assets_root / key
    local.parent.mkdir(parents=True)
    local.write_bytes(b"png")
    _delete(monkeypatch, {
        "storage_bucket": None, "storage_path": None, "local_path": None,
        "attributes": {"_preview_path": key}, "asset_kind": "font",
    })
    assert not local.exists()


# ---------------------------------------------------------------------------
# Video-edit agent sticker (A17)
# ---------------------------------------------------------------------------

def _store_sticker(monkeypatch) -> tuple:
    from app.services import vertex_client

    async def _embed(texts):
        return [[0.0, 0.0, 0.0]]

    monkeypatch.setattr(vertex_client, "gemini_embed", _embed)
    db = _FakeDB()
    db.results.append(("INSERT INTO producer_assets", {"id": 42}))
    monkeypatch.setattr(agent, "get_conn", db.get_conn())
    assert _run(agent._store_generated_asset(_png(), "Star", "a gold star")) == 42
    # INSERT order: slug, name, description, bucket, key, local, mime, bytes, attributes, ...
    return db.params_of("INSERT INTO producer_assets")


def test_generated_sticker_gets_a_stored_thumbnail(monkeypatch, s3_storage):
    params = _store_sticker(monkeypatch)
    b, key = params[3], params[4]
    assert b == config.producer_assets_bucket
    assert params[8].obj == {"_thumb_path": f"thumbs/{key}.webp"}
    assert s3_storage.client.head_object(Bucket=b, Key=f"thumbs/{key}.webp")["ContentType"] == "image/webp"


def test_generated_sticker_on_local_disk_has_no_thumbnail(monkeypatch, assets_root):
    params = _store_sticker(monkeypatch)
    assert params[3] is None and params[8].obj == {}
    assert not (assets_root / "thumbs").exists()


# ---------------------------------------------------------------------------
# Server-side bucket fetches sign on the internal origin (A23)
# ---------------------------------------------------------------------------

def test_video_editor_downloads_use_the_internal_origin(monkeypatch, tmp_path, s3_storage):
    b = config.producer_assets_bucket
    monkeypatch.setattr(ve, "VE_ROOT", tmp_path / "ve")
    for key in ("yas/animations/5.mp4", "manual/s.png", "yas/generations/9.png", "fonts/f.ttf"):
        s3_storage.client.put_object(Bucket=b, Key=key, Body=b"x")

    async def _anim(anim_id):
        return {"local_path": None, "storage_bucket": b, "storage_path": "yas/animations/5.mp4"}

    async def _asset(asset_id):
        if asset_id == 2:
            return {"asset_kind": "font", "mime_type": "font/ttf", "local_path": None,
                    "storage_bucket": b, "storage_path": "fonts/f.ttf"}
        return {"asset_kind": "image", "local_path": None, "storage_bucket": b,
                "storage_path": "manual/s.png"}

    async def _still(gen_id):
        return {"local_path": None, "storage_bucket": b, "storage_path": "yas/generations/9.png"}

    fetched: list[str] = []

    async def _download_to(url, dest):
        fetched.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return True

    monkeypatch.setattr(ve, "_fetch_animation_row", _anim)
    monkeypatch.setattr(ve, "_fetch_asset_row", _asset)
    monkeypatch.setattr(ve, "_fetch_still_row", _still)
    monkeypatch.setattr(ve, "_download_to", _download_to)

    assert _run(ve.ensure_animation_local(5))
    assert _run(ve.ensure_asset_local(1))
    assert _run(ve.ensure_still_local(9))
    assert _run(ve.ensure_font_local(2))
    assert len(fetched) == 4
    assert all(u.startswith(f"{s3_storage.endpoint}/{b}/") for u in fetched), fetched

    # The browser redirect for the same animation keeps the public origin.
    served = _run(ve.resolve_source_serving("animation", 5))
    assert served["redirect"].startswith(f"{s3_storage.public_base}/{b}/yas/animations/5.mp4?")


@pytest.mark.parametrize("mod", [th1, th2], ids=["th", "th_v2"])
def test_talking_head_bucket_fetches_use_the_internal_origin(monkeypatch, tmp_path, s3_storage, mod):
    b = config.producer_assets_bucket
    monkeypatch.setattr(mod, "TH_ROOT", tmp_path / "th")
    s3_storage.client.put_object(Bucket=b, Key="characters/a.png", Body=b"img")
    s3_storage.client.put_object(Bucket=b, Key="yas/talking-head/1/intro-3.png", Body=b"card")

    async def _asset_row(asset_id):
        return {"local_path": None, "storage_bucket": b, "storage_path": "characters/a.png"}

    async def _card_row(card_id):
        return {"local_path": None, "storage_bucket": b,
                "storage_path": "yas/talking-head/1/intro-3.png"}

    monkeypatch.setattr(mod, "_resolve_asset_row", _asset_row)
    monkeypatch.setattr(mod, "_fetch_card_row", _card_row)
    seen: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_http(seen))

    assert _run(mod._resolve_asset_path(1)).read_bytes() == b"bytes"
    assert _run(mod._resolve_card_path(3)).read_bytes() == b"bytes"
    assert len(seen) == 2
    assert all(u.startswith(f"{s3_storage.endpoint}/{b}/") for u in seen), seen


@pytest.mark.parametrize("poller", [th1_poller, th2_poller], ids=["th", "th_v2"])
def test_composition_pollers_fetch_assets_on_the_internal_origin(
    monkeypatch, tmp_path, s3_storage, poller,
):
    b = config.producer_assets_bucket
    monkeypatch.setattr(poller, "COMP_DIR", tmp_path / "comp")
    s3_storage.client.put_object(Bucket=b, Key="characters/a.png", Body=b"img")
    db = _FakeDB()
    db.results.append(("FROM producer_assets", {
        "storage_bucket": b, "storage_path": "characters/a.png", "local_path": None,
    }))
    monkeypatch.setattr(poller, "get_conn", db.get_conn())
    seen: list[str] = []
    monkeypatch.setattr(poller.httpx, "AsyncClient", _fake_http(seen))

    assert _run(poller._fetch_asset_path(3)).read_bytes() == b"bytes"
    assert seen and seen[0].startswith(f"{s3_storage.endpoint}/{b}/characters/a.png?")


# Server-side fetchers must sign internally; URLs that reach a browser (307
# redirects, JSON fields) must not, or the browser gets http://minio:9000.
_SERVER_FETCHES = [
    ve.ensure_animation_local, ve.ensure_asset_local, ve.ensure_still_local, ve.ensure_font_local,
    th1._resolve_asset_path, th1._resolve_card_path, th2._resolve_asset_path, th2._resolve_card_path,
    th1_poller._fetch_asset_path, th2_poller._fetch_asset_path, pa.get_asset_preview,
]
_BROWSER_URLS = [
    ve.resolve_source_serving, pa.get_asset_file, pa.get_asset,
    pa.upload_simple, pa.create_from_post, pa.create_from_frame,
]


def test_internal_origin_only_at_server_side_fetches():
    for fn in _SERVER_FETCHES:
        src = inspect.getsource(fn)
        assert src.count("url_for(") == src.count("internal=True") == 1, fn.__qualname__
    for fn in _BROWSER_URLS:
        assert "internal=True" not in inspect.getsource(fn), fn.__qualname__


# ---------------------------------------------------------------------------
# Talking Head save-to-assets keys (A4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod,prefix", [
    (th1, "video_talking_head/"), (th2, "video_talking_head_v2/"),
], ids=["th", "th_v2"])
@pytest.mark.parametrize("name,slug", [
    ("Ramadan صحة 2026", "ramadan_2026"),
    ("صحة المرأة", "talking_head"),
])
def test_save_to_assets_keys_are_ascii(monkeypatch, tmp_path, mod, prefix, name, slug):
    comp = tmp_path / "final.mp4"
    comp.write_bytes(b"mp4")

    async def _run_row(run_id):
        return {"id": run_id, "topic": "topic", "metadata": {
            "composition_status": mod.COMP_READY, "composition_path": "x/final.mp4",
            "script": {"dialogue": "hi"},
        }}

    uploads = []

    async def _upload(**kwargs):
        uploads.append(kwargs)
        return {"storage": "local", "storage_bucket": None, "storage_path": None,
                "local_path": "data/assets_library/x.mp4"}

    async def _embed(texts):
        raise RuntimeError("offline")

    monkeypatch.setattr(mod, "_fetch_run_row", _run_row)
    monkeypatch.setattr(mod, "_abs_from_rel", lambda rel: comp)
    monkeypatch.setattr(asset_storage, "upload", _upload)
    monkeypatch.setattr(mod, "gemini_embed", _embed)
    db = _FakeDB()
    db.results.append(("INSERT INTO producer_assets", {"id": 77}))
    monkeypatch.setattr(mod, "get_conn", db.get_conn())

    assert _run(mod.save_to_assets(run_id=3, display_name=name))["id"] == 77
    assert re.fullmatch(re.escape(prefix + slug) + r"_[0-9a-f]{6}\.mp4", uploads[0]["path"])


# ---------------------------------------------------------------------------
# Composition backups: written on export, removed on delete
# ---------------------------------------------------------------------------

def test_ve_export_backup_is_recorded_on_s3(monkeypatch, tmp_path, s3_storage):
    out = tmp_path / "final.mp4"
    out.write_bytes(b"mp4")
    patches = []

    async def _patch(project_id, patch):
        patches.append(patch)

    monkeypatch.setattr(ve, "_abs_from_rel", lambda rel: out)
    monkeypatch.setattr(ve, "_patch_render", _patch)
    _run(ve_poller._mark_ready(12, "yas_producer/video-editor/12/final.mp4"))
    b, key = config.producer_assets_bucket, "yas/compositions/video-editor/12.mp4"
    assert patches[0]["composition_storage_bucket"] == b
    assert patches[0]["composition_storage_path"] == key
    assert s3_storage.client.head_object(Bucket=b, Key=key)["ContentType"] == "video/mp4"


def test_ve_export_on_local_disk_writes_no_second_copy(monkeypatch, tmp_path, assets_root):
    out = tmp_path / "final.mp4"
    out.write_bytes(b"mp4")
    patches = []

    async def _patch(project_id, patch):
        patches.append(patch)

    monkeypatch.setattr(ve, "_abs_from_rel", lambda rel: out)
    monkeypatch.setattr(ve, "_patch_render", _patch)
    _run(ve_poller._mark_ready(12, "yas_producer/video-editor/12/final.mp4"))
    assert patches == [{"composition_status": ve.COMP_READY,
                        "composition_path": "yas_producer/video-editor/12/final.mp4"}]
    assert not assets_root.exists() or not any(assets_root.rglob("*.mp4"))


def test_ve_delete_project_removes_the_bucket_backup(monkeypatch, tmp_path, s3_storage):
    b, key = config.producer_assets_bucket, "yas/compositions/video-editor/12.mp4"
    s3_storage.client.put_object(Bucket=b, Key=key, Body=b"mp4")
    monkeypatch.setattr(ve, "VE_ROOT", tmp_path / "ve")

    async def _fetch_row(project_id):
        return {"id": project_id, "metadata": {"video_edit": {"render": {
            "composition_storage_bucket": b, "composition_storage_path": key,
        }}}}

    monkeypatch.setattr(ve, "_fetch_row", _fetch_row)
    db = _FakeDB()
    db.results.append(("DELETE FROM yas_generations", {"id": 12}))
    monkeypatch.setattr(ve, "get_conn", db.get_conn())

    assert _run(ve.delete_project(12)) == {"deleted": 1, "id": 12}
    assert _keys(s3_storage.client, b) == []


def test_th_v2_delete_run_removes_the_composition_backup(
    monkeypatch, tmp_path, s3_storage, assets_root,
):
    b, key = config.producer_assets_bucket, "yas/compositions/talking-head/4.mp4"
    s3_storage.client.put_object(Bucket=b, Key=key, Body=b"mp4")
    rel = "yas_producer/talking-head-v2/4/final.mp4"
    local = assets_root.parent / rel
    local.parent.mkdir(parents=True)
    local.write_bytes(b"mp4")
    monkeypatch.setattr(th2, "TH_ROOT", tmp_path / "th2")

    async def _fetch_run_row(run_id):
        return {"id": run_id, "storage_bucket": None, "storage_path": None, "local_path": None,
                "metadata": {"composition_path": rel, "composition_storage_bucket": b,
                             "composition_storage_path": key}}

    monkeypatch.setattr(th2, "_fetch_run_row", _fetch_run_row)
    db = _FakeDB()
    db.results.append(("DELETE FROM yas_generations WHERE id", {"id": 4}))
    monkeypatch.setattr(th2, "get_conn", db.get_conn())

    assert _run(th2.delete_run(4)) == {"deleted": 1, "id": 4}
    assert _keys(s3_storage.client, b) == []
    assert not local.exists()
