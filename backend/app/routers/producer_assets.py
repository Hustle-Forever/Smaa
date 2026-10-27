"""/api/agents/producer/assets/* — Assets Library (v3).

Manual upload surface for characters / brand images / reference videos / fonts.
Each upload is analysed inline (images, fonts) or queued (videos), embedded
for retrieval, and saved to the object store (S3/MinIO or Supabase Storage,
with local-disk fallback).
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import secrets
import subprocess
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from ..config import config
from ..db import get_conn
from ..services import asset_storage
from ..services.asset_analyzer import (
    describe_audio,
    describe_background_image,
    describe_character_image,
    describe_font,
    describe_generic_image,
)
from ..services import asset_frame_extractor as afe
from ..services.drive_folder import (
    DriveFile,
    DriveFolderError,
    FOLDER_MIME,
    build_file_view_url,
    extract_drive_folder_id,
    is_drive_folder_url,
    list_drive_folder,
)
from ..services import thumbs as _thumbs
from ..services.storage import first_existing
# The stored 480px derivatives live in services/thumbs.py so every image writer
# shares them; the old private names stay as aliases.
from ..services.thumbs import (
    THUMB_BOX as _THUMB_BOX,
    THUMB_PREFIX as _THUMB_PREFIX,
    THUMB_WEBP_QUALITY as _THUMB_WEBP_QUALITY,
    ensure_thumb_object,
    make_thumb_bytes as _make_thumb_bytes,
    thumb_object_path as _thumb_object_path,
)
from ..services.url_downloader import UrlDownloadError, download_from_url
from ..services.vertex_client import gemini_embed

log = logging.getLogger("archivist.router.producer_assets")

router = APIRouter(prefix="/api/agents/producer/assets", tags=["agents:producer:assets"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Asset files are immutable per id (a new upload = a new id), so the browser
# can safely cache thumbnails/previews. Avoids re-downloading every <img> on
# each grid mount/scroll — the main "assets load slow" complaint.
_ASSET_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}
# How long the asset file route signs its Supabase target for.
_ASSET_SIGN_TTL = 604800  # 7 days
# Applied to the 307 signed-URL redirect. The asset BYTES are immutable per id,
# but the REDIRECT TARGET is not — it carries a signature that dies in
# _ASSET_SIGN_TTL. Caching the 307 for a year with `immutable` (which forbids
# revalidation) meant that ~8 days after a user first viewed an asset the cached
# redirect replayed a dead signature -> 400 -> broken tile, for the remaining
# ~357 days. Cap freshness safely below the signature lifetime, no `immutable`.
_ASSET_REDIRECT_CACHE_HEADERS = {
    "Cache-Control": f"private, max-age={min(3600, _ASSET_SIGN_TTL - 600)}"
}

ALLOWED_KINDS = {"image", "video", "font", "audio", "other"}
# Known/suggested categories. Categories are now USER-EXTENSIBLE — the DB has no
# CHECK on `category`, so we accept any short slug in addition to these (see
# _clean_category). These remain the curated defaults the UI surfaces.
ALLOWED_CATEGORIES = {"character", "logo", "mood", "free", "sticker", None}
KNOWN_CATEGORIES = ALLOWED_CATEGORIES  # alias; kept for readability
ALLOWED_ARCHETYPES = {
    "emirati_male", "emirati_female", "casual_male", "casual_female",
}

IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic"}
VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/x-matroska", "video/webm"}
FONT_MIMES = {
    "font/ttf", "font/otf", "font/woff", "font/woff2",
    "application/font-sfnt", "application/x-font-ttf",
    "application/vnd.ms-opentype",
    "application/octet-stream",  # many browsers send this for fonts
}
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".flac", ".opus", ".weba"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


def _slugify(name: str, fallback_prefix: str) -> str:
    # ASCII only: the slug becomes the bucket key, and Arabic or spaces in a
    # key break the presigned URL through the /media proxy (A4).
    base = asset_storage.ascii_slug(name, fallback=fallback_prefix)
    return f"{base}_{secrets.token_hex(3)}"


def _ext_from(filename: str | None, mime: str | None) -> str:
    """Extension without the dot, "bin" when none is safe. A raw suffix such as
    ".JPG copy" is not used as-is; it falls back to the mime."""
    return (asset_storage.safe_ext(filename, mime) or ".bin").lstrip(".")


def _image_dims(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None


def _probe_audio_duration(path: Path) -> float | None:
    """Audio duration (sec) by parsing ffmpeg's stderr 'Duration:' line (the
    bundled imageio-ffmpeg binary)."""
    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        proc = subprocess.run([ff, "-i", str(path)], capture_output=True, timeout=60)
        err = (proc.stderr or b"").decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        log.debug("[asset] audio duration probe failed for %s", path)
    return None


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AssetPatchRequest(BaseModel):
    display_name: Optional[str] = None
    ai_description: Optional[str] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


def _clean_category(category: Optional[str]) -> Optional[str]:
    """Normalize + validate a (possibly user-defined) category.

    Returns None for empty/omitted; otherwise a trimmed slug. Categories are
    user-extensible (no DB CHECK), so we accept any short, sane slug — not just
    the curated KNOWN_CATEGORIES — while rejecting absurd input."""
    if category is None:
        return None
    c = category.strip()
    if not c:
        return None
    if not _CATEGORY_RE.match(c):
        raise HTTPException(
            400,
            "category must be 1–40 chars (letters, numbers, spaces, '-' or '_')",
        )
    return c


def _validate_upload_params(
    asset_kind: str,
    category: Optional[str],
    archetype: Optional[str],
) -> None:
    """Shared validation for both /assets and /assets/from-url."""
    if asset_kind not in ALLOWED_KINDS:
        raise HTTPException(400, f"asset_kind must be one of {sorted(ALLOWED_KINDS)}")
    _clean_category(category)  # raises on malformed; free-text categories allowed
    if category == "character":
        if archetype not in ALLOWED_ARCHETYPES:
            raise HTTPException(400, f"archetype required for category=character; one of {sorted(ALLOWED_ARCHETYPES)}")
        if asset_kind != "image":
            raise HTTPException(400, "character references must be images")
    if category == "sticker" and asset_kind != "image":
        raise HTTPException(400, "stickers must be images")


def _infer_kind_from_mime(mime: str | None, filename: str | None) -> str:
    """Pick an asset_kind when the caller didn't specify (URL uploads)."""
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/") or (filename and Path(filename).suffix.lower() in AUDIO_EXTS):
        return "audio"
    if (
        "font" in m
        or (filename and re.search(r"\.(ttf|otf|woff2?|sfnt)$", filename, re.I))
    ):
        return "font"
    return "other"


