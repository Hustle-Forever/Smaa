"""Tests for scripts/backfill_local_to_s3.py: the one-off local -> S3 backfill.

Offline: key derivation, path resolution, content sniffing and the plan are
pure (plus a temp data tree); the apply path runs against moto's S3 with a
fake database that records every statement.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import config
from app.services import asset_storage, s3_backend
from app.services.asset_storage import AssetStorageError
from scripts import backfill_local_to_s3 as bf


def _run(coro):
    return asyncio.run(coro)


def _png(size=(900, 600)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (20, 120, 200)).save(out, format="PNG")
    return out.getvalue()


def _jpeg(size=(640, 480)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (200, 20, 20)).save(out, format="JPEG")
    return out.getvalue()


MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64


def _triple(name: str) -> bf.Triple:
    return next(t for t in bf.TRIPLES if t.name == name)


@pytest.fixture
def roots(tmp_path):
    """A throwaway backend/ tree: backend/data/assets_library is the upload
    fallback root, backend/data the other local-path base."""
    saved = {n: getattr(config, n) for n in ("producer_assets_root", "asset_root")}
    backend = tmp_path / "backend"
    data = backend / "data"
    lib = data / "assets_library"
    lib.mkdir(parents=True)
    object.__setattr__(config, "producer_assets_root", lib)
    object.__setattr__(config, "asset_root", data / "assets")

    def put(rel: str, blob: bytes, base: Path = data) -> Path:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
        return p

    try:
        yield SimpleNamespace(tmp=tmp_path, backend=backend, data=data, lib=lib, put=put)
    finally:
        for name, value in saved.items():
            object.__setattr__(config, name, value)


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# Which buckets, which triples
# ---------------------------------------------------------------------------

def test_every_triple_goes_to_its_writers_bucket():
    buckets = {t.name: t.bucket_setting for t in bf.TRIPLES}
    assert buckets == {
        "producer_assets": "producer_assets_bucket",
        "yas_generations": "producer_assets_bucket",
        "yas_animations": "producer_assets_bucket",
        "yas_poster_generations": "producer_media_bucket",
        "yas_animation_generations.frame": "producer_media_bucket",
        "yas_animation_generations.video": "producer_media_bucket",
        "yas_general_video_generations.frame": "producer_media_bucket",
        "yas_general_video_generations.video": "producer_media_bucket",
    }
    assert _triple("producer_assets").bucket == config.producer_assets_bucket
    assert _triple("yas_poster_generations").bucket == config.producer_media_bucket
    frame = _triple("yas_general_video_generations.frame")
    assert (frame.bucket_col, frame.key_col, frame.local_col, frame.media) == (
        "frame_storage_bucket", "frame_storage_path", "frame_local_path", "frame")
    assert {t.media for t in bf.TRIPLES if t.name.endswith(".video")} == {"video"}
    assert _triple("yas_animations").media == "video"


def test_select_triples():
    assert len(bf.select_triples(None)) == 8
    assert [t.name for t in bf.select_triples("yas_animation_generations")] == [
        "yas_animation_generations.frame", "yas_animation_generations.video"]
    assert [t.name for t in bf.select_triples("yas_animation_generations.video")] == [
        "yas_animation_generations.video"]
    with pytest.raises(ValueError):
        bf.select_triples("brand_documents")


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("local_path, key", [
    ("data/assets_library/manual/bg_01.jpg", "manual/bg_01.jpg"),
    ("data/assets_library/poster/12.png", "poster/12.png"),
    ("data/assets_library/animation/3-frame.png", "animation/3-frame.png"),
    ("data/assets_library/general-video/4.mp4", "general-video/4.mp4"),
    ("data/assets_library/yas/animations/9.mp4", "yas/animations/9.mp4"),
    ("assets_library/poster/13.png", "poster/13.png"),                    # data-relative
    ("data\\assets_library\\animation\\5-frame.png", "animation/5-frame.png"),
])
def test_library_paths_lose_the_fallback_prefix(local_path, key):
    assert bf.derive_key(local_path) == (key, "assets_library")


@pytest.mark.parametrize("local_path, key", [
    ("yas_producer/generations/12.png", "yas/generations/12.png"),
    ("yas_producer/talking-head-v2/7/main.png", "yas/talking-head-v2/7/main.png"),
    ("yas_producer/talking-head-v2/7/intro-40.png", "yas/talking-head-v2/7/intro-40.png"),
    ("yas_producer/talking-head/3/scene-1-55.png", "yas/talking-head/3/scene-1-55.png"),
    ("data/yas_producer/generations/13.jpg", "yas/generations/13.jpg"),
])
def test_yas_producer_paths_become_the_writer_keys(local_path, key):
    assert bf.derive_key(local_path) == (key, "yas_producer")


def test_absolute_paths_map_through_either_base(roots):
    anim = roots.data / "yas_producer" / "animations" / "5.mp4"
    assert bf.derive_key(str(anim)) == ("yas/animations/5.mp4", "yas_producer")
    lib = roots.lib / "manual" / "a.png"
    assert bf.derive_key(str(lib)) == ("manual/a.png", "assets_library")
    assert bf.derive_key(str(roots.tmp / "elsewhere" / "x.png")) == ("", "outside")
    assert bf.derive_key("/srv/other/x.png") == ("", "outside")


def test_other_paths_keep_their_data_relative_form():
    assert bf.derive_key("uploads/1d50c0ae.mp4") == ("uploads/1d50c0ae.mp4", "as-is")
    assert bf.derive_key("./data/frames/3/1.jpg") == ("frames/3/1.jpg", "as-is")


def test_clean_key_keeps_valid_keys_and_sanitises_the_rest():
    assert bf.clean_key("manual/bg_01.jpg") == "manual/bg_01.jpg"
    arabic = bf.clean_key("manual/صورة 1.jpg")
    assert arabic != "manual/صورة 1.jpg" and s3_backend.valid_key(arabic)
    assert arabic.startswith("manual/") and arabic.endswith("_1.jpg")
    assert bf.clean_key("/a//b/../c+d.png") == "a/b/c_d.png"
    assert bf.clean_key("..") == ""


# ---------------------------------------------------------------------------
# Files on disk
# ---------------------------------------------------------------------------

def test_resolve_local_tries_both_bases(roots):
    a = roots.put("assets_library/manual/a.jpg", b"x")                # backend/-relative row
    g = roots.put("yas_producer/generations/1.png", b"x")             # data-relative row
    assert bf.resolve_local("data/assets_library/manual/a.jpg") == a.resolve()
    assert bf.resolve_local("yas_producer/generations/1.png") == g.resolve()
    assert bf.resolve_local("data\\assets_library\\manual\\a.jpg") == a.resolve()
    assert bf.resolve_local(str(a)) == a.resolve()
    assert bf.resolve_local("data/assets_library/manual/gone.jpg") is None


def test_resolve_preview_finds_the_upload_fallback_copy(roots):
    p = roots.put("fonts/_preview/vg5000.png", _png((10, 10)), base=roots.lib)
    assert bf.resolve_preview("fonts/_preview/vg5000.png") == p.resolve()
    assert bf.resolve_preview("fonts/_preview/missing.png") is None


# ---------------------------------------------------------------------------
# Content-Type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("head, ctype", [
    (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n\x00\x00", "image/png"),
    (b"GIF89a\x01\x00", "image/gif"),
    (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"RIFF\x24\x00\x00\x00WAVEfmt ", "audio/wav"),
    (b"%PDF-1.7\n", "application/pdf"),
    (MP4, "video/mp4"),
    (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video/quicktime"),
    (b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00", "audio/mp4"),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81", "video/webm"),
    (b"ID3\x04\x00\x00\x00", "audio/mpeg"),
    (b"OggS\x00\x02", "audio/ogg"),
    (b"\x00\x01\x00\x00\x00\x10\x01\x00", "font/ttf"),
    (b"OTTO\x00\x0b\x00\x80", "font/otf"),
    (b"wOF2\x00\x01\x00\x00", "font/woff2"),
    (b'\xef\xbb\xbf<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg">', "image/svg+xml"),
    (b"<svg viewBox='0 0 1 1'></svg>", "image/svg+xml"),
    (b"just some text", None),
    (b"", None),
])
def test_sniff_content_type(head, ctype):
    assert bf.sniff_content_type(head) == ctype


def test_frame_rows_never_take_the_rows_mime():
    frame = _triple("yas_animation_generations.frame")
    # Frame rows carry the finished video's mime_type (video/mp4).
    assert bf.content_type_for(frame, _png((4, 4))[:64], "3-frame.png", "video/mp4") == "image/png"
    assert bf.content_type_for(frame, b"????", "3-frame.png", "video/mp4") == "image/png"
    assert bf.content_type_for(frame, b"????", "3-frame.bin", "video/mp4") == "application/octet-stream"


def test_video_rows_are_always_mp4():
    video = _triple("yas_general_video_generations.video")
    assert bf.content_type_for(video, b"????", "4.bin", None) == "video/mp4"
    assert bf.content_type_for(_triple("yas_animations"), MP4, "9.mp4", "image/png") == "video/mp4"


def test_file_rows_prefer_the_bytes_then_the_row_then_the_extension():
    pa = _triple("producer_assets")
    # The bytes win over a mislabelled row.
    assert bf.content_type_for(pa, _jpeg()[:64], "a.png", "image/png") == "image/jpeg"
    assert bf.content_type_for(pa, b"????", "a.bin", "audio/x-custom") == "audio/x-custom"
    # A vague row type says nothing, so the extension decides.
    assert bf.content_type_for(pa, b"????", "a.mp3", "application/octet-stream") == "audio/mpeg"
    assert bf.content_type_for(pa, b"????", "a.unknown", None) == "application/octet-stream"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def _pa_row(id_, local_path, *, kind="image", mime="image/png", attrs=None):
    return {"id": id_, "local_path": local_path, "mime_type": mime,
            "asset_kind": kind, "attributes": attrs if attrs is not None else {}}


def test_plan_row_image_gets_a_thumbnail_and_the_file_type(roots):
    roots.put("assets_library/manual/bg.png", _png())
    item = bf.plan_row(_triple("producer_assets"), _pa_row(
        7, "data/assets_library/manual/bg.png", mime="image/jpeg",
        attrs={"_thumb_path": "thumbs/manual/old-supabase-key.jpg.webp"},
    ))
    assert not item.problem
    assert item.key == "manual/bg.png" and item.rule == "assets_library" and not item.sanitised
    assert item.content_type == "image/png"
    assert item.thumb is True
    assert item.size == (roots.lib / "manual" / "bg.png").stat().st_size


def test_plan_row_non_images_get_no_thumbnail(roots):
    roots.put("assets_library/audios/a.mp4", MP4)
    item = bf.plan_row(_triple("producer_assets"), _pa_row(
        8, "data/assets_library/audios/a.mp4", kind="audio", mime="video/mp4"))
    assert item.content_type == "video/mp4" and item.thumb is False
    # Image kind but not a raster type the thumbnailer handles.
    roots.put("assets_library/manual/logo.svg", b"<svg viewBox='0 0 1 1'></svg>")
    svg = bf.plan_row(_triple("producer_assets"), _pa_row(
        9, "data/assets_library/manual/logo.svg", mime="image/svg+xml"))
    assert svg.content_type == "image/svg+xml" and svg.thumb is False
    # Only producer_assets rows have thumbnails at all.
    roots.put("assets_library/poster/3.png", _png())
    poster = bf.plan_row(_triple("yas_poster_generations"), {
        "id": 3, "local_path": "data/assets_library/poster/3.png", "mime_type": "image/png"})
    assert poster.thumb is False and poster.key == "poster/3.png"


def test_plan_row_font_preview(roots):
    roots.put("assets_library/fonts/vg.ttf", b"\x00\x01\x00\x00" + b"\x00" * 60)
    roots.put("fonts/_preview/vg_ab12.png", _png((20, 10)), base=roots.lib)
    item = bf.plan_row(_triple("producer_assets"), _pa_row(
        11, "data/assets_library/fonts/vg.ttf", kind="font", mime="font/ttf",
        attrs={"_preview_path": "fonts/_preview/vg_ab12.png"}))
    assert item.content_type == "font/ttf" and item.thumb is False
    assert item.preview_key == "fonts/_preview/vg_ab12.png"
    assert item.preview_abs == (roots.lib / "fonts/_preview/vg_ab12.png").resolve()


def test_plan_rows_left_alone(roots):
    roots.put("assets_library/manual/a.png", _png())
    outside = roots.tmp / "elsewhere" / "x.png"
    outside.parent.mkdir()
    outside.write_bytes(_png())
    pa = _triple("producer_assets")
    assert bf.plan_row(pa, _pa_row(1, "data/assets_library/manual/gone.png")).problem == "missing"
    assert bf.plan_row(pa, _pa_row(2, str(outside))).problem == "outside"
    # A '..' that climbs out of the tree is refused even when the file exists.
    assert bf.plan_row(pa, _pa_row(3, "data/assets_library/../../../elsewhere/x.png")).problem == "outside"


def test_mark_conflicts_only_between_different_files(roots):
    roots.put("assets_library/manual/a b.png", _png((10, 10)))
    roots.put("assets_library/manual/a_b.png", _png((12, 12)))
    roots.put("assets_library/manual/shared.png", _png((14, 14)))
    pa = _triple("producer_assets")
    plan = bf.plan_table(pa, [
        _pa_row(1, "data/assets_library/manual/a_b.png"),
        _pa_row(2, "data/assets_library/manual/a b.png"),        # sanitises onto row 1's key
        _pa_row(3, "data/assets_library/manual/shared.png"),
        _pa_row(4, "assets_library/manual/shared.png"),          # same file, other base
    ])
    bf.mark_conflicts([plan])
    problems = {i.row_id: i.problem for i in plan.items}
    assert problems == {1: "", 2: "conflict", 3: "", 4: ""}


def test_format_plan_reports_totals_sanitised_keys_and_missing_files(roots):
    roots.put("assets_library/manual/bg.png", _png())
    roots.put("assets_library/manual/صورة.png", _png((30, 30)))
    roots.put("assets_library/animation/3-frame.png", _png((40, 40)))
    pa = bf.plan_table(_triple("producer_assets"), [
        _pa_row(1, "data/assets_library/manual/bg.png"),
        _pa_row(2, "data/assets_library/manual/صورة.png"),
        _pa_row(3, "data/assets_library/manual/gone.jpg", mime="image/jpeg"),
        _pa_row(4, "data/assets_library/fonts/x.ttf", kind="font"),
    ], in_bucket=5, no_local=1, candidates=9)
    frames = bf.plan_table(_triple("yas_animation_generations.frame"), [
        {"id": 3, "local_path": "data/assets_library/animation/3-frame.png", "mime_type": "video/mp4"},
    ])
    bf.mark_conflicts([pa, frames])
    text = bf.format_plan([pa, frames], limit=4)
    assert "producer_assets -> producer-assets" in text
    assert "upload 2 row(s)" in text and "2 thumbnail(s)" in text
    assert "(--limit 4: 4 of 9 rows looked at)" in text
    assert "not copied, file missing on disk: 2" in text
    assert "already in a bucket: 5; no local file recorded: 1" in text
    assert "keys sanitised:" in text and "id 2: 'manual/صورة.png' -> 'manual/____.png'" in text
    assert "id 3 (file missing on disk): data/assets_library/manual/gone.jpg" in text
    assert "yas_animation_generations.frame -> producer-media" in text
    assert "content types: image/png 1" in text
    assert "TOTAL: 3 row(s) to upload" in text and "2 row(s) left alone" in text
    assert "5 already in a bucket" in text
    assert "no writer layout matched" not in text


def test_format_plan_lists_keys_no_writer_layout_produced(roots):
    roots.put("brand/logo.png", _png((8, 8)))
    plan = bf.plan_table(_triple("producer_assets"), [_pa_row(1, "brand/logo.png")])
    text = bf.format_plan([plan])
    assert "keys kept as the data path (no writer layout matched):" in text
    assert "id 1: brand/logo.png -> brand/logo.png" in text


# ---------------------------------------------------------------------------
# The UPDATE
# ---------------------------------------------------------------------------

def test_build_update_is_conditional_and_keeps_local_path():
    item = bf.Item(triple=_triple("producer_assets"), row_id=7,
                   local_path="data/assets_library/manual/bg.png", key="manual/bg.png")
    sql, params = bf.build_update(item, {"_thumb_path": "thumbs/manual/bg.png.webp"}, [])
    assert sql.startswith("UPDATE producer_assets SET storage_bucket = %s, storage_path = %s, ")
    assert "attributes = (attributes - %s::text[]) || %s::jsonb" in sql
    assert sql.endswith("WHERE id = %s AND storage_bucket IS NULL AND local_path = %s")
    assert "local_path = %s," not in sql and "updated_at" not in sql
    bucket, key, drop, patch, row_id, local = params
    assert (bucket, key, drop, row_id, local) == (
        config.producer_assets_bucket, "manual/bg.png", [], 7, "data/assets_library/manual/bg.png")
    assert patch.obj == {"_thumb_path": "thumbs/manual/bg.png.webp"}

    frame = bf.Item(triple=_triple("yas_animation_generations.frame"), row_id=3,
                    local_path="data/assets_library/animation/3-frame.png", key="animation/3-frame.png")
    sql, params = bf.build_update(frame, {}, [])
    assert sql == (
        "UPDATE yas_animation_generations SET frame_storage_bucket = %s, frame_storage_path = %s"
        " WHERE id = %s AND frame_storage_bucket IS NULL AND frame_local_path = %s"
    )
    assert params == (config.producer_media_bucket, "animation/3-frame.png", 3,
                      "data/assets_library/animation/3-frame.png")


# ---------------------------------------------------------------------------
# Applying (moto S3 + a fake database)
# ---------------------------------------------------------------------------

class FakeDB:
    """Just enough of app.db.get_conn for the script: the SELECTs read
    `tables` (rows with every column), UPDATEs are recorded."""

    def __init__(self, tables: dict[str, list[dict]] | None = None, rowcount: int = 1):
        self.tables = tables or {}
        self.rowcount = rowcount
        self.executed: list[tuple[str, tuple]] = []
        self.opened = self.closed = 0

    @property
    def updates(self):
        return [(s, p) for s, p in self.executed if s.startswith("UPDATE")]

    @asynccontextmanager
    async def get_conn(self, *, autocommit: bool = False):
        yield _FakeConn(self)

    async def open_pool(self):
        self.opened += 1

    async def close_pool(self):
        self.closed += 1


class _FakeConn:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def cursor(self):
        yield _FakeCursor(self.db)


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1
        self._rows: list[dict] = []

    async def execute(self, sql, params=()):
        self.db.executed.append((sql, params))
        if sql.startswith("UPDATE"):
            self.rowcount = self.db.rowcount
            return
        triple = next(t for t in bf.TRIPLES
                      if f"FROM {t.table}" in sql and t.local_col in sql and t.bucket_col in sql)
        rows = self.db.tables.get(triple.table, [])
        free = [r for r in rows if not r.get(triple.bucket_col)]
        with_local = [r for r in free if r.get(triple.local_col)]
        if "count(*)" in sql:
            self._rows = [{
                "in_bucket": len(rows) - len(free),
                "no_local": len(free) - len(with_local),
                "candidates": len(with_local),
            }]
            return
        out = []
        for r in sorted(with_local, key=lambda r: r["id"]):
            row = {"id": r["id"], "local_path": r[triple.local_col], "mime_type": r.get("mime_type")}
            if triple.table == "producer_assets":
                row["asset_kind"] = r.get("asset_kind")
                row["attributes"] = r.get("attributes") or {}
            out.append(row)
        self._rows = out[: params[0]] if params else out

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(bf, "get_conn", db.get_conn)
    monkeypatch.setattr(bf, "open_pool", db.open_pool)
    monkeypatch.setattr(bf, "close_pool", db.close_pool)
    return db


def _head(s3, bucket, key):
    return s3.client.head_object(Bucket=bucket, Key=key)


def test_apply_uploads_thumbnails_and_updates_each_row(s3_storage, roots, fake_db):
    roots.put("assets_library/manual/bg.png", _png())
    roots.put("assets_library/animation/3-frame.png", _png((64, 64)))
    roots.put("assets_library/animation/3.mp4", MP4)
    roots.put("yas_producer/generations/12.png", _png((50, 50)))
    before = _files(roots.backend)
    plans = [
        bf.plan_table(_triple("producer_assets"), [_pa_row(
            7, "data/assets_library/manual/bg.png",
            attrs={"_thumb_path": "thumbs/stale-supabase-key.webp", "tone": "warm"})]),
        bf.plan_table(_triple("yas_generations"), [
            {"id": 12, "local_path": "yas_producer/generations/12.png", "mime_type": "image/png"}]),
        bf.plan_table(_triple("yas_animation_generations.frame"), [
            {"id": 3, "local_path": "data/assets_library/animation/3-frame.png", "mime_type": "video/mp4"}]),
        bf.plan_table(_triple("yas_animation_generations.video"), [
            {"id": 3, "local_path": "data/assets_library/animation/3.mp4", "mime_type": "video/mp4"}]),
    ]
    bf.mark_conflicts(plans)
    summary = _run(bf.apply_plans(plans))
    assert (summary.updated, summary.failed, summary.changed, summary.thumbs) == (4, 0, 0, 1)

    assets, media = config.producer_assets_bucket, config.producer_media_bucket
    obj = _head(s3_storage, assets, "manual/bg.png")
    assert obj["ContentType"] == "image/png"
    assert obj["CacheControl"] == asset_storage.CACHE_IMMUTABLE
    thumb = s3_storage.client.get_object(Bucket=assets, Key="thumbs/manual/bg.png.webp")
    assert thumb["ContentType"] == "image/webp"
    with Image.open(BytesIO(thumb["Body"].read())) as im:
        assert im.format == "WEBP" and max(im.size) == 480
    assert _head(s3_storage, assets, "yas/generations/12.png")["ContentType"] == "image/png"
    assert _head(s3_storage, media, "animation/3-frame.png")["ContentType"] == "image/png"
    assert _head(s3_storage, media, "animation/3.mp4")["ContentType"] == "video/mp4"

    updates = fake_db.updates
    assert len(updates) == 4
    sql, params = updates[0]
    assert params[:3] == (assets, "manual/bg.png", [])
    assert params[3].obj == {"_thumb_path": "thumbs/manual/bg.png.webp"}
    assert params[4:] == (7, "data/assets_library/manual/bg.png")
    assert updates[1][1] == (assets, "yas/generations/12.png", 12, "yas_producer/generations/12.png")
    assert updates[2][1][:2] == (media, "animation/3-frame.png")
    assert updates[3][1][:2] == (media, "animation/3.mp4")
    # Nothing was written to (or removed from) local disk.
    assert _files(roots.backend) == before


def test_a_failed_thumbnail_drops_the_stale_pointer(s3_storage, roots, fake_db, monkeypatch):
    roots.put("assets_library/manual/bg.png", _png())
    monkeypatch.setattr(bf.thumbs, "make_thumb_bytes", lambda raw: None)
    plans = [bf.plan_table(_triple("producer_assets"), [_pa_row(
        7, "data/assets_library/manual/bg.png", attrs={"_thumb_path": "thumbs/stale.webp"})])]
    summary = _run(bf.apply_plans(plans))
    assert (summary.updated, summary.thumbs, summary.notes) == (1, 0, 1)
    params = fake_db.updates[0][1]
    assert params[2] == ["_thumb_path"] and params[3].obj == {}
    listed = s3_storage.client.list_objects_v2(Bucket=config.producer_assets_bucket)
    assert [o["Key"] for o in listed.get("Contents", [])] == ["manual/bg.png"]


def test_font_preview_is_uploaded_under_a_clean_key(s3_storage, roots, fake_db):
    roots.put("assets_library/fonts/vg.ttf", b"\x00\x01\x00\x00" + b"\x00" * 60)
    roots.put("fonts/_preview/vg 1.png", _png((20, 10)), base=roots.lib)
    plans = [bf.plan_table(_triple("producer_assets"), [_pa_row(
        11, "data/assets_library/fonts/vg.ttf", kind="font", mime="font/ttf",
        attrs={"_preview_path": "fonts/_preview/vg 1.png", "_thumb_path": "thumbs/x.webp"})])]
    summary = _run(bf.apply_plans(plans))
    assert summary.updated == 1 and summary.notes == 0
    assets = config.producer_assets_bucket
    assert _head(s3_storage, assets, "fonts/vg.ttf")["ContentType"] == "font/ttf"
    assert _head(s3_storage, assets, "fonts/_preview/vg_1.png")["ContentType"] == "image/png"
    params = fake_db.updates[0][1]
    assert params[2] == ["_thumb_path"]
    assert params[3].obj == {"_preview_path": "fonts/_preview/vg_1.png"}


def test_a_failed_upload_leaves_the_row_and_the_disk_alone(s3_config, roots, fake_db, monkeypatch):
    roots.put("assets_library/manual/bg.png", _png())
    roots.put("assets_library/poster/3.png", _png())
    before = _files(roots.backend)
    calls = []

    async def _boom(bucket, key, **kw):
        calls.append(key)
        if key == "manual/bg.png":
            raise AssetStorageError("S3 put producer-assets/manual/bg.png failed: 500")
        return key

    monkeypatch.setattr(asset_storage, "put_s3_object", _boom)
    plans = [
        bf.plan_table(_triple("producer_assets"), [_pa_row(7, "data/assets_library/manual/bg.png")]),
        bf.plan_table(_triple("yas_poster_generations"), [
            {"id": 3, "local_path": "data/assets_library/poster/3.png", "mime_type": "image/png"}]),
    ]
    summary = _run(bf.apply_plans(plans))
    assert (summary.failed, summary.updated, summary.stopped) == (1, 1, False)
    # The failed row got no UPDATE and no thumbnail; the next row still ran.
    assert calls == ["manual/bg.png", "poster/3.png"]
    assert [p[2] for _, p in fake_db.updates] == [3]
    assert _files(roots.backend) == before


def test_an_unreachable_store_stops_the_run(s3_config, roots, fake_db, monkeypatch):
    roots.put("assets_library/poster/1.png", _png())
    roots.put("assets_library/poster/2.png", _png())
    calls = []

    async def _down(bucket, key, **kw):
        calls.append(key)
        raise AssetStorageError("endpoint unreachable")

    monkeypatch.setattr(asset_storage, "put_s3_object", _down)
    monkeypatch.setattr(s3_backend, "breaker_open", lambda: True)
    plans = [bf.plan_table(_triple("yas_poster_generations"), [
        {"id": 1, "local_path": "data/assets_library/poster/1.png", "mime_type": "image/png"},
        {"id": 2, "local_path": "data/assets_library/poster/2.png", "mime_type": "image/png"},
    ])]
    summary = _run(bf.apply_plans(plans))
    assert summary.stopped and summary.failed == 1 and calls == ["poster/1.png"]
    assert fake_db.updates == []


def test_a_row_changed_meanwhile_is_reported_not_retried(s3_storage, roots, fake_db):
    roots.put("assets_library/poster/1.png", _png())
    fake_db.rowcount = 0
    plans = [bf.plan_table(_triple("yas_poster_generations"), [
        {"id": 1, "local_path": "data/assets_library/poster/1.png", "mime_type": "image/png"}])]
    summary = _run(bf.apply_plans(plans))
    assert (summary.changed, summary.updated, summary.failed) == (1, 0, 0)
    assert len(fake_db.updates) == 1


def test_rows_sharing_one_file_upload_it_once(s3_storage, roots, fake_db, monkeypatch):
    roots.put("assets_library/poster/1.png", _png())
    calls = []
    real = asset_storage.put_s3_object

    async def _count(bucket, key, **kw):
        calls.append(key)
        return await real(bucket, key, **kw)

    monkeypatch.setattr(asset_storage, "put_s3_object", _count)
    plans = [bf.plan_table(_triple("yas_poster_generations"), [
        {"id": 1, "local_path": "data/assets_library/poster/1.png", "mime_type": "image/png"},
        {"id": 2, "local_path": "assets_library/poster/1.png", "mime_type": "image/png"},
    ])]
    bf.mark_conflicts(plans)
    summary = _run(bf.apply_plans(plans))
    assert summary.updated == 2 and calls == ["poster/1.png"]


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def test_apply_refuses_without_the_s3_backend(fake_db, capsys):
    assert asset_storage.backend_name() == "local"
    assert _run(bf.run(["--apply"])) == 2
    assert "needs the S3 backend" in capsys.readouterr().out
    assert fake_db.opened == 0 and fake_db.executed == []


def test_dry_run_reads_and_prints_but_writes_nothing(roots, fake_db, capsys, monkeypatch):
    roots.put("assets_library/manual/bg.png", _png())
    roots.put("yas_producer/generations/12.png", _png())
    fake_db.tables = {
        "producer_assets": [
            {"id": 1, "storage_bucket": None, "local_path": "data/assets_library/manual/bg.png",
             "mime_type": "image/png", "asset_kind": "image", "attributes": {}},
            {"id": 2, "storage_bucket": "producer-assets", "local_path": "x", "mime_type": "image/png"},
        ],
        "yas_generations": [
            {"id": 12, "storage_bucket": None, "local_path": "yas_producer/generations/12.png",
             "mime_type": "image/png"},
            {"id": 798, "storage_bucket": None, "local_path": None, "mime_type": None},
        ],
    }

    async def _no_writes(*a, **kw):
        raise AssertionError("a dry run must not write")

    monkeypatch.setattr(asset_storage, "put_s3_object", _no_writes)
    assert _run(bf.run([])) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "storage backend: local" in out
    assert "producer_assets -> producer-assets" in out
    assert "yas_generations -> producer-assets" in out
    assert "already in a bucket: 1; no local file recorded: 0" in out
    assert "already in a bucket: 0; no local file recorded: 1" in out
    assert "TOTAL: 2 row(s) to upload" in out
    assert "Re-run with --apply" in out
    assert fake_db.updates == [] and (fake_db.opened, fake_db.closed) == (1, 1)


def test_apply_command_copies_one_table(s3_storage, roots, fake_db, capsys):
    roots.put("assets_library/manual/bg.png", _png())
    roots.put("assets_library/poster/3.png", _png())
    fake_db.tables = {
        "producer_assets": [
            {"id": 1, "storage_bucket": None, "local_path": "data/assets_library/manual/bg.png",
             "mime_type": "image/png", "asset_kind": "image", "attributes": {}},
            {"id": 2, "storage_bucket": None, "local_path": "data/assets_library/manual/gone.png",
             "mime_type": "image/png", "asset_kind": "image", "attributes": {}},
        ],
        "yas_poster_generations": [
            {"id": 3, "storage_bucket": None, "local_path": "data/assets_library/poster/3.png",
             "mime_type": "image/png"},
        ],
    }
    assert _run(bf.run(["--apply", "--table", "producer_assets"])) == 0
    out = capsys.readouterr().out
    assert "ok producer_assets id 1 -> producer-assets/manual/bg.png" in out
    assert "id 2 (file missing on disk)" in out
    assert "Done: 1 row(s) updated, 1 thumbnail(s), 0 changed meanwhile, 0 failed" in out
    assert [p[4] for _, p in fake_db.updates] == [1]
    # --table kept the poster table out of it.
    assert "yas_poster_generations" not in out
    listed = s3_storage.client.list_objects_v2(Bucket=config.producer_media_bucket)
    assert listed.get("KeyCount", 0) == 0


def test_limit_caps_rows_per_triple(roots, fake_db, capsys):
    for i in (1, 2, 3):
        roots.put(f"assets_library/poster/{i}.png", _png((8, 8)))
    fake_db.tables = {"yas_poster_generations": [
        {"id": i, "storage_bucket": None, "local_path": f"data/assets_library/poster/{i}.png",
         "mime_type": "image/png"} for i in (1, 2, 3)]}
    assert _run(bf.run(["--table", "yas_poster_generations", "--limit", "2"])) == 0
    out = capsys.readouterr().out
    assert "upload 2 row(s)" in out and "(--limit 2: 2 of 3 rows looked at)" in out
    select = next(p for s, p in fake_db.executed if "ORDER BY id LIMIT %s" in s)
    assert select == (2,)


def test_limit_must_be_positive():
    with pytest.raises(SystemExit):
        bf.parse_args(["--limit", "0"])
    with pytest.raises(SystemExit):
        bf.parse_args(["--table", "brand_documents"])


def test_the_documented_rollback_undoes_every_triple():
    """The previous commit reads the bucket first and never falls back to
    local_path, so DEPLOY-AWS.md's rollback must reset every column pair the
    backfill fills, and only on rows that still have their local copy."""
    import re

    doc = (Path(__file__).resolve().parents[2] / "docs" / "DEPLOY-AWS.md").read_text(encoding="utf-8")
    section = doc.split("## Rolling back", 1)[1].split("\n## ", 1)[0]
    sql = re.search(r"<<'SQL'\n(.*?)\nSQL\n", section, re.S).group(1)
    statements = [" ".join(s.split()) for s in sql.split(";") if "UPDATE" in s]
    expected = [
        f"UPDATE {t.table} SET {t.bucket_col} = NULL, {t.key_col} = NULL "
        f"WHERE {t.bucket_col} IS NOT NULL AND {t.local_col} IS NOT NULL AND {t.local_col} <> ''"
        for t in bf.TRIPLES
    ]
    assert statements == expected
    assert sql.lstrip().startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")
    # The undo comes before the old code starts.
    assert section.index("<<'SQL'") < section.index("check out the previous commit")
