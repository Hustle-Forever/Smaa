"""Enumerate the contents of a public Google Drive folder.

Used by the Assets Library so the user can paste a folder URL and bulk-import
every supported media file inside it.

Auth model: API key only. The folder must be shared as "Anyone with the link
can view". OAuth and service accounts are out of scope — adding more auth
modes is a one-line change once we actually need them.

If GOOGLE_DRIVE_API_KEY is not set in env, every call raises a setup error
with the exact steps to fix it.

The downstream upload path reuses the existing single-file downloader in
`services.url_downloader` — we just hand it back a `view_url` per child.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

from ..config import config

log = logging.getLogger("archivist.drive_folder")

_FOLDER_ID_PATTERNS = [
    re.compile(r"/drive/folders/([a-zA-Z0-9_-]{15,})"),
    re.compile(r"/drive/u/\d+/folders/([a-zA-Z0-9_-]{15,})"),
]

# Drive API folder mime — children of these are NOT recursed (v1 keeps it flat).
FOLDER_MIME = "application/vnd.google-apps.folder"

# How many pages (of 1000 each) we'll fetch before giving up. Hard ceiling
# against runaway enumeration on absurdly large folders.
MAX_PAGES = 5


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: Optional[int]


class DriveFolderError(RuntimeError):
    pass


def extract_drive_folder_id(url: str) -> Optional[str]:
    for pat in _FOLDER_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def is_drive_folder_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if "drive.google.com" not in host:
        return False
    return "/drive/folders/" in url or "/folders/" in url


def build_file_view_url(file_id: str) -> str:
    """Canonical "view" URL for a Drive file id so the existing single-file
    downloader can pick it up via its file-id regex."""
    return f"https://drive.google.com/file/d/{file_id}/view"


async def list_drive_folder(folder_id: str) -> list[DriveFile]:
    """List immediate children of a public Drive folder.

    Raises DriveFolderError with a user-friendly message on any non-success.
    """
    api_key = (config.google_drive_api_key or "").strip()
    if not api_key:
        raise DriveFolderError(
            "Google Drive folder uploads require a GOOGLE_DRIVE_API_KEY env "
            "var. In Google Cloud Console, enable the Drive API and create "
            "an API key (APIs & Services → Credentials → Create API key). "
            "Restrict it to the Drive API."
        )

    base = "https://www.googleapis.com/drive/v3/files"
    out: list[DriveFile] = []
    page_token: Optional[str] = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        for page_num in range(1, MAX_PAGES + 1):
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,size)",
                "pageSize": "1000",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "key": api_key,
            }
            if page_token:
                params["pageToken"] = page_token
            r = await client.get(base, params=params)
            if r.status_code == 403:
                raise DriveFolderError(
                    "Drive API rejected the request (403). Either the folder "
                    "isn't shared as 'Anyone with the link can view', or the "
                    "GOOGLE_DRIVE_API_KEY doesn't have the Drive API enabled."
                )
            if r.status_code == 404:
                raise DriveFolderError(
                    "Folder not found. Check the link is correct and that "
                    "the folder hasn't been deleted."
                )
            if r.status_code >= 400:
                raise DriveFolderError(
                    f"Drive API error ({r.status_code}): {r.text[:240]}"
                )
            data = r.json()
            for f in data.get("files", []):
                size: Optional[int]
                try:
                    size = int(f["size"]) if "size" in f else None
                except (TypeError, ValueError):
                    size = None
                out.append(
                    DriveFile(
                        id=f["id"],
                        name=f.get("name", "untitled"),
                        mime_type=f.get("mimeType", "application/octet-stream"),
                        size=size,
                    )
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break
            if page_num == MAX_PAGES:
                log.warning(
                    "drive folder %s has >%d files; truncating",
                    folder_id, MAX_PAGES * 1000,
                )

    return out


__all__ = [
    "DriveFile",
    "DriveFolderError",
    "FOLDER_MIME",
    "build_file_view_url",
    "extract_drive_folder_id",
    "is_drive_folder_url",
    "list_drive_folder",
]
