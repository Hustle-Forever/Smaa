"""Producer character references resolve to local files, never URLs (A13).

generate_video keeps a reference only if `Path(ref).exists()`, so the signed
URL generate_script used to store for a bucket-only character was dropped
without a word and the character never reached Veo. Every new character is
bucket-only once the object store is on, so the stored copy is now cached
under producer_root/refs_cache and that absolute path is stored instead.
Offline: fake cursor, download_bytes monkeypatched.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.producer.phases import generate_script as gs
from app.config import config
from app.services import asset_storage


def _run(coro):
    return asyncio.run(coro)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    async def execute(self, _sql, params=None):
        self.params = params

    async def fetchall(self):
        return list(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def db(monkeypatch):
    """Swap the producer_assets query for canned rows: `db.rows = [...]`."""
    cur = _Cursor([])

    class _Conn:
        def cursor(self):
            return cur

    @asynccontextmanager
    async def _get_conn(*_a, **_k):
        yield _Conn()

    monkeypatch.setattr(gs, "get_conn", _get_conn)
    return cur


@pytest.fixture
def producer_root(tmp_path):
    saved = config.producer_root
    root = tmp_path / "producer"
    object.__setattr__(config, "producer_root", root)
    try:
        yield root
    finally:
        object.__setattr__(config, "producer_root", saved)


@pytest.fixture
def downloads(monkeypatch):
    """download_bytes returns `downloads.data[key]` (or raises it); `calls` logs."""
    d = SimpleNamespace(data={}, calls=[])

    async def _download(*, storage_bucket, storage_path):
        d.calls.append((storage_bucket, storage_path))
        v = d.data.get(storage_path, asset_storage.AssetStorageError("NoSuchKey"))
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(asset_storage, "download_bytes", _download)
    return d


def _scenes(*ids):
    return [{"subject": " and ".join(f"#{i} someone" for i in ids)}]


def _row(aid, *, local=None, bucket=None, key=None, mime="image/png"):
    return {"id": aid, "local_path": local, "storage_bucket": bucket,
            "storage_path": key, "mime_type": mime, "slug": f"c{aid}"}


def test_no_tags_no_query(db):
    assert _run(gs._resolve_character_refs([{"subject": "a campus at dusk"}])) == ([], [])
    assert db.params is None


def test_a_local_character_uses_its_own_file(db, producer_root, downloads, tmp_path):
    f = tmp_path / "char7.png"
    f.write_bytes(b"png")
    db.rows = [_row(7, local=str(f), bucket="producer-assets", key="characters/c7.png")]
    paths, ids = _run(gs._resolve_character_refs(_scenes(7)))
    assert paths == [str(f)] and ids == [7]
    assert downloads.calls == []


def test_a_bucket_only_character_is_cached_as_a_local_file(db, producer_root, downloads):
    db.rows = [_row(8, bucket="producer-assets", key="characters/c8.png")]
    downloads.data["characters/c8.png"] = b"\x89PNG-bytes"
    paths, ids = _run(gs._resolve_character_refs(_scenes(8)))

    expected = producer_root / "refs_cache" / "asset-8.png"
    assert paths == [str(expected)] and ids == [8]
    assert Path(paths[0]).is_absolute() and not paths[0].startswith("http")
    assert expected.read_bytes() == b"\x89PNG-bytes"
    assert not list(expected.parent.glob("*.part"))

    # The next script reuses the cached file instead of downloading again.
    assert _run(gs._resolve_character_refs(_scenes(8)))[0] == [str(expected)]
    assert downloads.calls == [("producer-assets", "characters/c8.png")]


def test_a_missing_local_file_falls_back_to_the_stored_copy(db, producer_root, downloads, tmp_path):
    db.rows = [_row(9, local=str(tmp_path / "gone.jpg"), bucket="producer-assets",
                    key="characters/c9", mime="image/jpeg")]
    downloads.data["characters/c9"] = b"jpg"
    paths, ids = _run(gs._resolve_character_refs(_scenes(9)))
    # No extension on the key: the cached file takes one from the mime type.
    assert paths == [str(producer_root / "refs_cache" / "asset-9.jpg")] and ids == [9]


def test_an_unfetchable_character_is_skipped_not_stored_as_a_url(
    db, producer_root, downloads, monkeypatch,
):
    async def _no_urls(**_k):
        raise AssertionError("character refs must never be signed URLs")

    monkeypatch.setattr(asset_storage, "url_for", _no_urls)
    db.rows = [
        _row(10, bucket="producer-assets", key="characters/c10.png"),  # download fails
        _row(11),                                                       # nowhere at all
    ]
    assert _run(gs._resolve_character_refs(_scenes(10, 11))) == ([], [])
    assert not (producer_root / "refs_cache" / "asset-10.png").exists()


def test_an_empty_object_is_not_cached(db, producer_root, downloads):
    db.rows = [_row(12, bucket="producer-assets", key="characters/c12.png")]
    downloads.data["characters/c12.png"] = b""
    assert _run(gs._resolve_character_refs(_scenes(12))) == ([], [])
    assert not (producer_root / "refs_cache" / "asset-12.png").exists()


def test_mixed_rows_keep_their_order_and_ids(db, producer_root, downloads, tmp_path):
    f = tmp_path / "c1.png"
    f.write_bytes(b"x")
    db.rows = [
        _row(1, local=str(f)),
        _row(2, bucket="producer-assets", key="characters/c2.webp", mime="image/webp"),
        _row(3, bucket="producer-assets", key="characters/c3.png"),
    ]
    downloads.data["characters/c2.webp"] = b"webp"
    paths, ids = _run(gs._resolve_character_refs(_scenes(1, 2, 3)))
    assert ids == [1, 2]
    assert paths == [str(f), str(producer_root / "refs_cache" / "asset-2.webp")]
    assert db.params == ([1, 2, 3],)
