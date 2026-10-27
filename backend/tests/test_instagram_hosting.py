"""Instagram media hosting on the object store, offline.

`approved_media` copies a booked asset into the public bucket and returns the
URL Instagram fetches. These tests cover what the S3 (MinIO) move changed:
the hosting gate follows the configured store, byte readers fall back to the
local copy (and to the composition backups), a store that is down is retried
instead of being read as a deleted file, the poster_human fallback reads
the object directly, keys carry 128 random bits, and every path that drops a
hosted copy deletes it (partial carousels, a failed campaign booking, Library
delete). S3 runs against moto (`s3_storage` in conftest); the database is a
fake cursor. Nothing touches the network.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from io import BytesIO

import pytest

from app.config import config
from app.services import approved_media as am
from app.services import asset_storage
from app.services import scheduled_posts as sp


def _run(coro):
    return asyncio.run(coro)


def _png(size=(8, 6)) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGBA", size, (200, 30, 30, 128)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeCursor:
    """Records every statement; answers fetchone/fetchall with `rows`."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.executed: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return list(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_conn(cursor: _FakeCursor):
    class _Conn:
        def cursor(self):
            return cursor

    @asynccontextmanager
    async def get_conn(*_a, **_k):
        yield _Conn()

    return get_conn


def _objects(client, bucket: str) -> list[str]:
    return [o["Key"] for o in client.list_objects_v2(Bucket=bucket).get("Contents", [])]


# --- the hosting gate ---------------------------------------------------------

def test_configured_follows_the_object_store(s3_config):
    assert am.configured() == (True, "")


def test_configured_says_why_without_an_object_store():
    ok, reason = am.configured()
    assert ok is False
    assert reason == asset_storage.public_hosting_configured()[1]
    assert "public URL" in reason


def test_upload_for_post_refuses_without_hosting(monkeypatch):
    async def _never(*_a, **_k):
        raise AssertionError("must not read media when hosting is off")

    monkeypatch.setattr(am, "_resolve_media", _never)
    with pytest.raises(asset_storage.AssetStorageError):
        _run(am.upload_for_post(tool="poster", ref_id=1, media_type="image"))


# --- D15: byte readers fall through to the local copy -------------------------

def _patch_download(monkeypatch, result):
    calls = []

    async def _download(*, storage_bucket, storage_path):
        calls.append((storage_bucket, storage_path))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(asset_storage, "download_bytes", _download)
    return calls


def test_read_media_prefers_the_bucket(monkeypatch, tmp_path):
    local = tmp_path / "x.png"
    local.write_bytes(b"local")
    calls = _patch_download(monkeypatch, b"bucket")
    data = _run(am._read_media_bytes(
        storage_bucket="producer-media", storage_path="poster/1.png", local_path=str(local),
    ))
    assert data == b"bucket"
    assert calls == [("producer-media", "poster/1.png")]


@pytest.mark.parametrize("bucket_result", [
    asset_storage.AssetNotFoundError("S3 get producer-media/poster/1.png: no such object"),
    asset_storage.AssetStorageError("S3 get producer-media/poster/1.png: storage unreachable"),
    b"",
])
def test_read_media_falls_back_to_local_when_the_bucket_fails(monkeypatch, tmp_path, bucket_result):
    local = tmp_path / "x.png"
    local.write_bytes(b"local")
    _patch_download(monkeypatch, bucket_result)
    data = _run(am._read_media_bytes(
        storage_bucket="producer-media", storage_path="poster/1.png", local_path=str(local),
    ))
    assert data == b"local"


@pytest.mark.parametrize("bucket_result", [asset_storage.AssetNotFoundError("gone"), b""])
def test_read_media_is_none_when_neither_copy_exists(monkeypatch, tmp_path, bucket_result):
    _patch_download(monkeypatch, bucket_result)
    assert _run(am._read_media_bytes(
        storage_bucket="producer-media", storage_path="poster/1.png",
        local_path=str(tmp_path / "missing.png"),
    )) is None