async def _process_asset_bytes(
    *,
    raw: bytes,
    filename: Optional[str],
    mime: str,
    asset_kind: str,
    category: Optional[str],
    archetype: Optional[str],
    display_name: Optional[str],
    hint: Optional[str],
    source_url: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
) -> dict:
    """Shared pipeline for both file uploads and URL downloads. Performs
    analysis, embedding, storage upload, and DB insert. Returns the row.
    `extra_metadata` is merged into the row's metadata, for provenance a
    caller knows and an upload does not (e.g. the Suno job a track came from).
    """
    # Pick the logical bucket path
    slug = _slugify(display_name or (filename or asset_kind), asset_kind)
    ext = _ext_from(filename, mime)
    if category == "character" and archetype:
        bucket_path = f"characters/{archetype}/{slug}.{ext}"
    else:
        bucket_path = f"{asset_kind}s/{slug}.{ext}"

    # Write to a temp file so analyzers can use a path
    tmp_dir = Path(tempfile.mkdtemp(prefix="asset_upload_"))
    tmp_path = tmp_dir / f"{slug}.{ext}"
    tmp_path.write_bytes(raw)

    # Pre-analysis: dimensions for images
    width = height = None
    duration_sec: float | None = None
    if asset_kind == "image":
        width, height = _image_dims(tmp_path)
    elif asset_kind == "audio":
        duration_sec = _probe_audio_duration(tmp_path)
    elif asset_kind == "video":
        # The ffmpeg "Duration:" probe works for any media container, so reuse
        # it for video — the Assets grid shows a length badge and downstream
        # pickers get the clip length without a full deep-analysis pass.
        duration_sec = _probe_audio_duration(tmp_path)

    # Analyze
    ai_description = ""
    attributes: dict = {}
    preview_bytes: bytes | None = None

    try:
        if asset_kind == "image":
            if category == "character":
                ai_description, attributes = await describe_character_image(
                    tmp_path, archetype=archetype,  # type: ignore[arg-type]
                )
            else:
                # Stickers get a sticker-tuned hint; other images use the plain hint.
                eff_hint = hint
                if category == "sticker":
                    eff_hint = (
                        "This is a STICKER / overlay graphic for a video editor — note "
                        "transparency, art style, and what moments it suits."
                        + (f" Uploader hint: {hint}" if hint else "")
                    )
                ai_description, attributes = await describe_generic_image(
                    tmp_path, hint=eff_hint,
                )
        elif asset_kind == "audio":
            ai_description, attributes = await describe_audio(
                tmp_path, title=display_name or filename,
            )
            if duration_sec:
                attributes = {**attributes, "duration_sec": round(duration_sec, 3)}
        elif asset_kind == "font":
            ai_description, attributes, preview_bytes = describe_font(tmp_path)
        elif asset_kind == "video":
            # Video deep-analysis happens in the background — store a stub
            # description for now. (A small follow-up phase will fill in the
            # rich summary once the video phase + deepen step finish.)
            ai_description = (
                f"Reference video '{display_name or filename or slug}' — "
                f"pending deep analysis (Whisper + scenes + structured)."
            )
            attributes = {"analyze_status": "pending"}
            if duration_sec:
                attributes["duration_sec"] = round(duration_sec, 3)
        else:  # other
            ai_description = f"Uploaded asset: {filename or slug}"

        # Embed the description (768d) when we have one
        embedding = None
        if ai_description and ai_description.strip():
            try:
                vecs = await gemini_embed([ai_description])
                if vecs and vecs[0]:
                    embedding = vecs[0]
            except Exception:
                log.exception("Embedding failed for %s", slug)

        # Persist file to storage (bucket primary, local fallback)
        upload_result = await asset_storage.upload(
            data=raw, path=bucket_path, mime=mime,
        )

        # Store a 480px WebP derivative beside the original so the grid never has
        # to fetch (or on-the-fly transform) the full-resolution file. Recorded in
        # `attributes._thumb_path`, which _attach_thumb_urls prefers. Best-effort:
        # a failure just means this row's tile signs the original instead.
        if asset_kind == "image":
            thumb_path = await ensure_thumb_object(
                raw=raw,
                storage_path=upload_result.get("storage_path"),
                storage_bucket=upload_result.get("storage_bucket"),
                mime=mime,
            )
            if thumb_path:
                attributes["_thumb_path"] = thumb_path

        # If a font preview was rendered, store it alongside under a sibling path,
        # in the same store as the font: /preview looks in the font's bucket,
        # then under producer_assets_root/<key> (see _preview_local_abs).
        if preview_bytes is not None:
            preview_path = f"fonts/_preview/{slug}.png"
            try:
                prev = await asset_storage.upload(
                    data=preview_bytes, path=preview_path, mime="image/png",
                    prefer="auto" if upload_result.get("storage_bucket") else "local",
                )
                # The key actually written (S3 can rewrite an invalid one); a
                # local copy keeps the logical key.
                attributes["_preview_path"] = prev.get("storage_path") or preview_path
            except Exception:
                log.exception("Preview upload failed for %s", slug)

        # Insert
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO producer_assets
                      (slug, asset_kind, category, archetype, display_name,
                       storage_bucket, storage_path, local_path,
                       mime_type, bytes, width, height, duration_sec,
                       ai_description, attributes, embedding, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    RETURNING id, slug, asset_kind, category, archetype,
                              display_name, storage_bucket, storage_path,
                              local_path, mime_type, bytes, width, height,
                              duration_sec,
                              ai_description, attributes,
                              (embedding IS NOT NULL) AS embedded,
                              usage_count, last_used_at, enabled,
                              created_at, updated_at, metadata
                    """,
                    (
                        slug, asset_kind, category, archetype, display_name,
                        upload_result.get("storage_bucket"),
                        upload_result.get("storage_path"),
                        upload_result.get("local_path"),
                        mime, len(raw), width, height, duration_sec,
                        ai_description, Jsonb(attributes), embedding,
                        Jsonb({
                            "upload_storage": upload_result.get("storage"),
                            "original_filename": filename,
                            **({"source_url": source_url} if source_url else {}),
                            **(extra_metadata or {}),
                        }),
                    ),
                )
                row = await cur.fetchone()

        # TODO: when kind=video, queue background processing here. For v1 the
        # user can manually re-trigger via a follow-up endpoint (skipped here
        # to keep the upload synchronous and the surface small).

        return _row(row) or {}
    finally:
        # Best-effort cleanup of the tempdir
        try:
            tmp_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP endpoints — upload + from-url both delegate to _process_asset_bytes
# ---------------------------------------------------------------------------

class UrlUploadRequest(BaseModel):
    url: str
    asset_kind: Optional[str] = None      # auto-detected from MIME if None
    category: Optional[str] = None
    archetype: Optional[str] = None
    display_name: Optional[str] = None
    hint: Optional[str] = None


@router.post("")
async def upload_asset(
    file: UploadFile = File(...),
    asset_kind: str = Form(...),
    category: Optional[str] = Form(default=None),
    archetype: Optional[str] = Form(default=None),
    display_name: Optional[str] = Form(default=None),
    hint: Optional[str] = Form(default=None),
):
    """Upload one asset via multipart file. Sync analysis for images/fonts."""
    _validate_upload_params(asset_kind, category, archetype)
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file upload")
    mime = (
        (file.content_type
         or mimetypes.guess_type(file.filename or "")[0]
         or "application/octet-stream")
    ).lower()
    return await _process_asset_bytes(
        raw=raw,
        filename=file.filename,
        mime=mime,
        asset_kind=asset_kind,
        category=category,
        archetype=archetype,
        display_name=display_name,
        hint=hint,
        source_url=None,
    )


@router.post("/from-url")
async def upload_from_url(body: UrlUploadRequest):
    """Download an asset from a Google Drive / Dropbox / OneDrive / plain
    HTTP URL, then run it through the same analysis + storage pipeline as
    a file upload. The original URL is recorded in metadata.source_url for
    provenance."""
    # Download first so we know the MIME (needed for asset_kind inference)
    try:
        raw, mime, filename = await download_from_url(body.url)
    except UrlDownloadError as e:
        raise HTTPException(400, str(e))
    if not raw:
        raise HTTPException(400, "downloaded file is empty")

    mime = (mime or mimetypes.guess_type(filename or "")[0] or "application/octet-stream").lower()
    asset_kind = body.asset_kind or _infer_kind_from_mime(mime, filename)
    _validate_upload_params(asset_kind, body.category, body.archetype)

    return await _process_asset_bytes(
        raw=raw,
        filename=filename,
        mime=mime,
        asset_kind=asset_kind,
        category=body.category,
        archetype=body.archetype,
        display_name=body.display_name,
        hint=body.hint,
        source_url=body.url,
    )


# ---------------------------------------------------------------------------
# Bulk import from a Google Drive folder
# ---------------------------------------------------------------------------

class FolderImportRequest(BaseModel):
    url: str
    # Default kind/category/archetype to apply per-file when compatible.
    # Folders are heterogeneous; we don't force every file into the same
    # bucket. The rule: infer kind per file from MIME; if the user picked
    # a default category/archetype, apply it only to files where the
    # inferred kind matches what that category requires (e.g. character
    # archetypes only attach to images).
    default_category: Optional[str] = None
    default_archetype: Optional[str] = None
    display_name_prefix: Optional[str] = None
    hint: Optional[str] = None


def _is_supported_drive_file(df: DriveFile) -> bool:
    if df.mime_type == FOLDER_MIME:
        return False
    m = (df.mime_type or "").lower()
    if m.startswith("image/") or m.startswith("video/"):
        return True
    if "font" in m:
        return True
    # Fallback by name for fonts that come back as application/octet-stream
    suf = Path(df.name).suffix.lower()
    return suf in FONT_EXTS


@router.post("/folder-preview")
async def preview_folder(body: FolderImportRequest) -> dict:
    """Inspect a Drive folder URL without importing. Returns the file list
    so the UI can show 'About to import N files (X images, Y videos, …)'
    before the user commits."""
    if not is_drive_folder_url(body.url):
        raise HTTPException(400, "URL must be a Google Drive folder (drive.google.com/drive/folders/…)")
    folder_id = extract_drive_folder_id(body.url)
    if not folder_id:
        raise HTTPException(400, "Could not extract a folder id from that URL.")
    try:
        files = await list_drive_folder(folder_id)
    except DriveFolderError as e:
        raise HTTPException(400, str(e))

    supported, unsupported, subfolders = [], [], []
    for f in files:
        entry = {"id": f.id, "name": f.name, "mime_type": f.mime_type, "size": f.size}
        if f.mime_type == FOLDER_MIME:
            subfolders.append(entry)
        elif _is_supported_drive_file(f):
            kind = _infer_kind_from_mime(f.mime_type, f.name)
            supported.append({**entry, "inferred_kind": kind})
        else:
            unsupported.append(entry)

    counts = {
        "images": sum(1 for s in supported if s["inferred_kind"] == "image"),
        "videos": sum(1 for s in supported if s["inferred_kind"] == "video"),
        "fonts": sum(1 for s in supported if s["inferred_kind"] == "font"),
        "other": sum(1 for s in supported if s["inferred_kind"] == "other"),
        "unsupported": len(unsupported),
        "subfolders": len(subfolders),
        "total_supported": len(supported),
    }
    return {
        "folder_id": folder_id,
        "supported": supported,
        "unsupported": unsupported,
        "subfolders": subfolders,
        "counts": counts,
    }


@router.post("/from-folder")
async def import_from_folder(body: FolderImportRequest) -> dict:
    """Bulk-import every supported media file inside a public Drive folder.

    Heterogeneity is the point: a folder typically holds images + videos +
    fonts mixed together. We infer kind per file and route each through the
    same analysis pipeline as single uploads. Failures on individual files
    don't abort the batch — they're collected in `failed` so the UI can
    show what skipped and why.

    Subfolders are NOT recursed (v1). Unsupported MIMEs (PDFs, zips, etc.)
    are reported in `skipped` with reason='unsupported_type'.
    """
    if not is_drive_folder_url(body.url):
        raise HTTPException(400, "URL must be a Google Drive folder (drive.google.com/drive/folders/…)")
    folder_id = extract_drive_folder_id(body.url)
    if not folder_id:
        raise HTTPException(400, "Could not extract a folder id from that URL.")

    # Validate defaults early — if they're nonsense we'd rather fail now than
    # after the first download.
    _clean_category(body.default_category)  # raises on malformed; free-text allowed
    if body.default_archetype is not None and body.default_archetype not in ALLOWED_ARCHETYPES:
        raise HTTPException(400, f"default_archetype must be one of {sorted(ALLOWED_ARCHETYPES)}")
    if body.default_category == "character" and body.default_archetype not in ALLOWED_ARCHETYPES:
        raise HTTPException(400, "default_category=character requires default_archetype to be set")

    try:
        files = await list_drive_folder(folder_id)
    except DriveFolderError as e:
        raise HTTPException(400, str(e))

    imported: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    subfolders: list[dict] = []

    # Process serially — analyzers hit Vertex which has tight per-minute
    # quotas. Parallel + bulk = rate-limit thrash. ~10s per image is fine
    # for the typical <50-file batch.
    for f in files:
        if f.mime_type == FOLDER_MIME:
            subfolders.append({"id": f.id, "name": f.name})
            continue

        if not _is_supported_drive_file(f):
            skipped.append({
                "id": f.id, "name": f.name, "mime_type": f.mime_type,
                "reason": "unsupported_type",
            })
            continue

        kind = _infer_kind_from_mime(f.mime_type, f.name)

        # Apply the user's chosen defaults only when compatible with the
        # per-file kind. Character archetypes only attach to images; other
        # kinds get category=None.
        per_file_category: Optional[str]
        per_file_archetype: Optional[str]
        if kind == "image":
            per_file_category = body.default_category
            per_file_archetype = (
                body.default_archetype if body.default_category == "character" else None
            )
        elif kind == "video":
            per_file_category = None  # videos have no category in v3 schema
            per_file_archetype = None
        elif kind == "font":
            per_file_category = None
            per_file_archetype = None
        else:
            per_file_category = "free"
            per_file_archetype = None

        try:
            _validate_upload_params(kind, per_file_category, per_file_archetype)
        except HTTPException as e:
            failed.append({
                "id": f.id, "name": f.name, "reason": f"validation: {e.detail}",
            })
            continue

        view_url = build_file_view_url(f.id)
        try:
            raw, mime, filename = await download_from_url(view_url)
        except UrlDownloadError as e:
            failed.append({"id": f.id, "name": f.name, "reason": f"download: {e}"})
            continue
        except Exception as e:
            log.exception("folder import download crashed for %s", f.id)
            failed.append({"id": f.id, "name": f.name, "reason": f"download crash: {e}"})
            continue

        if not raw:
            failed.append({"id": f.id, "name": f.name, "reason": "empty download"})
            continue

        # Prefer the Drive-side filename (UTF-8 safe) over content-disposition's
        # ASCII-mangled fallback.
        effective_filename = f.name or filename
        effective_mime = (
            mime
            or f.mime_type
            or mimetypes.guess_type(effective_filename or "")[0]
            or "application/octet-stream"
        ).lower()
        # Re-infer kind from the real MIME we got back (Drive sometimes
        # returns generic application/octet-stream pre-download).
        effective_kind = _infer_kind_from_mime(effective_mime, effective_filename) or kind

        display_name = (
            f"{body.display_name_prefix} — {f.name}"
            if body.display_name_prefix
            else None
        )

        try:
            row = await _process_asset_bytes(
                raw=raw,
                filename=effective_filename,
                mime=effective_mime,
                asset_kind=effective_kind,
                category=per_file_category if effective_kind == "image" else None,
                archetype=per_file_archetype if effective_kind == "image" else None,
                display_name=display_name,
                hint=body.hint,
                source_url=view_url,
            )
            imported.append({
                "id": row.get("id"),
                "drive_id": f.id,
                "name": f.name,
                "asset_kind": row.get("asset_kind"),
                "slug": row.get("slug"),
            })
        except HTTPException as e:
            failed.append({"id": f.id, "name": f.name, "reason": f"process: {e.detail}"})
        except Exception as e:
            log.exception("folder import processing crashed for %s", f.id)
            failed.append({"id": f.id, "name": f.name, "reason": f"process crash: {e}"})

        # Yield control briefly between items so the event loop can flush
        # logs / serve other requests while a large folder imports.
        await asyncio.sleep(0)

    return {
        "folder_id": folder_id,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "subfolders": subfolders,
        "counts": {
            "imported": len(imported),
            "skipped": len(skipped),
            "failed": len(failed),
            "subfolders": len(subfolders),
        },
    }


# ---------------------------------------------------------------------------
# Simple upload (no AI) — local file + content_category
# ---------------------------------------------------------------------------

# Content categories the manual "Add asset" dialog can upload under. Mirrors
# the 7 cards the frontend shows (Assets.tsx CATEGORIES) so every category the
# user can pick actually uploads — and so videos can be filed under the
# video-ish ones (video_interview / img_poster_animated). content_category is a
# free-form metadata tag, so widening this is just a tag, not a schema change.
UPLOAD_ALLOWED_CATEGORIES = {
    "img_poster", "img_poster_human", "img_poster_animated",
    "video_interview", "img_intro", "img_outro", "img_bg",
}

# Categories that get vision + embedding analysis at upload time. Other
# categories upload the bytes only — they're already styled posters where
# AI-generated descriptions don't add much retrieval value.
_AI_ANALYZE_CATEGORIES = {"img_bg"}


@router.post("/upload-simple")
async def upload_simple(
    file: UploadFile = File(...),
    content_category: str = Form(...),
    display_name: Optional[str] = Form(None),
) -> dict:
    """Upload a local image OR video with a content category.

    Images: `img_poster` / `img_poster_human` upload bytes-only (no AI);
    `img_bg` runs the background analyzer (vision + structured attributes)
    and embeds the description so retrieval works like character references.
    Videos: stored as `asset_kind="video"` with the clip duration probed;
    deep analysis (transcript / scenes) is deferred, not run inline.
    """
    if content_category not in UPLOAD_ALLOWED_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Category must be one of: {', '.join(sorted(UPLOAD_ALLOWED_CATEGORIES))}",
        )

    raw = await file.read()
    mime = file.content_type or mimetypes.guess_type(file.filename or "img.jpg")[0] or "image/jpeg"
    is_video = mime.startswith("video/")
    if not (mime.startswith("image/") or is_video):
        raise HTTPException(status_code=400, detail="Only image or video files are allowed")
    asset_kind = "video" if is_video else "image"

    ext = _ext_from(file.filename, mime)
    slug = _slugify(
        (display_name or file.filename or "")[:40] or "upload",
        "upload",
    )
    bucket_path = f"manual/{content_category}/{slug}.{ext}"
    name = (display_name or file.filename or "").strip()[:80] or f"Upload {slug}"

    # Dimensions (images) / duration (videos)
    width = height = None
    duration_sec: float | None = None
    if is_video:
        # Probe the clip length so the grid shows a duration badge. The ffmpeg
        # probe needs a path, so stage the bytes in a temp file briefly.
        import tempfile as _tf
        _vdir = Path(_tf.mkdtemp(prefix="vid_upload_"))
        _vpath = _vdir / f"{slug}.{ext}"
        try:
            _vpath.write_bytes(raw)
            duration_sec = _probe_audio_duration(_vpath)
        finally:
            try:
                _vpath.unlink(missing_ok=True)
                _vdir.rmdir()
            except Exception:
                pass
    else:
        try:
            from PIL import Image
            with Image.open(BytesIO(raw)) as im:
                width, height = im.size
        except Exception:
            pass

    # AI vision + embedding for analyzable IMAGE categories (currently img_bg).
    # We do this BEFORE bucket upload so a model failure doesn't waste a
    # storage round-trip on an asset we'd then have to clean up. Videos skip
    # inline analysis — their deep pass (transcript / scenes) runs later.
    ai_description: str = ""
    attributes: dict = {}
    embedding: list[float] | None = None
    if is_video:
        attributes = {"analyze_status": "pending"}
    if not is_video and content_category in _AI_ANALYZE_CATEGORIES:
        import tempfile as _tempfile
        # describe_background_image takes a file path; write to a temp file.
        tmp_dir = Path(_tempfile.mkdtemp(prefix="bg_upload_"))
        tmp_path = tmp_dir / f"{slug}.{ext}"
        try:
            tmp_path.write_bytes(raw)
            try:
                ai_description, attributes = await describe_background_image(tmp_path)
            except Exception:
                log.exception("Background analysis failed for %s", slug)
                ai_description, attributes = "", {}
            # Embed the description for semantic retrieval (same lane as
            # character ai_description, producer_scripts, etc.)
            if ai_description.strip():
                try:
                    vecs = await gemini_embed([ai_description])
                    if vecs and vecs[0]:
                        embedding = vecs[0]
                except Exception:
                    log.exception("Embedding failed for %s", slug)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except Exception:
                pass

    upload_result = await asset_storage.upload(data=raw, path=bucket_path, mime=mime)

    # 480px WebP derivative so the grid never fetches this original again.
    if asset_kind == "image":
        _tp = await ensure_thumb_object(
            raw=raw,
            storage_path=upload_result.get("storage_path"),
            storage_bucket=upload_result.get("storage_bucket"),
            mime=mime,
        )
        if _tp:
            attributes = {**(attributes or {}), "_thumb_path": _tp}

    md = {
        "content_category": content_category,
        "source": "manual_upload",
        "original_filename": file.filename,
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes, width, height, duration_sec,
                   ai_description, attributes, embedding,
                   metadata, enabled)
                VALUES
                  (%s, %s, 'free', %s,
                   %s, %s, %s,
                   %s, %s, %s, %s, %s,
                   %s, %s, %s,
                   %s, TRUE)
                RETURNING *
                """,
                (
                    slug, asset_kind, name,
                    upload_result.get("storage_bucket"),
                    upload_result.get("storage_path"),
                    upload_result.get("local_path"),
                    mime, len(raw), width, height, duration_sec,
                    # ai_description column is NOT NULL — store '' when no
                    # analysis ran (poster / poster_human / video paths).
                    ai_description.strip(),
                    Jsonb(attributes), embedding,
                    Jsonb(md),
                ),
            )
            row = await cur.fetchone()

    result = _row(row) or {}
    # pgvector returns embedding as numpy.ndarray which Pydantic / json can't
    # serialise. The frontend doesn't use the raw vector — surface only
    # whether one exists (matches the from-frame endpoint's convention).
    if "embedding" in result:
        result["embedded"] = result.pop("embedding") is not None
    if row.get("storage_bucket") and row.get("storage_path"):
        try:
            result["signed_url"] = await asset_storage.url_for(
                storage_bucket=row["storage_bucket"],
                storage_path=row["storage_path"],
                local_path=row.get("local_path"),
            )
        except Exception:
            result["signed_url"] = None
    return result


