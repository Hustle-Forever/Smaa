"""Tests for services/thumbs.py: the stored 480px WebP grid derivatives.

On S3 these derivatives are the only small tiles (MinIO has no image
transformer), so every bucket image writer calls ensure_thumb_object.
Offline: moto for the S3 path, monkeypatched uploads for Supabase.
"""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.config import config
from app.services import asset_storage, thumbs


def _run(coro):
    return asyncio.run(coro)


def _image_bytes(size=(1000, 500), mode="RGBA", fmt="PNG", exif=None) -> bytes:
    im = Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else None)
    out = BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    im.save(out, format=fmt, **kwargs)
    return out.getvalue()


def test_thumb_object_path():
    assert thumbs.thumb_object_path("manual/a.png") == "thumbs/manual/a.png.webp"


def test_make_thumb_bytes_fits_the_box_and_is_webp():
    out = thumbs.make_thumb_bytes(_image_bytes((1000, 500)))
    with Image.open(BytesIO(out)) as im:
        assert im.format == "WEBP"
        assert im.size == (480, 240)
    # Small images are not upscaled; palette sources convert cleanly.
    with Image.open(BytesIO(thumbs.make_thumb_bytes(_image_bytes((100, 80), mode="P")))) as im:
        assert im.size == (100, 80)


def test_make_thumb_bytes_applies_exif_orientation():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 on display
    raw = _image_bytes((200, 100), mode="RGB", fmt="JPEG", exif=exif)
    with Image.open(BytesIO(thumbs.make_thumb_bytes(raw))) as im:
        assert im.size == (100, 200)


def test_make_thumb_bytes_rejects_non_images():
    assert thumbs.make_thumb_bytes(b"not an image") is None
    assert thumbs.make_thumb_bytes(b"") is None


def test_ensure_thumb_object_skips_what_it_cannot_thumbnail(monkeypatch):
    async def _no_upload(**kwargs):
        raise AssertionError("nothing should be uploaded")

    monkeypatch.setattr(asset_storage, "upload", _no_upload)
    raw = _image_bytes()
    assert _run(thumbs.ensure_thumb_object(
        raw=raw, storage_path=None, storage_bucket=None, mime="image/png")) is None
    for mime in ("image/svg+xml", "image/gif", "video/mp4", None):
        assert _run(thumbs.ensure_thumb_object(
            raw=raw, storage_path="a.x", storage_bucket="producer-assets", mime=mime)) is None
    assert _run(thumbs.ensure_thumb_object(
        raw=b"garbage", storage_path="a.png", storage_bucket="producer-assets",
        mime="image/png")) is None


def test_ensure_thumb_object_on_s3(s3_storage):
    b = config.producer_assets_bucket
    key = "thumbs/manual/pic.png.webp"
    s3_storage.client.put_object(Bucket=b, Key=key, Body=b"old", ContentType="image/webp")
    assert _run(asset_storage.url_for(storage_bucket=b, storage_path=key, local_path=None))
    assert any(k[1] == key for k in asset_storage._SIGNED_URL_CACHE)

    got = _run(thumbs.ensure_thumb_object(
        raw=_image_bytes(), storage_path="manual/pic.png", storage_bucket=b, mime="image/png"))
    assert got == key
    head = s3_storage.client.head_object(Bucket=b, Key=key)
    assert head["ContentType"] == "image/webp"
    assert head["CacheControl"] == asset_storage.CACHE_IMMUTABLE
    assert head["ContentLength"] > 3
    # The overwrite evicted the old signed URL (A2).
    assert not any(k[1] == key for k in asset_storage._SIGNED_URL_CACHE)


def test_ensure_thumb_object_s3_failure_is_none_and_writes_nothing_locally(monkeypatch, s3_config, tmp_path):
    async def _fail(*args, **kwargs):
        raise asset_storage.AssetStorageError("down")

    root = tmp_path / "data" / "assets_library"
    old = config.producer_assets_root
    object.__setattr__(config, "producer_assets_root", root)
    try:
        monkeypatch.setattr(asset_storage, "put_s3_object", _fail)
        assert _run(thumbs.ensure_thumb_object(
            raw=_image_bytes(), storage_path="a.png", storage_bucket="producer-assets",
            mime="image/png")) is None
        assert not root.exists() or not any(p.is_file() for p in root.rglob("*"))
    finally:
        object.__setattr__(config, "producer_assets_root", old)


def test_ensure_thumb_object_on_supabase(monkeypatch):
    object.__setattr__(config, "supabase_service_role_key", "k")  # backend -> supabase
    seen = {}

    async def _upload(*, data, path, mime, bucket):
        seen.update(path=path, mime=mime, bucket=bucket)
        return {"storage": "supabase", "storage_bucket": bucket, "storage_path": path,
                "local_path": None}

    monkeypatch.setattr(asset_storage, "upload", _upload)
    got = _run(thumbs.ensure_thumb_object(
        raw=_image_bytes(), storage_path="manual/a.png", storage_bucket="producer-assets",
        mime="image/png"))
    assert got == "thumbs/manual/a.png.webp"
    assert seen == {"path": got, "mime": "image/webp", "bucket": "producer-assets"}