@pytest.mark.parametrize("error", [
    asset_storage.AssetStorageError("S3 get producer-media/poster/1.png: storage unreachable"),
    ConnectionError("connection refused"),
])
def test_read_media_raises_when_the_store_is_unreachable(monkeypatch, tmp_path, error):
    """No local copy and a store that did not answer: the media may be fine,
    so this must not read as "gone" (None -> FileNotFoundError)."""
    _patch_download(monkeypatch, error)
    for local_path in (None, str(tmp_path / "missing.png")):
        with pytest.raises(asset_storage.AssetStorageError) as info:
            _run(am._read_media_bytes(
                storage_bucket="producer-media", storage_path="poster/1.png",
                local_path=local_path,
            ))
        assert not isinstance(info.value, asset_storage.AssetNotFoundError)


# --- A23: poster_human reads the stored copy directly --------------------------

def test_poster_human_falls_back_to_download_bytes(monkeypatch, tmp_path):
    import app.db as db

    cur = _FakeCursor([{
        "local_path": str(tmp_path / "gone.png"), "storage_bucket": "producer-assets",
        "storage_path": "yas/generations/5.png", "mime_type": "image/png",
    }])
    monkeypatch.setattr(db, "get_conn", _fake_get_conn(cur))
    calls = _patch_download(monkeypatch, b"stored")

    async def _no_signing(**_k):
        raise AssertionError("poster_human must not mint a URL to fetch its own object")

    monkeypatch.setattr(asset_storage, "url_for", _no_signing)
    data, mime = _run(am._resolve_media("poster_human", 5))
    assert (data, mime) == (b"stored", "image/png")
    assert calls == [("producer-assets", "yas/generations/5.png")]


def test_poster_human_missing_everywhere_is_not_found(monkeypatch, tmp_path):
    import app.db as db

    cur = _FakeCursor([{
        "local_path": None, "storage_bucket": "producer-assets",
        "storage_path": "yas/generations/5.png", "mime_type": None,
    }])
    monkeypatch.setattr(db, "get_conn", _fake_get_conn(cur))
    _patch_download(monkeypatch, asset_storage.AssetNotFoundError("NoSuchKey"))
    with pytest.raises(FileNotFoundError):
        _run(am._resolve_media("poster_human", 5))


def test_poster_human_unreachable_store_is_not_not_found(monkeypatch, tmp_path):
    import app.db as db

    cur = _FakeCursor([{
        "local_path": None, "storage_bucket": "producer-assets",
        "storage_path": "yas/generations/5.png", "mime_type": None,
    }])
    monkeypatch.setattr(db, "get_conn", _fake_get_conn(cur))
    _patch_download(monkeypatch, asset_storage.AssetStorageError("storage unreachable"))
    with pytest.raises(asset_storage.AssetStorageError, match="unreachable"):
        _run(am._resolve_media("poster_human", 5))


# --- composition backups for talking_head_v2 and video_editor ------------------

def _patch_th_v2(monkeypatch, tmp_path, md):
    from app.services import yas_talking_head_v2 as th_v2

    async def _row(_rid):
        return {"metadata": md}

    monkeypatch.setattr(th_v2, "_fetch_run_row", _row)
    monkeypatch.setattr(th_v2, "_abs_from_rel", lambda rel: tmp_path / rel)


def test_th_v2_uses_the_local_composition_first(monkeypatch, tmp_path):
    (tmp_path / "final.mp4").write_bytes(b"local-mp4")
    _patch_th_v2(monkeypatch, tmp_path, {
        "composition_path": "final.mp4",
        "composition_storage_bucket": "producer-assets",
        "composition_storage_path": "yas/compositions/talking-head/3.mp4",
    })
    calls = _patch_download(monkeypatch, b"bucket-mp4")
    assert _run(am._resolve_media("talking_head_v2", 3)) == (b"local-mp4", "video/mp4")
    assert calls == []