# ---------------------------------------------------------------------------
# Create from Instagram post
# ---------------------------------------------------------------------------

class FromPostRequest(BaseModel):
    post_id: int
    asset_id: Optional[int] = None  # specific carousel child; None = first asset


@router.post("/from-post")
async def create_from_post(body: FromPostRequest) -> dict:
    """Save an Instagram post as a producer asset.

    Reads the post's first image/video from local disk, uploads it to
    Supabase Storage, and inserts a producer_assets row with the post's
    existing captions and AI descriptions (no re-analysis).
    """
    from ..services.storage import resolve_local

    post_id = body.post_id

    # 1. Fetch the post + its first asset
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ip.id, ip.caption, ip.ai_summary, ip.permalink,
                       ip.media_type,
                       ip.metadata->>'content_category' AS content_category
                  FROM instagram_posts ip
                 WHERE ip.id = %s
                """,
                (post_id,),
            )
            post = await cur.fetchone()
            if not post:
                raise HTTPException(status_code=404, detail="Post not found")

            if not post["content_category"]:
                raise HTTPException(
                    status_code=400,
                    detail="Post must be classified before adding to assets",
                )

            # Get the requested asset (specific or first)
            if body.asset_id:
                await cur.execute(
                    """
                    SELECT a.id AS asset_id, a.local_path, a.mime_type,
                           a.asset_type, a.width, a.height, a.ai_description
                      FROM instagram_post_assets ipa
                      JOIN assets a ON a.id = ipa.asset_id
                     WHERE ipa.post_id = %s AND a.id = %s
                       AND a.download_status = 'downloaded'
                       AND a.local_path IS NOT NULL
                    """,
                    (post_id, body.asset_id),
                )
            else:
                await cur.execute(
                    """
                    SELECT a.id AS asset_id, a.local_path, a.mime_type,
                           a.asset_type, a.width, a.height, a.ai_description
                      FROM instagram_post_assets ipa
                      JOIN assets a ON a.id = ipa.asset_id
                     WHERE ipa.post_id = %s
                       AND a.download_status = 'downloaded'
                       AND a.local_path IS NOT NULL
                     ORDER BY ipa.child_index
                     LIMIT 1
                    """,
                    (post_id,),
                )
            asset = await cur.fetchone()
            if not asset:
                raise HTTPException(
                    status_code=400, detail="Post has no downloaded media file",
                )

            # Check duplicate — per asset, not per post (carousels have multiple)
            await cur.execute(
                "SELECT id FROM producer_assets WHERE source_post_id = %s AND source_asset_id = %s",
                (post_id, asset["asset_id"]),
            )
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail="This image is already saved as asset"
                )

    # 2. Read file bytes
    local = resolve_local(asset["local_path"])
    if not local.exists():
        raise HTTPException(status_code=410, detail="Media file missing on disk")
    raw = local.read_bytes()
    mime = asset["mime_type"] or mimetypes.guess_type(str(local))[0] or "image/jpeg"

    # 3. Determine kind + build metadata
    content_cat = post["content_category"]
    # Use mime type to decide — animated posts are videos even though
    # the category starts with "img_"
    asset_kind = "video" if (mime or "").startswith("video/") else "image"
    # For the two video content categories, the user wants frame extraction
    # (one image per detected person, or the cleanest poster frame), NOT a
    # raw video asset. Reject and direct them to the dedicated endpoint.
    if asset_kind == "video" and content_cat in {"img_poster_animated", "video_interview"}:
        mode = "talking_head" if content_cat == "video_interview" else "animated_poster"
        raise HTTPException(
            status_code=422,
            detail={
                "error": "use_extract_frames",
                "hint": f"POST /api/agents/producer/assets/from-post/extract-frames with mode='{mode}' for {content_cat} posts",
                "mode": mode,
            },
        )
    ext = _ext_from(local.name, mime)
    slug = _slugify(
        (post["caption"] or "")[:40] or f"ig-{post_id}",
        f"ig_{post_id}",
    )
    bucket_path = f"instagram/{content_cat}/{slug}.{ext}"

    caption = (post["caption"] or "").strip()
    display_name = caption[:80] if caption else f"IG post #{post_id}"
    image_understanding = (asset["ai_description"] or "").strip()
    composed_text = (post["ai_summary"] or "").strip()
    # ai_description gets the richest text available
    ai_desc = composed_text or image_understanding or caption or ""

    # 4. Upload to Supabase bucket
    upload_result = await asset_storage.upload(data=raw, path=bucket_path, mime=mime)

    # 480px WebP derivative so the grid never fetches this original again.
    _thumb_path = None
    if asset_kind == "image":
        _thumb_path = await ensure_thumb_object(
            raw=raw,
            storage_path=upload_result.get("storage_path"),
            storage_bucket=upload_result.get("storage_bucket"),
            mime=mime,
        )

    # 5. Insert into producer_assets (no AI calls — reuse existing descriptions)
    md = {
        "content_category": content_cat,
        "caption": caption,
        "image_understanding": image_understanding,
        "composed_retrieval_text": composed_text,
        "permalink": post.get("permalink"),
        "source": "instagram",
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name, ai_description,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes, width, height,
                   source_post_id, source_asset_id,
                   attributes, metadata, enabled)
                VALUES
                  (%s, %s, 'free', %s, %s,
                   %s, %s, %s,
                   %s, %s, %s, %s,
                   %s, %s,
                   %s, %s, TRUE)
                RETURNING *
                """,
                (
                    slug, asset_kind, display_name, ai_desc,
                    upload_result.get("storage_bucket"),
                    upload_result.get("storage_path"),
                    upload_result.get("local_path"),
                    mime, len(raw),
                    asset.get("width"), asset.get("height"),
                    post_id, asset["asset_id"],
                    Jsonb({"_thumb_path": _thumb_path} if _thumb_path else {}),
                    Jsonb(md),
                ),
            )
            row = await cur.fetchone()

    result = _row(row)
    # Add signed URL for frontend
    if row.get("storage_bucket") and row.get("storage_path"):
        try:
            result["signed_url"] = await asset_storage.url_for(
                storage_bucket=row["storage_bucket"],
                storage_path=row["storage_path"],
                local_path=row.get("local_path"),
            )
        except Exception:
            result["signed_url"] = None
    return result