def test_ensure_thumb_object_removes_a_stray_local_copy(monkeypatch, tmp_path):
    object.__setattr__(config, "supabase_service_role_key", "k")
    stray = tmp_path / "stray.webp"
    stray.write_bytes(b"x")

    async def _upload(**kwargs):
        return {"storage": "local", "storage_bucket": None, "storage_path": None,
                "local_path": str(stray)}

    monkeypatch.setattr(asset_storage, "upload", _upload)
    assert _run(thumbs.ensure_thumb_object(
        raw=_image_bytes(), storage_path="a.png", storage_bucket="producer-assets",
        mime="image/png")) is None
    assert not Path(stray).exists()


# --- lazy derivatives (thumbs.derivative_urls) on S3 ---------------------------

def _drain():
    """Let the queued background generations finish (they run on the loop)."""
    async def _wait():
        while thumbs._tasks:
            await asyncio.gather(*list(thumbs._tasks))
    return _wait()


def _put_original(s3, key, size=(1600, 900)):
    s3.client.put_object(
        Bucket=config.producer_media_bucket, Key=key,
        Body=_image_bytes(size, mode="RGB"), ContentType="image/png",
    )


def test_derivative_keys_match_what_thumbs_writes():
    assert asset_storage.derivative_keys("poster/7.png") == [
        thumbs.thumb_object_path("poster/7.png"), thumbs.preview_object_path("poster/7.png"),
    ]


def test_a_missing_derivative_is_queued_then_served(s3_storage):
    bucket = config.producer_media_bucket
    _put_original(s3_storage, "poster/7.png")

    async def _go():
        first = await thumbs.derivative_urls(bucket, ["poster/7.png"], "thumb")
        await _drain()
        second = await thumbs.derivative_urls(bucket, ["poster/7.png"], "thumb")
        return first, second

    first, second = _run(_go())
    assert first == {}  # the caller serves the original this time
    url = second["poster/7.png"]
    assert url.startswith(f"{s3_storage.public_base}/{bucket}/thumbs/poster/7.png.webp?")
    body = s3_storage.client.get_object(Bucket=bucket, Key="thumbs/poster/7.png.webp")
    assert body["ContentType"] == "image/webp"
    with Image.open(BytesIO(body["Body"].read())) as im:
        assert max(im.size) == thumbs.THUMB_BOX


def test_preview_kind_is_1400px(s3_storage):
    bucket = config.producer_media_bucket
    _put_original(s3_storage, "bg/big.jpg", size=(4000, 2000))

    async def _go():
        await thumbs.derivative_urls(bucket, ["bg/big.jpg"], "preview")
        await _drain()
        return await thumbs.derivative_urls(bucket, ["bg/big.jpg"], "preview")

    assert "bg/big.jpg" in _run(_go())
    body = s3_storage.client.get_object(Bucket=bucket, Key="previews/bg/big.jpg.webp")["Body"].read()
    with Image.open(BytesIO(body)) as im:
        assert im.size == (thumbs.PREVIEW_BOX, 700)


def test_non_raster_keys_get_no_derivative(s3_storage):
    bucket = config.producer_media_bucket
    s3_storage.client.put_object(Bucket=bucket, Key="animation/1.mp4", Body=b"mp4")

    async def _go():
        out = await thumbs.derivative_urls(bucket, ["animation/1.mp4", "x.svg"], "thumb")
        await _drain()
        return out

    assert _run(_go()) == {}
    keys = [o["Key"] for o in s3_storage.client.list_objects_v2(Bucket=bucket).get("Contents", [])]
    assert keys == ["animation/1.mp4"]


def test_rewriting_or_deleting_an_original_drops_its_derivatives(s3_storage):
    bucket = config.producer_media_bucket

    async def _go():
        await asset_storage.upload(data=_image_bytes(mode="RGB"), path="th/main.png",
                                   mime="image/png", bucket=bucket)
        for kind in ("thumb", "preview"):
            assert await thumbs.ensure_derivative(bucket, "th/main.png", kind)
        # Regenerated in place: the old copies must go.
        await asset_storage.upload(data=_image_bytes((640, 640), mode="RGB"), path="th/main.png",
                                   mime="image/png", bucket=bucket)
        after_rewrite = sorted(
            o["Key"] for o in s3_storage.client.list_objects_v2(Bucket=bucket)["Contents"])
        assert not asset_storage.derivative_known(bucket, "thumbs/th/main.png.webp")
        await thumbs.ensure_derivative(bucket, "th/main.png", "thumb")
        await asset_storage.delete(storage_bucket=bucket, storage_path="th/main.png", local_path=None)
        return after_rewrite

    assert _run(_go()) == ["th/main.png"]
    assert s3_storage.client.list_objects_v2(Bucket=bucket).get("Contents") is None


def test_derivatives_are_s3_only():
    assert _run(thumbs.derivative_urls("producer-media", ["poster/7.png"])) == {}
    assert _run(thumbs.ensure_derivative("producer-media", "poster/7.png")) is None


def test_warm_script_plans_thumbs_and_previews():
    from scripts import warm_derivatives as wd

    keys = ["a.png", "thumbs/a.png.webp", "previews/a.png.webp", "v.mp4", "doc.pdf", "b.JPG"]
    assert wd.plan(keys, previews=False) == [("a.png", "thumb"), ("b.JPG", "thumb")]
    assert wd.plan(keys, previews=True) == [
        ("a.png", "thumb"), ("a.png", "preview"), ("b.JPG", "thumb"), ("b.JPG", "preview"),
    ]