def test_th_v2_falls_back_to_the_bucket_backup(monkeypatch, tmp_path):
    _patch_th_v2(monkeypatch, tmp_path, {
        "composition_path": "final.mp4",
        "composition_storage_bucket": "producer-assets",
        "composition_storage_path": "yas/compositions/talking-head/3.mp4",
    })
    calls = _patch_download(monkeypatch, b"bucket-mp4")
    assert _run(am._resolve_media("talking_head_v2", 3)) == (b"bucket-mp4", "video/mp4")
    assert calls == [("producer-assets", "yas/compositions/talking-head/3.mp4")]


def test_th_v2_without_either_copy_is_not_found(monkeypatch, tmp_path):
    _patch_th_v2(monkeypatch, tmp_path, {"composition_path": "final.mp4"})
    calls = _patch_download(monkeypatch, b"never")
    with pytest.raises(FileNotFoundError, match="composition missing on disk"):
        _run(am._resolve_media("talking_head_v2", 3))
    assert calls == []


def test_video_editor_falls_back_to_the_bucket_backup(monkeypatch, tmp_path):
    from app.services import yas_video_editor as ve

    render = {
        "composition_status": ve.COMP_READY,
        "composition_path": "yas_producer/video-editor/9/final.mp4",
        "composition_storage_bucket": "producer-assets",
        "composition_storage_path": "yas/compositions/video-editor/9.mp4",
    }

    async def _row(_pid):
        return {"metadata": {"video_edit": {"render": render}}}

    monkeypatch.setattr(ve, "_fetch_row", _row)
    monkeypatch.setattr(ve, "_video_edit", lambda md: md["video_edit"])
    monkeypatch.setattr(ve, "_abs_from_rel", lambda rel: tmp_path / "gone.mp4")
    _patch_download(monkeypatch, b"ve-mp4")
    assert _run(am._resolve_media("video_editor", 9)) == (b"ve-mp4", "video/mp4")


@pytest.mark.parametrize("error,expected", [
    (asset_storage.AssetNotFoundError("NoSuchKey"), FileNotFoundError),
    (asset_storage.AssetStorageError("storage unreachable"), asset_storage.AssetStorageError),
])
def test_video_editor_backup_missing_or_unreadable(monkeypatch, tmp_path, error, expected):
    from app.services import yas_video_editor as ve

    render = {
        "composition_status": ve.COMP_READY, "composition_path": "final.mp4",
        "composition_storage_bucket": "producer-assets",
        "composition_storage_path": "yas/compositions/video-editor/9.mp4",
    }

    async def _row(_pid):
        return {"metadata": {"video_edit": {"render": render}}}

    monkeypatch.setattr(ve, "_fetch_row", _row)
    monkeypatch.setattr(ve, "_video_edit", lambda md: md["video_edit"])
    monkeypatch.setattr(ve, "_abs_from_rel", lambda rel: tmp_path / "gone.mp4")
    _patch_download(monkeypatch, error)
    with pytest.raises(expected):
        _run(am._resolve_media("video_editor", 9))


def test_broll_posts_its_video_editor_export(monkeypatch, tmp_path):
    """A B-roll is a Video-Editor project booked as tool "broll"; hosting used
    to stop at "unknown tool 'broll'"."""
    from app.services import yas_video_editor as ve

    (tmp_path / "final.mp4").write_bytes(b"broll-mp4")
    render = {"composition_status": ve.COMP_READY, "composition_path": "final.mp4"}

    async def _row(_pid):
        return {"metadata": {"video_edit": {"render": render}}}

    monkeypatch.setattr(ve, "_fetch_row", _row)
    monkeypatch.setattr(ve, "_video_edit", lambda md: md["video_edit"])
    monkeypatch.setattr(ve, "_abs_from_rel", lambda rel: tmp_path / rel)
    assert _run(am._resolve_media("broll", 7)) == (b"broll-mp4", "video/mp4")

    render["composition_status"] = "rendering"
    with pytest.raises(FileNotFoundError, match="broll #7: export not ready"):
        _run(am._resolve_media("broll", 7))