# ---------------------------------------------------------------------------
# Frame extraction for video IG categories
# ---------------------------------------------------------------------------
#
# For img_poster_animated and video_interview, raw-video import is wrong: the
# Producer needs reference *images*, not clips. These two endpoints sit between
# the user's "+ Add to Asset" click and the actual asset row creation.
#
#   POST /from-post/extract-frames  →  analyse video, return candidate frames
#   POST /from-post/from-frame      →  create producer_assets row from chosen frame
#
# Candidates are cached in-process for 1h per (post_id, asset_id, mode) so
# re-opening the modal doesn't re-burn the Gemini call.

# Simple TTL cache: {key -> (expiry_epoch, payload)}. Per-process; survives
# only until backend restart. Good enough for v1 — Redis can replace this if
# we add multiple backend instances.
import time as _time

_FRAME_CACHE_TTL_SEC = 60 * 60
_frame_cache: dict[tuple[int, int, str], tuple[float, dict]] = {}


def _frame_cache_get(post_id: int, asset_id: int, mode: str) -> dict | None:
    key = (post_id, asset_id, mode)
    entry = _frame_cache.get(key)
    if not entry:
        return None
    expiry, payload = entry
    if expiry < _time.time():
        _frame_cache.pop(key, None)
        return None
    return payload


