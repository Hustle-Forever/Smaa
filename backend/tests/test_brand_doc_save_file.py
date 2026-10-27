"""Brand-doc `save_file` against the object store, offline (A14, D12).

The row records whatever `save_file` returns, so it must return where the bytes
really went: the bucket key from the upload RESULT, or a data-relative path
under upload_root when the object store is off or the upload fell back. It used
to record the bucket even when upload() had quietly written the file under
data/assets_library, which the ingest extractors then could not find.
"""
from __future__ import annotations

import asyncio
import re
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile

from app.agents.brand_doc_ingest import upload as bd
from app.config import config
from app.services import asset_storage

_NAME = re.compile(r"[0-9a-f]{32}(\.[a-z0-9]{1,5})?")


def _run(coro):
    return asyncio.run(coro)


def _upload(filename: str, data: bytes = b"%PDF-1.4 brand book") -> UploadFile:
    return UploadFile(file=BytesIO(data), filename=filename)


def _files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


@pytest.fixture
def roots(tmp_path):
    """Temp upload_root and local-fallback root (config is frozen)."""
    saved = (config.upload_root, config.producer_assets_root)
    uploads = tmp_path / "data" / "uploads"
    library = tmp_path / "data" / "assets_library"
    object.__setattr__(config, "upload_root", uploads)
    object.__setattr__(config, "producer_assets_root", library)
    try:
        yield uploads, library
    finally:
        object.__setattr__(config, "upload_root", saved[0])
        object.__setattr__(config, "producer_assets_root", saved[1])


def test_stored_in_the_bucket_under_an_ascii_key(s3_storage, roots):
    uploads, library = roots
    bucket, key, n = _run(bd.save_file(_upload("تقرير الهوية.PDF")))
    assert bucket == config.brand_docs_bucket
    assert _NAME.fullmatch(key) and key.endswith(".pdf")
    assert n == len(b"%PDF-1.4 brand book")
    head = s3_storage.client.head_object(Bucket=bucket, Key=key)
    assert head["ContentType"] == "application/pdf"
    assert _files_under(uploads) == [] and _files_under(library) == []


def test_a_failed_upload_stays_local_under_upload_root(monkeypatch, s3_config, roots):
    uploads, library = roots
    from app.services import s3_backend

    async def _down(*_a, **_k):
        raise asset_storage.AssetStorageError("S3 unreachable")

    monkeypatch.setattr(s3_backend, "create_bucket", _down)
    monkeypatch.setattr(s3_backend, "put_bytes", _down)

    bucket, path, n = _run(bd.save_file(_upload("guide.pdf")))
    assert bucket is None
    stored = uploads / Path(path).name
    assert stored.read_bytes() == b"%PDF-1.4 brand book"
    assert stored.suffix == ".pdf"
    # upload()'s own fallback copy under the assets library is gone.
    assert _files_under(library) == []
    assert _files_under(uploads) == [stored]


def test_local_mode_never_calls_the_object_store(monkeypatch, roots):
    uploads, _library = roots

    async def _never(*_a, **_k):
        raise AssertionError("no object store is configured")

    monkeypatch.setattr(asset_storage, "upload", _never)
    monkeypatch.setattr(asset_storage, "ensure_bucket", _never)
    bucket, path, _n = _run(bd.save_file(_upload("notes.md", b"# notes")))
    assert bucket is None
    assert (uploads / Path(path).name).read_bytes() == b"# notes"


def test_force_local_skips_the_bucket_for_videos(monkeypatch, s3_config, roots):
    uploads, _library = roots

    async def _never(*_a, **_k):
        raise AssertionError("videos stay local")

    monkeypatch.setattr(asset_storage, "upload", _never)
    bucket, path, _n = _run(bd.save_file(_upload("film.mp4", b"mp4"), force_local=True))
    assert bucket is None and path.endswith(".mp4")
    assert _files_under(uploads)


def test_a_bucket_ensure_failure_still_tries_the_upload(monkeypatch, s3_storage, roots):
    async def _denied(*_a, **_k):
        raise asset_storage.AssetStorageError("AccessDenied")

    monkeypatch.setattr(asset_storage, "ensure_bucket", _denied)
    bucket, key, _n = _run(bd.save_file(_upload("logo.png", b"\x89PNG")))
    assert bucket == config.brand_docs_bucket
    assert s3_storage.client.head_object(Bucket=bucket, Key=key)["ContentType"] == "image/png"


def test_the_key_the_store_kept_is_what_the_row_records(monkeypatch, s3_config, roots):
    """In S3 mode upload() may sanitise a key; the row must keep the result's."""
    async def _upload_result(**kw):
        return {"storage": "s3", "storage_bucket": kw["bucket"], "storage_path": "sanitised.pdf",
                "local_path": None, "bytes": len(kw["data"]), "mime_type": kw["mime"]}

    async def _ok(*_a, **_k):
        return None

    monkeypatch.setattr(asset_storage, "ensure_bucket", _ok)
    monkeypatch.setattr(asset_storage, "upload", _upload_result)
    assert _run(bd.save_file(_upload("a.pdf")))[:2] == (config.brand_docs_bucket, "sanitised.pdf")


@pytest.mark.parametrize("filename,ext", [
    ("Brand Book.PDF", ".pdf"),
    ("photo.jpeg", ".jpeg"),
    ("صورة.png", ".png"),
    ("readme.markdown", None),  # too long for a key extension: derived or dropped
])
def test_the_name_is_always_a_plain_ascii_key(roots, filename, ext):
    uploads, _library = roots
    _bucket, path, _n = _run(bd.save_file(_upload(filename)))
    name = Path(path).name
    assert _NAME.fullmatch(name), name
    if ext:
        assert name.endswith(ext)