def test_deleting_a_video_editor_project_drops_its_broll_bookings():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath(
        "app", "routers", "yas_producer.py").read_text(encoding="utf-8")
    body = src.split("async def video_editor_delete(", 1)[1].split("\n@router", 1)[0]
    assert '_delete_library_siblings("video_editor"' in body
    assert '_delete_library_siblings("broll"' in body


# --- Instagram's aspect-ratio range (4:5 .. 1.91:1) ---------------------------

def _jpeg_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    img = Image.open(BytesIO(data))
    assert img.format == "JPEG"
    return img.size


def _solid(size, color=(10, 120, 200)) -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize("size", [
    (1080, 1920),   # 9:16 story-shaped poster: too tall
    (900, 1600),
    (1000, 3000),   # 1:3
    (3000, 1000),   # 3:1 banner: too wide
    (2400, 1000),
    (200, 1000),    # tiny and very tall
])
def test_out_of_range_images_are_padded_into_range(size):
    w, h = _jpeg_size(am._to_jpeg(_solid(size)))
    assert am._IG_MIN_RATIO <= w / h <= am._IG_MAX_RATIO, (size, (w, h))
    assert max(w, h) <= am._IG_MAX_EDGE


@pytest.mark.parametrize("size", [(1080, 1350), (1080, 1080), (1910, 1000), (8, 6)])
def test_in_range_images_keep_their_shape(size):
    w, h = _jpeg_size(am._to_jpeg(_solid(size)))
    assert abs(w / h - size[0] / size[1]) < 0.01