def _frame_cache_set(post_id: int, asset_id: int, mode: str, payload: dict) -> None:
    _frame_cache[(post_id, asset_id, mode)] = (
        _time.time() + _FRAME_CACHE_TTL_SEC, payload,
    )


def _frame_to_dict(post_id: int, f: afe.FrameInfo) -> dict:
    """Shape sent to the frontend for one candidate frame."""
    return {
        "frame_id": f.frame_id,
        "timestamp_sec": round(f.timestamp_sec, 3),
        "url": f"/api/agents/instagram-ingest/posts/{post_id}/frames/{f.frame_id}/file",
    }


class ExtractFramesRequest(BaseModel):
    post_id: int
    asset_id: int
    mode: str  # 'talking_head' | 'animated_poster' | 'intro' | 'outro'


_VALID_EXTRACT_MODES = {"talking_head", "animated_poster", "intro", "outro"}


def _intro_outro_frame_dict(post_id: int, c: afe.IntroOutroFrameCandidate) -> dict:
    """Shape sent to the frontend for one intro/outro candidate."""
    return {
        "rank": c.rank,
        "frame": _frame_to_dict(post_id, c.frame),
        "text_completeness": c.text_completeness,
        "visual_dominance": c.visual_dominance,
        "clarity": c.clarity,
        "note": c.note,
    }


@router.post("/from-post/extract-frames")
async def extract_frames(body: ExtractFramesRequest) -> dict:
    """Analyse a video IG post and return candidate frames for the user to pick.

    Sync — returns within ~15s for a 30s reel. Caches results 1h so re-opens
    don't re-burn the Gemini call.
    """
    mode = body.mode
    if mode not in _VALID_EXTRACT_MODES:
        raise HTTPException(400, f"mode must be one of {sorted(_VALID_EXTRACT_MODES)}")

    cached = _frame_cache_get(body.post_id, body.asset_id, mode)
    if cached is not None:
        return {**cached, "cached": True}

    try:
        if mode == "talking_head":
            result = await afe.extract_talking_head_candidates(
                post_id=body.post_id, asset_id=body.asset_id,
            )
            payload = {
                "mode": mode,
                "candidates": [
                    {
                        "person_label": c.person_label,
                        "description": c.description,
                        "suggested_archetype": c.suggested_archetype,
                        "score": c.score,
                        "best_frame": _frame_to_dict(body.post_id, c.best_frame),
                        "alternates": [
                            _frame_to_dict(body.post_id, f) for f in c.alternates
                        ],
                    }
                    for c in result.candidates
                ],
                "degrade_reason": result.degrade_reason,
                "dominant_content": result.dominant_content,
                "suggest_animated_poster": result.suggest_animated_poster,
            }
        elif mode == "animated_poster":
            result = await afe.extract_animated_poster_candidates(
                post_id=body.post_id, asset_id=body.asset_id,
            )
            payload = {
                "mode": mode,
                "candidates": [
                    {
                        "rank": c.rank,
                        "frame": _frame_to_dict(body.post_id, c.frame),
                    }
                    for c in result.candidates
                ],
                "confidence": result.confidence,
                "rejected": [
                    {
                        "frame": _frame_to_dict(body.post_id, c.frame),
                        "reason": c.rejected_reason,
                    }
                    for c in result.rejected
                ],
            }
        else:
            # intro | outro
            if mode == "intro":
                io_result = await afe.extract_intro_candidates(
                    post_id=body.post_id, asset_id=body.asset_id,
                )
            else:
                io_result = await afe.extract_outro_candidates(
                    post_id=body.post_id, asset_id=body.asset_id,
                )
            payload = {
                "mode": mode,
                "position": io_result.position,
                "candidates": [
                    _intro_outro_frame_dict(body.post_id, c)
                    for c in io_result.candidates
                ],
                "confidence": io_result.confidence,
                "rejected": [
                    {
                        "frame": _frame_to_dict(body.post_id, c.frame),
                        "reason": c.rejected_reason,
                    }
                    for c in io_result.rejected
                ],
                "degrade_reason": io_result.degrade_reason,
            }
    except afe.VideoNotFoundError as e:
        raise HTTPException(422, str(e))
    except afe.BudgetGuardError as e:
        raise HTTPException(503, str(e))
    except afe.GeminiTimeoutError as e:
        # Structured payload so the frontend can offer a retry button
        raise HTTPException(
            status_code=504,
            detail={"error": "timeout", "message": str(e), "retry_safe": True},
        )

    _frame_cache_set(body.post_id, body.asset_id, mode, payload)
    return {**payload, "cached": False}


