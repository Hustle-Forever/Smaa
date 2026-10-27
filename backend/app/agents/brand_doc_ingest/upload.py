"""Upload-side helpers: write incoming files to disk + INSERT brand_documents.

Imported by routers/agents_brand_doc.py. Kept separate from agent.py so the
agent can be re-run on already-uploaded rows without re-running the upload step.
"""
from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Iterable

from fastapi import UploadFile
from psycopg.types.json import Jsonb

from ...config import config
from ...db import get_conn
from ...services import asset_storage
from ...services.storage import ensure_dir, relativise

log = logging.getLogger("archivist.brand_doc.upload")


# brand_documents.doc_kind CHECK constraint:
# ('pdf','docx','text','image','free_text','video').
# Map known file extensions / mime types to the constraint values. Anything not
# in this map is rejected at the API boundary so we never violate the CHECK.
EXT_TO_KIND = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
    ".rtf": "text",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".gif": "image",
    ".bmp": "image",
    ".tiff": "image",
    ".tif": "image",
    ".heic": "image",
    ".heif": "image",
    ".svg": "image",
    ".avif": "image",
    # Brand videos (42 manifesto / brand films). Analysed + embedded by the
    # brand_video_poller, then the raw file is discarded.
    ".mp4": "video",
    ".mov": "video",
    ".m4v": "video",
    ".webm": "video",
    ".avi": "video",
    ".mkv": "video",
}


def classify(filename: str) -> str | None:
    ext = Path(filename or "").suffix.lower()
    return EXT_TO_KIND.get(ext)


async def save_file(up: UploadFile, *, force_local: bool = False) -> tuple[str | None, str, int]:
    """Persist the upload to the object store (the brand-docs bucket) so it's
    durable and previewable anywhere, falling back to local disk when there is
    no object store or the upload fails. Returns (storage_bucket, storage_path, bytes).

    • Object store: storage_bucket = the brand-docs bucket, storage_path = the object key.
    • Local fallback: storage_bucket = None, storage_path = a project-relative path.

    `force_local=True` skips the object store entirely. Used for videos, which are
    large and only needed transiently (the brand_video_poller analyses then discards
    them), so there's no point round-tripping them through a bucket.
    """
    mime = guess_mime(Path(up.filename or ""))
    # The name doubles as the object key, so the extension must be plain ASCII
    # ("report.PDF" -> ".pdf"; an Arabic or odd suffix -> one derived from mime).
    name = f"{uuid.uuid4().hex}{asset_storage.safe_ext(up.filename, mime)}"
    chunks: list[bytes] = []
    bytes_written = 0
    while True:
        chunk = await up.read(64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        bytes_written += len(chunk)
    data = b"".join(chunks)

    if asset_storage.remote_enabled() and not force_local:
        try:
            await asset_storage.ensure_bucket(config.brand_docs_bucket, public=False)
        except Exception as e:  # noqa: BLE001 - the bucket usually exists already
            log.warning("brand-docs bucket ensure failed, uploading anyway: %s", e)
        try:
            result = await asset_storage.upload(
                data=data, path=name, mime=mime,
                prefer="auto", bucket=config.brand_docs_bucket,
            )
            if result.get("storage_bucket"):
                # The stored key can differ from `name` (the S3 backstop sanitises).
                return result["storage_bucket"], result["storage_path"], bytes_written
            # upload() fell back to its own local root (and logged why). Brand
            # docs live under upload_root with a data-relative path, so drop
            # that copy and write the usual one below.
            if result.get("local_path"):
                asset_storage.local_to_abs(result["local_path"]).unlink(missing_ok=True)
        except Exception:
            log.exception("brand-doc object-store upload failed; falling back to local disk")

    ensure_dir(config.upload_root)
    dest = config.upload_root / name
    dest.write_bytes(data)
    return None, relativise(dest), bytes_written


async def insert_file_doc(
    *,
    kind: str,
    source_filename: str | None,
    storage_bucket: str | None,
    storage_path: str,
    bytes_: int,
    mime_type: str | None,
    uploader_note: str | None,
) -> int:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO brand_documents
                  (doc_kind, source_filename, bytes, mime_type, storage_bucket, storage_path,
                   uploader_note, upload_status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'uploaded', %s)
                RETURNING id
                """,
                (
                    kind, source_filename, bytes_, mime_type, storage_bucket, storage_path,
                    uploader_note,
                    Jsonb({"upload": {"original_filename": source_filename}}),
                ),
            )
            row = await cur.fetchone()
    return int(row["id"])


async def insert_free_text_doc(text: str, uploader_note: str | None) -> int:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO brand_documents
                  (doc_kind, raw_text, uploader_note, upload_status, metadata)
                VALUES ('free_text', %s, %s, 'uploaded', %s)
                RETURNING id
                """,
                (text, uploader_note, Jsonb({"upload": {"kind": "free_text"}})),
            )
            row = await cur.fetchone()
    return int(row["id"])


def guess_mime(path: Path, default: str | None = None) -> str | None:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or default