def test_padding_keeps_the_whole_poster():
    """Padding, not cropping: the poster's own pixels are all still there, centred,
    and the bars are a dimmed blur rather than the poster colour itself."""
    from PIL import Image

    data = am._to_jpeg(_solid((1080, 1920), (240, 20, 20)))
    w, h = _jpeg_size(data)
    img = Image.open(BytesIO(data)).convert("RGB")
    assert (w, h) == (1152, 1440)
    poster_w = round(1080 * 1440 / 1920)  # 810 after the downscale
    left = (w - poster_w) // 2
    for x in (left + 5, w // 2, left + poster_w - 5):
        r, g, b = img.getpixel((x, h // 2))
        assert r > 200 and g < 60 and b < 60
    r, g, b = img.getpixel((5, h // 2))  # the bar: blurred and dimmed
    assert r < 225


def test_an_explicit_ratio_wins():
    w, h = _jpeg_size(am._to_jpeg(_solid((1080, 1080)), ratio=0.8))
    assert abs(w / h - 0.8) < 0.01
    w, h = _jpeg_size(am._to_jpeg(_solid((1080, 1920)), ratio=1.91))
    assert abs(w / h - 1.91) < 0.01 and max(w, h) <= am._IG_MAX_EDGE


# --- hosting on S3 (moto) -----------------------------------------------------

def test_upload_for_post_hosts_a_jpeg_at_a_permanent_public_url(monkeypatch, s3_storage):
    async def _media(tool, ref_id):
        return _png(), "image/png"

    monkeypatch.setattr(am, "_resolve_media", _media)
    url = _run(am.upload_for_post(tool="poster", ref_id=12, media_type="image"))

    bucket = config.approved_media_bucket
    m = re.fullmatch(
        re.escape(f"{s3_storage.public_base}/{bucket}/poster/12_ig_") + r"([0-9a-f]{32})\.jpg",
        url,
    )
    assert m, url  # 128 random bits: tool and ref_id are guessable
    assert asset_storage.parse_public_url(url) == (bucket, f"poster/12_ig_{m.group(1)}.jpg")

    head = s3_storage.client.head_object(Bucket=bucket, Key=f"poster/12_ig_{m.group(1)}.jpg")
    assert head["ContentType"] == "image/jpeg"
    assert head["CacheControl"] == asset_storage.CACHE_PUBLIC_IMMUTABLE
    # The bucket got its anonymous GetObject-only policy on the way.
    policy = s3_storage.client.get_bucket_policy(Bucket=bucket)["Policy"]
    assert "s3:GetObject" in policy and "s3:ListBucket" not in policy


def test_two_bookings_of_one_asset_never_share_a_key(monkeypatch, s3_storage):
    async def _media(tool, ref_id):
        return b"\x00\x00\x00\x18ftypmp42", "video/mp4"

    monkeypatch.setattr(am, "_resolve_media", _media)
    a = _run(am.upload_for_post(tool="animation", ref_id=4, media_type="video"))
    b = _run(am.upload_for_post(tool="animation", ref_id=4, media_type="video"))
    assert a != b and a.endswith(".mp4") and b.endswith(".mp4")
    assert len(_objects(s3_storage.client, config.approved_media_bucket)) == 2


def test_a_failed_carousel_removes_the_slides_it_already_hosted(monkeypatch, s3_storage):
    async def _media(tool, ref_id):
        if ref_id == 3:
            raise FileNotFoundError("poster #3: not found")
        return _png(), "image/png"

    monkeypatch.setattr(am, "_resolve_media", _media)
    with pytest.raises(FileNotFoundError):
        _run(am.upload_carousel([
            {"tool": "poster", "ref_id": 1, "media_type": "image"},
            {"tool": "poster", "ref_id": 2, "media_type": "image"},
            {"tool": "poster", "ref_id": 3, "media_type": "image"},
        ]))
    assert _objects(s3_storage.client, config.approved_media_bucket) == []


def test_a_hosted_carousel_keeps_every_slide(monkeypatch, s3_storage):
    async def _media(tool, ref_id):
        return _png(), "image/png"

    monkeypatch.setattr(am, "_resolve_media", _media)
    out = _run(am.upload_carousel([
        {"tool": "poster", "ref_id": 1},
        {"tool": "poster", "ref_id": 2, "media_type": "image"},
    ]))
    assert [o["ref_id"] for o in out] == [1, 2]
    assert all(o["media_type"] == "image" for o in out)
    assert len(_objects(s3_storage.client, config.approved_media_bucket)) == 2


def test_carousel_slides_take_the_first_slides_shape(monkeypatch, s3_storage):
    """Instagram crops every slide to the first image's shape, so later slides
    are padded to it instead of losing their edges. A video slide is left alone
    and does not set the shape."""
    sizes = {1: None, 2: (1080, 1350), 3: (1080, 1080), 4: (1080, 1920)}

    async def _media(tool, ref_id):
        if sizes[ref_id] is None:
            return b"\x00\x00\x00\x18ftypmp42", "video/mp4"
        return _solid(sizes[ref_id]), "image/png"

    monkeypatch.setattr(am, "_resolve_media", _media)
    out = _run(am.upload_carousel([
        {"tool": "animation", "ref_id": 1, "media_type": "video"},
        {"tool": "poster", "ref_id": 2, "media_type": "image"},
        {"tool": "poster", "ref_id": 3, "media_type": "image"},
        {"tool": "poster", "ref_id": 4, "media_type": "image"},
    ]))
    bucket = config.approved_media_bucket
    ratios = []
    for o in out[1:]:
        _, key = asset_storage.parse_public_url(o["media_url"])
        body = s3_storage.client.get_object(Bucket=bucket, Key=key)["Body"].read()
        w, h = _jpeg_size(body)
        ratios.append(w / h)
    assert all(abs(r - 0.8) < 0.01 for r in ratios), ratios


# --- A19: Library delete purges the hosted copies ----------------------------

def test_hosted_media_urls_covers_every_slide():
    row = {
        "media_url": "https://x/media/approved-instagram/a.jpg",
        "metadata": {"is_carousel": True, "carousel_items": [
            {"media_url": "https://x/media/approved-instagram/a.jpg"},
            {"media_url": "https://x/media/approved-instagram/b.jpg"},
            {"media_url": None},
            "junk",
        ]},
    }
    assert sp.hosted_media_urls(row) == [
        "https://x/media/approved-instagram/a.jpg",
        "https://x/media/approved-instagram/a.jpg",
        "https://x/media/approved-instagram/b.jpg",
    ]
    assert sp.hosted_media_urls({"media_url": None, "metadata": {}}) == []
    assert sp.hosted_media_urls(None) == []


def _patch_store(monkeypatch, rows):
    cur = _FakeCursor(rows)
    monkeypatch.setattr(sp, "get_conn", _fake_get_conn(cur))
    monkeypatch.setattr(sp, "_ensured", True)
    return cur


def test_delete_by_ref_purges_what_the_deleted_rows_hosted(monkeypatch):
    cur = _patch_store(monkeypatch, [
        {"media_url": "https://x/media/approved-instagram/p/1.jpg", "metadata": {}},
        {"media_url": "https://x/media/approved-instagram/c/1.jpg", "metadata": {
            "carousel_items": [{"media_url": "https://x/media/approved-instagram/c/1.jpg"},
                               {"media_url": "https://x/media/approved-instagram/c/2.jpg"}],
        }},
        {"media_url": None, "metadata": {}},
    ])
    purged = []

    async def _delete(urls):
        purged.append(list(urls))

    monkeypatch.setattr(asset_storage, "delete_public_urls", _delete)
    assert _run(sp.delete_by_ref("poster", 7)) == 3

    sql, params = cur.executed[0]
    assert "DELETE FROM scheduled_instagram_posts" in sql
    assert "status <> 'published'" in sql  # the audit trail stays
    assert "RETURNING" in sql
    assert params == ("poster", 7)
    assert purged == [[
        "https://x/media/approved-instagram/p/1.jpg",
        "https://x/media/approved-instagram/c/1.jpg",
        "https://x/media/approved-instagram/c/1.jpg",
        "https://x/media/approved-instagram/c/2.jpg",
    ]]


def test_delete_by_ref_removes_the_objects_from_the_bucket(monkeypatch, s3_storage):
    bucket = config.approved_media_bucket
    for key in ("poster/7_ig_a.jpg", "poster/7_ig_b.jpg", "poster/8_ig_keep.jpg"):
        s3_storage.client.put_object(Bucket=bucket, Key=key, Body=b"x", ContentType="image/jpeg")
    _patch_store(monkeypatch, [
        {"media_url": asset_storage.public_url(bucket, "poster/7_ig_a.jpg"), "metadata": {}},
        {"media_url": asset_storage.public_url(bucket, "poster/7_ig_b.jpg") + "?v=1",
         "metadata": {}},
    ])
    assert _run(sp.delete_by_ref("poster", 7)) == 2
    assert _objects(s3_storage.client, bucket) == ["poster/8_ig_keep.jpg"]


def test_delete_by_ref_with_nothing_to_delete(monkeypatch):
    _patch_store(monkeypatch, [])
    purged = []

    async def _delete(urls):
        purged.append(list(urls))

    monkeypatch.setattr(asset_storage, "delete_public_urls", _delete)
    assert _run(sp.delete_by_ref("poster", 7)) == 0
    assert purged in ([], [[]])


# --- A19: a campaign booking that fails after hosting drops the copy ------------

def _async(v=None):
    async def _f():
        return v
    return _f()


def _book_one(monkeypatch, *, create, mark=None, upload=None):
    from app.services import campaign_store as cs
    from app.workers import campaign_poller as cp

    updates, purged = [], []
    monkeypatch.setattr(cp.store, "items_by_status", lambda s, **k: _async([{
        "id": 9, "campaign_id": 1, "gen_id": 77, "gen_kind": "poster", "caption": "hi",
        "proposed_time": "2026-08-03T17:00:00+04:00",
    }]) if s == cs.ITEM_APPROVED else _async([]))
    monkeypatch.setattr(am, "upload_for_post",
                        upload or (lambda **k: _async("https://x/media/approved-instagram/p.jpg")))
    monkeypatch.setattr(sp, "create", create)
    monkeypatch.setattr(cp.store, "mark_scheduled", mark or (lambda i, p: _async({"id": i})))
    monkeypatch.setattr(cp.store, "update_item",
                        lambda iid, **k: updates.append(k) or _async({}))

    async def _delete(urls):
        purged.append(list(urls))

    monkeypatch.setattr(asset_storage, "delete_public_urls", _delete)
    _run(cp._book_approved())
    return updates, purged


def test_a_failed_booking_deletes_the_copy_it_just_hosted(monkeypatch):
    async def _create(**_kw):
        raise RuntimeError("db blip")

    updates, purged = _book_one(monkeypatch, create=_create)
    assert purged == [["https://x/media/approved-instagram/p.jpg"]]
    assert updates and "status" not in updates[0], "must stay 'approved' and retry"
    assert "schedule" in updates[0]["error"]


def test_a_booking_that_exists_keeps_its_media(monkeypatch):
    """Once the scheduled row exists it points at the media, so a later failure
    must not pull the file out from under it."""
    async def _create(**_kw):
        return {"id": 555}

    async def _mark(_i, _p):
        raise RuntimeError("db blip")

    updates, purged = _book_one(monkeypatch, create=_create, mark=_mark)
    assert purged == []
    assert "schedule" in updates[0]["error"]


def test_a_hosting_failure_has_nothing_to_delete(monkeypatch):
    async def _flaky(**_k):
        raise RuntimeError("503 from storage")

    async def _create(**_kw):
        raise AssertionError("must not book without media")

    updates, purged = _book_one(monkeypatch, create=_create, upload=_flaky)
    assert purged == []
    assert "host" in updates[0]["error"]


# --- a store that is down is retried; only a missing object fails the item ------

def _book_real_poster(monkeypatch):
    """Run _book_approved with the REAL approved_media (S3 on moto): one
    approved poster whose only copy is the bucket object poster/77.png."""
    from app.services import campaign_store as cs
    from app.services import yas_local_store as ystore
    from app.workers import campaign_poller as cp

    updates, created = [], []
    monkeypatch.setattr(cp.store, "items_by_status", lambda s, **k: _async([{
        "id": 9, "campaign_id": 1, "gen_id": 77, "gen_kind": "poster", "caption": "hi",
        "proposed_time": "2026-08-03T17:00:00+04:00",
    }]) if s == cs.ITEM_APPROVED else _async([]))
    monkeypatch.setattr(ystore, "get", lambda ref_id: _async({
        "id": ref_id, "storage_bucket": config.producer_media_bucket,
        "storage_path": "poster/77.png", "local_path": None, "mime_type": "image/png",
    }))

    async def _create(**kw):
        created.append(kw)
        return {"id": 555}

    monkeypatch.setattr(sp, "create", _create)
    monkeypatch.setattr(cp.store, "mark_scheduled", lambda i, p: _async({"id": i}))
    monkeypatch.setattr(cp.store, "update_item",
                        lambda iid, **k: updates.append(k) or _async({}))
    _run(cp._book_approved())
    return updates, created


def test_an_unreachable_store_keeps_the_item_approved(monkeypatch, s3_storage):
    """A MinIO restart opens the breaker: the poster is intact, so the item
    must be retried on the next tick, never failed as "deleted"."""
    from app.services import s3_backend

    s3_storage.client.put_object(Bucket=config.producer_media_bucket,
                                 Key="poster/77.png", Body=_png())
    s3_backend._trip_breaker(RuntimeError("minio restarting"))
    updates, created = _book_real_poster(monkeypatch)
    assert created == []
    assert len(updates) == 1 and "status" not in updates[0], "must stay 'approved' and retry"
    assert updates[0]["error"].startswith("couldn't host the media yet")

    # The store answers again: the next tick hosts and books it.
    s3_backend.reset()
    updates, created = _book_real_poster(monkeypatch)
    assert updates == []
    assert len(created) == 1
    assert created[0]["media_url"].startswith(
        asset_storage.public_url(config.approved_media_bucket, "poster/"))


def test_a_missing_object_still_fails_the_item(monkeypatch, s3_storage):
    from app.workers import campaign_poller as cp

    updates, created = _book_real_poster(monkeypatch)
    assert created == []
    assert updates == [{
        "status": cp.store.ITEM_FAILED,
        "error": "the generated media was deleted before it could be scheduled",
    }]