class FromFrameRequest(BaseModel):
    post_id: int
    asset_id: int  # the source video asset
    frame_id: int  # video_frames.id (verified to belong to asset_id)
    content_category: str  # 'img_poster_animated' | 'video_interview' | 'img_intro' | 'img_outro'
    archetype: Optional[str] = None
    display_name: Optional[str] = None
    person_label: Optional[str] = None
    person_description: Optional[str] = None
    analyze_attributes: bool = False  # run asset_analyzer for character JSON


@router.post("/from-post/from-frame")
async def create_from_frame(body: FromFrameRequest) -> dict:
    """Create a producer_assets image row from a chosen video_frame.

    Triggered when the user picks a candidate in the extract-frames modal.
    Validates linkage, copies the frame file into the assets bucket, runs
    the optional character analyzer, inserts the row. The new unique index
    on (source_frame_id WHERE NOT NULL) converts double-click races into a
    clean 409 with the existing asset id.
    """
    from ..services.storage import resolve_local
    from psycopg.errors import UniqueViolation

    valid_content_cats = {
        "img_poster_animated", "video_interview",
        "img_intro", "img_outro",
    }
    if body.content_category not in valid_content_cats:
        raise HTTPException(
            400, f"content_category must be one of {sorted(valid_content_cats)}",
        )
    if body.archetype is not None and body.archetype not in ALLOWED_ARCHETYPES:
        raise HTTPException(
            400, f"archetype must be one of {sorted(ALLOWED_ARCHETYPES)} or omitted",
        )

    # 1. Verify the frame belongs to the named (post, asset) and that the
    # underlying video asset still exists. Pull the post's permalink + the
    # video asset's URL for provenance.
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT vf.local_path AS frame_path,
                       vf.timestamp_sec,
                       ip.permalink,
                       ip.caption,
                       ip.metadata->>'content_category' AS post_content_category
                  FROM video_frames vf
                  JOIN instagram_post_assets ipa ON ipa.asset_id = vf.asset_id
                  JOIN instagram_posts ip ON ip.id = ipa.post_id
                 WHERE vf.id = %s
                   AND ipa.post_id = %s
                   AND vf.asset_id = %s
                """,
                (body.frame_id, body.post_id, body.asset_id),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "frame does not belong to this (post, asset)")
    if not row.get("frame_path"):
        raise HTTPException(410, "frame file missing from db")

    local_frame = resolve_local(row["frame_path"])
    if not local_frame.exists():
        raise HTTPException(410, "frame file missing on disk")
    raw = local_frame.read_bytes()
    mime = "image/jpeg"  # _save_frame_sync always writes JPEG

    # 2. Build slug + bucket path
    base_for_slug = (
        body.display_name
        or body.person_label
        or (body.person_description or "")[:40]
        or (row.get("caption") or "")[:40]
        or f"ig_{body.post_id}_frame_{body.frame_id}"
    )
    slug = _slugify(base_for_slug, f"ig_{body.post_id}_f{body.frame_id}")
    bucket_path = f"instagram/{body.content_category}/{slug}.jpg"

    # 3. Optional character archetype analysis (kandura colour, ghutra etc.)
    ai_description = ""
    attributes: dict = {}
    if body.analyze_attributes and body.archetype in ALLOWED_ARCHETYPES:
        try:
            ai_description, attributes = await describe_character_image(
                local_frame, archetype=body.archetype,  # type: ignore[arg-type]
            )
        except Exception:
            log.exception(
                "character analysis failed for frame #%d — saving without attributes",
                body.frame_id,
            )

    # Fall back to user-provided description or person_label if no analysis
    if not ai_description:
        ai_description = (
            (body.person_description or "")
            or (body.display_name or "")
            or f"Frame at {row['timestamp_sec']:.1f}s from IG video"
        )

    # 4. Pre-validate the (asset_kind=image, category) shape via the shared
    # validator. Character category requires archetype; otherwise NULL.
    category: Optional[str] = "character" if body.archetype else None
    _validate_upload_params("image", category, body.archetype)

    # 5. Embed the description for retrieval
    embedding = None
    if ai_description.strip():
        try:
            vecs = await gemini_embed([ai_description])
            if vecs and vecs[0]:
                embedding = vecs[0]
        except Exception:
            log.exception("embedding failed for frame import")

    # 6. Upload bytes to bucket-or-disk
    upload_result = await asset_storage.upload(
        data=raw, path=bucket_path, mime=mime,
    )

    # 480px WebP derivative so the grid never fetches this original again.
    _tp = await ensure_thumb_object(
        raw=raw,
        storage_path=upload_result.get("storage_path"),
        storage_bucket=upload_result.get("storage_bucket"),
        mime=mime,
    )
    if _tp:
        attributes = {**(attributes or {}), "_thumb_path": _tp}

    # 7. Get image dimensions for the DB row
    width = height = None
    try:
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(raw)) as im:
            width, height = im.size
    except Exception:
        pass

    # 8. Insert producer_assets row — the partial unique index on
    # source_frame_id WHERE NOT NULL converts a race-condition double-click
    # into a 23505 IntegrityError that we map to 409.
    md = {
        "content_category": body.content_category,
        "source": "instagram_frame",
        "person_label": body.person_label,
        "video_timestamp_sec": float(row["timestamp_sec"]),
        "derived_from_video_asset_id": body.asset_id,
        "permalink": row.get("permalink"),
    }

    display_name = (
        body.display_name
        or body.person_label
        or (f"Frame · {row['timestamp_sec']:.1f}s · post #{body.post_id}")
    )

    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO producer_assets
                      (slug, asset_kind, category, archetype, display_name,
                       storage_bucket, storage_path, local_path,
                       mime_type, bytes, width, height,
                       ai_description, attributes, embedding,
                       source_post_id, source_asset_id, source_frame_id,
                       metadata, enabled)
                    VALUES (%s, 'image', %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, TRUE)
                    RETURNING *
                    """,
                    (
                        slug, category, body.archetype, display_name,
                        upload_result.get("storage_bucket"),
                        upload_result.get("storage_path"),
                        upload_result.get("local_path"),
                        mime, len(raw), width, height,
                        ai_description.strip() or None,
                        Jsonb(attributes), embedding,
                        body.post_id, body.asset_id, body.frame_id,
                        Jsonb(md),
                    ),
                )
                row_inserted = await cur.fetchone()
    except UniqueViolation:
        # The losing request's copy and thumbnail are referenced by no row (its
        # slug is random), so drop them rather than leave orphans in the bucket.
        await asset_storage.delete(
            storage_bucket=upload_result.get("storage_bucket"),
            storage_path=upload_result.get("storage_path"),
            local_path=upload_result.get("local_path"),
        )
        if _tp:
            await asset_storage.delete(
                storage_bucket=upload_result.get("storage_bucket"),
                storage_path=_tp,
                local_path=None,
            )
        # Look up the existing row so the frontend can show "already imported"
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, display_name FROM producer_assets WHERE source_frame_id = %s",
                    (body.frame_id,),
                )
                existing = await cur.fetchone()
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_imported",
                "existing_asset_id": existing.get("id") if existing else None,
                "existing_display_name": existing.get("display_name") if existing else None,
            },
        )

    result = _row(row_inserted) or {}
    # pgvector returns embedding as numpy.ndarray, which pydantic v2 can't
    # serialise. The frontend doesn't use the raw vector — surface only
    # whether one exists.
    if "embedding" in result:
        result["embedded"] = result.pop("embedding") is not None
    if row_inserted.get("storage_bucket") and row_inserted.get("storage_path"):
        try:
            result["signed_url"] = await asset_storage.url_for(
                storage_bucket=row_inserted["storage_bucket"],
                storage_path=row_inserted["storage_path"],
                local_path=row_inserted.get("local_path"),
            )
        except Exception:
            result["signed_url"] = None
    return result


