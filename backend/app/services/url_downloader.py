"""Download a file from any pasteable URL — Google Drive, Dropbox, OneDrive
share link, or a plain HTTP/HTTPS URL.

The Assets Library uses this so the user can paste a cloud share link
instead of having to download the file locally first.

Design:
  - Always read bytes into memory (capped at MAX_BYTES) — keeps downstream
    code identical to the multipart-upload path.
  - Drive: handles the virus-scan interstitial automatically (the "Download
    anyway" form), no auth needed for "Anyone with the link" files.
  - Dropbox: rewrites `?dl=0` to `?dl=1` for direct-download.
  - OneDrive: tries the `?download=1` rewrite (works for "Anyone with the
    link can view" share URLs). SharePoint enterprise URLs are out of scope.
  - Generic: plain httpx GET with redirects.

Failure modes are surfaced with explicit messages so the UI can render them.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

log = logging.getLogger("archivist.url_downloader")

# Hard cap to protect against OOM. Veo source videos are tiny relative to
# this; characters/logos are kilobytes. Bump if you ever need bigger.
MAX_BYTES = 200 * 1024 * 1024  # 200 MB


class UrlDownloadError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------

_DRIVE_FILE_PATTERNS = [
    re.compile(r"/file/d/([a-zA-Z0-9_-]{20,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{20,})"),
]


def _extract_drive_file_id(url: str) -> Optional[str]:
    for pat in _DRIVE_FILE_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def _is_drive_folder(url: str) -> bool:
    return "/drive/folders/" in url


def _filename_from_content_disposition(cd: str | None) -> Optional[str]:
    if not cd:
        return None
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="([^"]+)"', cd)
    if m:
        return m.group(1)
    m = re.search(r"filename=([^;]+)", cd)
    if m:
        return m.group(1).strip().strip('"')
    return None


def _filename_from_path(url: str) -> Optional[str]:
    try:
        path = urlparse(url).path
        name = path.rsplit("/", 1)[-1]
        return name or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-provider downloaders
# ---------------------------------------------------------------------------

async def _download_drive(url: str) -> tuple[bytes, Optional[str], str]:
    if _is_drive_folder(url):
        raise UrlDownloadError(
            "That's a Google Drive FOLDER, not a file. Paste a link to a "
            "single file (right-click in Drive → Share → Copy link)."
        )
    file_id = _extract_drive_file_id(url)
    if not file_id:
        raise UrlDownloadError(
            "Could not extract a Drive file id from that URL. The link must "
            "point to a single file and look like "
            "https://drive.google.com/file/d/<ID>/view"
        )

    # Drive's stable download endpoint
    base = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download"}

    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        r = await client.get(base, params=params)
        if r.status_code == 200 and not (
            (r.headers.get("content-type", "").startswith("text/html"))
            and b"<form" in r.content[:4096]
        ):
            # Direct hit
            return _finalize_response(r, fallback_name=f"drive_{file_id}")

        # Either the virus-scan interstitial OR an error page — try to parse
        # the confirm token + uuid out of the HTML form, then retry.
        text = r.text if r.headers.get("content-type", "").startswith("text/html") else ""
        uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', text)
        confirm_m = re.search(r'name="confirm"\s+value="([^"]+)"', text)
        if uuid_m and confirm_m:
            r = await client.get(
                base,
                params={
                    **params,
                    "uuid": uuid_m.group(1),
                    "confirm": confirm_m.group(1),
                },
            )
            if r.status_code == 200 and not r.headers.get("content-type", "").startswith("text/html"):
                return _finalize_response(r, fallback_name=f"drive_{file_id}")

        # Failed. Most likely cause: file isn't publicly shared.
        raise UrlDownloadError(
            "Couldn't download from Drive. Make sure the file's sharing is set "
            "to 'Anyone with the link can view' (in Drive: right-click → Share "
            "→ change 'Restricted' to 'Anyone with the link')."
        )


async def _download_dropbox(url: str) -> tuple[bytes, Optional[str], str]:
    p = urlparse(url)
    qs = parse_qs(p.query)
    qs["dl"] = ["1"]  # force direct download
    direct = urlunparse(p._replace(query=urlencode({k: v[0] for k, v in qs.items()})))
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        r = await client.get(direct)
        if r.status_code != 200:
            raise UrlDownloadError(
                f"Dropbox download failed ({r.status_code}). The link must be a "
                f"single-file share with public visibility."
            )
        return _finalize_response(r, fallback_name=_filename_from_path(url) or "dropbox_file")


async def _download_onedrive(url: str) -> tuple[bytes, Optional[str], str]:
    """Best-effort. OneDrive personal share URLs support `?download=1`.
    SharePoint enterprise URLs need OAuth — surface a clear error if it fails."""
    sep = "&" if "?" in url else "?"
    direct = f"{url}{sep}download=1"
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        r = await client.get(direct)
        if r.status_code != 200 or r.headers.get("content-type", "").startswith("text/html"):
            raise UrlDownloadError(
                "OneDrive download failed. Make sure the file is shared with "
                "'Anyone with the link'. SharePoint enterprise links aren't "
                "supported (would need OAuth)."
            )
        return _finalize_response(r, fallback_name=_filename_from_path(url) or "onedrive_file")


async def _download_generic(url: str) -> tuple[bytes, Optional[str], str]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        r = await client.get(url)
        if r.status_code != 200:
            raise UrlDownloadError(f"Download failed ({r.status_code}): {r.text[:240]}")
        return _finalize_response(r, fallback_name=_filename_from_path(url) or "downloaded_file")


def _finalize_response(r: httpx.Response, *, fallback_name: str) -> tuple[bytes, Optional[str], str]:
    data = r.content
    if len(data) > MAX_BYTES:
        raise UrlDownloadError(
            f"File is larger than the {MAX_BYTES // 1024 // 1024} MB cap "
            f"({len(data) // 1024 // 1024} MB). Reduce size or raise MAX_BYTES."
        )
    mime = (r.headers.get("content-type") or "").split(";")[0].strip() or None
    name = _filename_from_content_disposition(r.headers.get("content-disposition")) or fallback_name
    return data, mime, name


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def download_from_url(url: str) -> tuple[bytes, Optional[str], str]:
    """Download bytes from any pasteable URL. Returns (data, mime, filename).

    Raises UrlDownloadError with a user-friendly message on failure.
    """
    url = (url or "").strip()
    if not url:
        raise UrlDownloadError("URL is empty")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise UrlDownloadError("URL must start with http:// or https://")

    host = (urlparse(url).hostname or "").lower()
    log.info("downloading from %s", host)

    try:
        if "drive.google.com" in host or "googleusercontent.com" in host:
            return await _download_drive(url)
        if "dropbox.com" in host:
            return await _download_dropbox(url)
        if "1drv.ms" in host or "onedrive.live.com" in host or "sharepoint.com" in host:
            return await _download_onedrive(url)
        return await _download_generic(url)
    except UrlDownloadError:
        raise
    except httpx.HTTPError as e:
        raise UrlDownloadError(f"Network error: {e}") from e
    except Exception as e:
        log.exception("download_from_url failed for %s", url)
        raise UrlDownloadError(f"Download failed: {e}") from e


__all__ = ["download_from_url", "UrlDownloadError", "MAX_BYTES"]