# ---------------------------------------------------------------------------
# List + detail
# ---------------------------------------------------------------------------

@router.get("")
async def list_assets(
    kind: Optional[str] = None,
    category: Optional[str] = None,
    archetype: Optional[str] = None,
    content_category: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    where: list[str] = []
    params: list = []
    if kind:
        where.append("asset_kind = %s")
        params.append(kind)
    if category:
        where.append("category = %s")
        params.append(category)
    if archetype:
        where.append("archetype = %s")
        params.append(archetype)
    if content_category:
        where.append("metadata->>'content_category' = %s")
        params.append(content_category)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    params.extend([limit, offset])
    # Read-only: run in autocommit so the two SELECTs below never leave the
    # connection idle-in-transaction between the rows fetch and the totals count
    # (Supabase's pooler kills such a connection → IdleInTransactionSessionTimeout
    # 500 → the asset picker hangs). See db.get_conn for the full rationale.
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, slug, asset_kind, category, archetype, display_name,
                       storage_bucket, storage_path, local_path,
                       mime_type, bytes, width, height, duration_sec,
                       LEFT(ai_description, 300) AS preview,
                       (embedding IS NOT NULL) AS embedded,
                       usage_count, last_used_at, enabled,
                       created_at, updated_at, metadata, attributes
                  FROM producer_assets
                  {where_sql}
                 ORDER BY created_at DESC
                 LIMIT %s OFFSET %s
                """,
                tuple(params),
            )
            items = [_row(r) for r in await cur.fetchall()]

            await cur.execute(
                """
                SELECT
                  COUNT(*)::int AS total,
                  COUNT(*) FILTER (WHERE asset_kind='image')::int AS images,
                  COUNT(*) FILTER (WHERE asset_kind='video')::int AS videos,
                  COUNT(*) FILTER (WHERE asset_kind='font')::int AS fonts,
                  COUNT(*) FILTER (WHERE asset_kind='audio')::int AS audio,
                  COUNT(*) FILTER (WHERE category='character')::int AS characters,
                  COUNT(*) FILTER (WHERE category='sticker')::int AS stickers,
                  COUNT(*) FILTER (WHERE enabled)::int AS enabled_count,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_poster')::int AS cat_img_poster,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_poster_human')::int AS cat_img_poster_human,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_human_character')::int AS cat_img_human_character,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_poster_animated')::int AS cat_img_poster_animated,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'video_interview')::int AS cat_video_interview,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_intro')::int AS cat_img_intro,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_outro')::int AS cat_img_outro,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_bg')::int AS cat_img_bg,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'other')::int AS cat_other
                FROM producer_assets
                """
            )
            totals = _row(await cur.fetchone()) or {}

    await _attach_thumb_urls(items)
    return {"items": items, "totals": totals}


# Grid tiles render at ~250px; the detail drawer at up to ~1400px.
_ASSET_THUMB_TTL = 86400   # 24h — see asset_storage.signed_render_url on why not 7d

# Stored thumbnail derivatives (thumbs/<key>.webp in the original's bucket) are
# made by services/thumbs.ensure_thumb_object, imported above.


async def _attach_thumb_urls(items: list[dict]) -> None:
    """Add `thumb_url` to every raster-image row, in place.

    Why this exists: there was NO thumbnail representation of an asset anywhere in
    the system. `/{id}/file` 307s to a signed URL of the untouched ORIGINAL, and
    producer assets average ~3.1 MB (the img_bg category averages 9.3 MB), so the
    Assets grid and the picker were downloading hundreds of megabytes to fill
    250px squares — 537 MB for Background alone.

    `/render/image/public` cannot serve producer-assets (it is a PRIVATE bucket,
    and must stay private: 1.5 GB of portrait and location photography). A signed
    token served off the /render/image path re-encodes to WebP without exposing
    anything, and batch signing makes it ONE round trip for the whole page.

    Skips non-raster kinds — Supabase's transformer cannot handle SVG or video, so
    a thumb_url for those would 400 and render as a broken tile. Those rows simply
    have no thumb_url and their consumer falls back to `/file`. Never raises.

    On S3 only rows with a stored derivative get a thumb_url (D9): S3 cannot
    resize, and its batch presign never checks existence, so signing the
    original would hand the picker's prefetcher full-size files as "thumbnails".
    """
    if not items:
        return
    # Which object to sign per row: the stored 480px derivative when one exists
    # (≈25 kB), else the original (batch signing can only re-encode it, ≈429 kB —
    # still 22x better than the untouched file, and never broken).
    derivative_only = asset_storage.backend_name() == "s3"
    want: dict[int, tuple[str, str]] = {}   # id -> (bucket, path_to_sign)
    by_bucket: dict[str, list[str]] = {}
    for it in items:
        if it.get("asset_kind") != "image":
            continue
        if not asset_storage.can_transform(it.get("mime_type")):
            continue
        b, p = it.get("storage_bucket"), it.get("storage_path")
        if not (b and p):
            continue
        attrs = it.get("attributes") if isinstance(it.get("attributes"), dict) else {}
        if derivative_only and not attrs.get("_thumb_path"):
            continue
        target = attrs.get("_thumb_path") or p
        want[it["id"]] = (b, target)
        by_bucket.setdefault(b, []).append(target)
    if not by_bucket:
        return
    # Whether the transformer is reachable decides whether the signed URLs below
    # come back on the /render/image path or the plain /object path. Without it
    # the grid ships URLs that 403 in the browser and every tile goes blank (#37).
    # The probe needs a real object, which is exactly what we just collected.
    _probe_bucket, _probe_paths = next(iter(by_bucket.items()))
    await asset_storage.ensure_image_transform_capability(
        (_probe_bucket, _probe_paths[0]) if _probe_paths else None,
    )
    try:
        results = await asyncio.gather(
            *(
                asset_storage.signed_render_urls(
                    b, sorted(set(paths)), expires=_ASSET_THUMB_TTL,
                )
                for b, paths in by_bucket.items()
            ),
            return_exceptions=True,
        )
    except Exception:  # noqa: BLE001
        log.exception("[asset] thumb signing failed (non-fatal)")
        return
    signed: dict[str, dict[str, str]] = {}
    for bucket, res in zip(by_bucket, results):
        signed[bucket] = res if isinstance(res, dict) else {}
    for it in items:
        target = want.get(it.get("id"))
        if not target:
            continue
        url = signed.get(target[0], {}).get(target[1])
        if url:
            it["thumb_url"] = url


class AssetSearchRequest(BaseModel):
    query: str
    kind: Optional[str] = None      # image | font | audio | video
    category: Optional[str] = None  # sticker | character | …
    limit: int = 12


@router.post("/search")
async def search_assets(body: AssetSearchRequest) -> dict:
    """Semantic search over the Assets Library (groundwork for the automatic AI
    editor): embeds the query (gemini-embedding-001, 768d) and ranks enabled,
    embedded assets by cosine distance over `producer_assets.embedding`."""
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(400, "query required")
    try:
        vecs = await gemini_embed([q])
    except Exception:
        log.exception("[asset] search embed failed")
        raise HTTPException(502, "embedding failed")
    qvec = vecs[0] if vecs else None
    if not qvec:
        raise HTTPException(502, "embedding failed")

    where = ["embedding IS NOT NULL", "enabled"]
    params: list = [qvec]
    if body.kind:
        where.append("asset_kind = %s")
        params.append(body.kind)
    if body.category:
        where.append("category = %s")
        params.append(body.category)
    limit = max(1, min(int(body.limit), 50))
    params.append(limit)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, slug, asset_kind, category, archetype, display_name,
                       mime_type, duration_sec,
                       LEFT(ai_description, 300) AS preview, attributes,
                       (embedding <=> %s::vector) AS distance
                  FROM producer_assets
                 WHERE {" AND ".join(where)}
                 ORDER BY embedding <=> %s::vector
                 LIMIT %s
                """,
                # qvec is used twice (SELECT distance + ORDER BY); splice it in.
                (params[0], *params[1:-1], params[0], params[-1]),
            )
            items = [_row(r) for r in await cur.fetchall()]
    return {"items": items, "query": q}


@router.get("/{asset_id}")
async def get_asset(asset_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, slug, asset_kind, category, archetype, display_name,
                       storage_bucket, storage_path, local_path,
                       mime_type, bytes, width, height, duration_sec,
                       source_post_id, source_asset_id, source_frame_id,
                       ai_description, attributes,
                       (embedding IS NOT NULL) AS embedded,
                       usage_count, last_used_at, enabled,
                       created_at, updated_at, metadata
                  FROM producer_assets
                 WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "asset not found")
    out = _row(row) or {}
    # `preview_url` — a MID-SIZE signed transform for the detail drawer.
    #
    # This replaces a `signed_url` field that minted a full-resolution signed URL
    # on every drawer open and that NO caller ever read: a wasted Supabase round
    # trip per open, and the drawer meanwhile rendered the 9-15 MB original.
    # 1400px contain lands around 150 kB. The full original stays reachable via
    # /{id}/file for "View original" and downloads.
    #
    # Single object, so a per-path transform sign is fine here (it is one call,
    # not one per grid tile) and it gives a real resize rather than just a
    # re-encode. Raster images only — Supabase cannot transform SVG or video.
    out["preview_url"] = None
    if (
        row.get("asset_kind") == "image"
        and row.get("storage_bucket") and row.get("storage_path")
        and asset_storage.can_transform(row.get("mime_type"))
    ):
        if asset_storage.backend_name() == "s3":
            # MinIO cannot resize: the stored 1400px derivative once it exists
            # (the first open queues it); until then the drawer falls back to
            # the original via /{id}/file.
            got = await _thumbs.derivative_urls(
                row["storage_bucket"], [row["storage_path"]], "preview",
                expires=_ASSET_THUMB_TTL,
            )
            out["preview_url"] = got.get(row["storage_path"])
            return out
        try:
            out["preview_url"] = await asset_storage.signed_render_url(
                row["storage_bucket"], row["storage_path"],
                transform=asset_storage.PREVIEW_TRANSFORM,
                expires=_ASSET_THUMB_TTL,
            )
        except Exception:  # noqa: BLE001
            log.exception("[asset] preview sign failed (non-fatal)")
    return out


# ---------------------------------------------------------------------------
# File proxy (local) + preview proxy (fonts)
# ---------------------------------------------------------------------------

@router.get("/{asset_id}/file")
async def get_asset_file(asset_id: int):
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT storage_bucket, storage_path, local_path, mime_type, slug
                  FROM producer_assets WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "asset not found")

    # If on disk, stream directly. A missing local copy falls through to the
    # bucket (a backfilled row keeps local_path, and the disk copy may since have
    # been cleaned up); 410 only when neither copy exists (A22).
    local_missing = False
    if row.get("local_path"):
        p = asset_storage.local_to_abs(row["local_path"])
        if p.exists():
            return FileResponse(
                str(p),
                media_type=row.get("mime_type") or "application/octet-stream",
                filename=p.name,
                headers=_ASSET_CACHE_HEADERS,
            )
        local_missing = True

    # Otherwise 307-redirect to a signed bucket URL so the browser pulls the
    # bytes straight from the object store, with no backend proxy hop on every
    # first load. Mirrors yas_producer._media_response: a long (7-day) TTL
    # keeps the redirect target stable; the redirect itself is cached for an
    # hour so it can revalidate once that signature expires.
    signed = await asset_storage.url_for(
        storage_bucket=row.get("storage_bucket"),
        storage_path=row.get("storage_path"),
        local_path=None,
        expires=_ASSET_SIGN_TTL,
    )
    if not signed:
        if local_missing:
            raise HTTPException(410, "file missing on disk")
        raise HTTPException(404, "asset file not retrievable")
    return RedirectResponse(signed, status_code=307, headers=_ASSET_REDIRECT_CACHE_HEADERS)


def _preview_local_abs(preview_path: str) -> Path:
    """Local copy of a font preview. `_preview_path` holds the bucket key, and
    upload()'s local fallback writes that key under producer_assets_root, not
    at the key itself, which is why local previews used to 404."""
    return first_existing(
        config.producer_assets_root / preview_path,
        asset_storage.local_to_abs(preview_path),
    )


@router.get("/{asset_id}/preview")
async def get_asset_preview(asset_id: int):
    """Font preview PNG (or fallback to the file for non-fonts)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT asset_kind, attributes, storage_bucket, storage_path, local_path
                  FROM producer_assets WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "asset not found")

    preview_path = ((row.get("attributes") or {}).get("_preview_path")) if row.get("asset_kind") == "font" else None
    if not preview_path:
        # Non-fonts use the file directly
        return await get_asset_file(asset_id)

    # Fonts: fetch the preview PNG from wherever it lives
    if row.get("storage_bucket"):
        # Fetched here, server side, so the internal origin (no hairpin).
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=preview_path,
            local_path=None,
            expires=600,
            internal=True,
        )
        if signed:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.get(signed)
                if r.status_code < 300:
                    return Response(
                        content=r.content,
                        media_type="image/png",
                        headers=_ASSET_CACHE_HEADERS,
                    )
            except httpx.HTTPError as e:
                log.warning("[asset] font preview fetch failed for #%d: %s", asset_id, e)

    # Local fallback
    local_preview = _preview_local_abs(preview_path)
    if local_preview.exists():
        return FileResponse(
            str(local_preview),
            media_type="image/png",
            headers=_ASSET_CACHE_HEADERS,
        )
    raise HTTPException(404, "preview not found")


# ---------------------------------------------------------------------------
# Patch / delete
# ---------------------------------------------------------------------------

@router.patch("/{asset_id}")
async def patch_asset(asset_id: int, body: AssetPatchRequest) -> dict:
    fields: list[str] = []
    params: list = []
    if body.display_name is not None:
        fields.append("display_name = %s"); params.append(body.display_name)
    if body.ai_description is not None:
        fields.append("ai_description = %s"); params.append(body.ai_description)
        # Re-embed if description changed
        try:
            vecs = await gemini_embed([body.ai_description])
            if vecs and vecs[0]:
                fields.append("embedding = %s"); params.append(vecs[0])
        except Exception:
            log.exception("re-embed on patch failed")
    if body.enabled is not None:
        fields.append("enabled = %s"); params.append(body.enabled)
    if not fields:
        raise HTTPException(400, "no fields to update")
    params.append(asset_id)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE producer_assets SET {', '.join(fields)} "
                f"WHERE id = %s RETURNING id, display_name, ai_description, enabled, updated_at",
                tuple(params),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "asset not found")
    return _row(row) or {}


@router.delete("/{asset_id}")
async def delete_asset(asset_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT storage_bucket, storage_path, local_path, attributes, asset_kind
                  FROM producer_assets WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "asset not found")

    # Delete the file (best-effort)
    try:
        await asset_storage.delete(
            storage_bucket=row.get("storage_bucket"),
            storage_path=row.get("storage_path"),
            local_path=row.get("local_path"),
        )
    except Exception:
        log.exception("file delete failed for asset #%d", asset_id)

    attrs = row.get("attributes") or {}

    # Delete the stored 480px thumbnail, which lives in the original's bucket.
    # Only bucket rows can have one; a local row's _thumb_path is a stale
    # Supabase-era key that exists nowhere.
    thumb_path = attrs.get("_thumb_path")
    if thumb_path and row.get("storage_bucket"):
        try:
            await asset_storage.delete(
                storage_bucket=row["storage_bucket"],
                storage_path=thumb_path,
                local_path=None,
            )
        except Exception:
            log.exception("thumbnail delete failed for asset #%d", asset_id)

    # Delete font preview if any, from the bucket and from the local fallback
    preview_path = attrs.get("_preview_path") if row.get("asset_kind") == "font" else None
    if preview_path:
        try:
            local_preview = _preview_local_abs(preview_path)
            await asset_storage.delete(
                storage_bucket=row.get("storage_bucket"),
                storage_path=preview_path,
                local_path=str(local_preview) if local_preview.exists() else None,
            )
        except Exception:
            log.exception("preview delete failed for asset #%d", asset_id)

    # Drop the row
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM producer_assets WHERE id = %s", (asset_id,))
    return {"deleted": asset_id}
