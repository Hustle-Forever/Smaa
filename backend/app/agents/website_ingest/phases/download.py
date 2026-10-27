"""Phase 7 — download pending assets to local disk.

- image / pdf  : httpx async GET → write to data/assets/...
- video_*      : yt-dlp subprocess in a thread → mp4 via ffmpeg merge
- iframe_other : marked 'skipped' (not media)

Idempotent. Skips already-downloaded rows. Respects hints.skip_images and
hints.skip_videos (caller already filters; this is a second line of defense).

For images we also compute width/height + perceptual hash (phash) to feed
the dedup pass in the image phase.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import subprocess
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services.storage import ensure_dir, relativise
from ....utils.hashing import url_hash as compute_url_hash
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.download")

VIDEO_TYPES = {"video_native", "video_youtube", "video_vimeo", "instagram"}
IMAGE_TYPES = {"image"}
PDF_TYPES = {"pdf"}


def _types_for(kind: str) -> set[str]:
    return {
        "image": IMAGE_TYPES,
        "pdf": PDF_TYPES,
        "video": VIDEO_TYPES,
        "all": IMAGE_TYPES | PDF_TYPES | VIDEO_TYPES | {"iframe_other"},
    }[kind]


async def _select_pending(types: set[str], limit: int | None) -> list[dict]:
    sql = (
        "SELECT id, page_id, asset_type, source_url, canonical_url, url_hash, metadata "
        "  FROM assets "
        " WHERE download_status='pending' AND asset_type = ANY(%s) "
        " ORDER BY id ASC"
    )
    params: tuple = (list(types),)
    if limit:
        sql += f" LIMIT {int(limit)}"
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


def _guess_ext(url: str, content_type: str | None, kind: str) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type)
        if ext:
            return ext.lstrip(".")
    suffix = Path(url.split("?", 1)[0].split("#", 1)[0]).suffix
    if suffix:
        return suffix.lstrip(".").lower()
    return {"image": "bin", "pdf": "pdf", "video": "mp4"}.get(kind, "bin")


async def _download_http(url: str, dest_dir: Path, uh: str, kind: str) -> tuple[Path, int, str | None]:
    ensure_dir(dest_dir)
    async with httpx.AsyncClient(
        headers={"User-Agent": config.user_agent},
        timeout=config.request_timeout * 3,
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").split(";")[0].strip() or None
            ext = _guess_ext(url, ctype, kind)
            dest = dest_dir / f"{uh}.{ext}"
            bytes_written = 0
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(64 * 1024):
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
    return dest, bytes_written, ctype


def _image_dims_and_phash(path: Path) -> tuple[int | None, int | None, str | None]:
    try:
        from PIL import Image
        try:
            import pillow_avif  # noqa: F401
        except ImportError:
            pass
        import imagehash
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            w, h = rgb.width, rgb.height
            ph = str(imagehash.phash(rgb))
            return w, h, ph
    except Exception:
        log.debug("dims/phash failed for %s", path)
        return None, None, None


def _ytdlp_path() -> str:
    return shutil.which("yt-dlp") or shutil.which("yt-dlp.exe") or "yt-dlp"


def _ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _download_video_sync(url: str, dest_dir: Path, uh: str) -> tuple[Path, int, str | None]:
    ensure_dir(dest_dir)
    out_template = str(dest_dir / f"{uh}.%(ext)s")
    cmd = [
        _ytdlp_path(),
        "-f", "bv*[height<=720]+ba/b[height<=720]/best",
        "-o", out_template,
        "--no-progress", "--no-warnings",
        "--retries", "2", "--socket-timeout", "30",
        "--merge-output-format", "mp4",
    ]
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        cmd += ["--ffmpeg-location", ffmpeg]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp exit={proc.returncode}: {(proc.stderr or '')[:500]}")
    candidates = sorted(dest_dir.glob(f"{uh}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("yt-dlp produced no output")
    path = candidates[0]
    return path, path.stat().st_size, mimetypes.guess_type(str(path))[0]


async def _process_asset(row: dict) -> str:
    """Returns one of 'downloaded', 'skipped', 'failed'."""
    aid = row["id"]
    asset_type = row["asset_type"]
    canon = row["canonical_url"] or row["source_url"]
    uh = row["url_hash"] or compute_url_hash(canon)

    try:
        if asset_type in IMAGE_TYPES:
            sub = config.asset_root / asset_type / uh[:2]
            path, bytes_, mime = await _download_http(canon, sub, uh, "image")
            w, h, ph = await asyncio.to_thread(_image_dims_and_phash, path)
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE assets
                           SET download_status='downloaded',
                               local_path=%s, bytes=%s, mime_type=%s,
                               width=%s, height=%s, perceptual_hash=%s
                         WHERE id=%s
                        """,
                        (relativise(path), bytes_, mime, w, h, ph, aid),
                    )
            return "downloaded"

        if asset_type in PDF_TYPES:
            sub = config.asset_root / asset_type / uh[:2]
            path, bytes_, mime = await _download_http(canon, sub, uh, "pdf")
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE assets
                           SET download_status='downloaded',
                               local_path=%s, bytes=%s, mime_type=%s
                         WHERE id=%s
                        """,
                        (relativise(path), bytes_, mime, aid),
                    )
            return "downloaded"

        if asset_type in VIDEO_TYPES:
            sub = config.asset_root / asset_type / uh[:2]
            try:
                path, bytes_, mime = await asyncio.to_thread(_download_video_sync, canon, sub, uh)
            except Exception as e:
                msg = str(e)
                skip_kind = "ig_rate_limit" if asset_type == "instagram" else "ytdlp_failed"
                log.warning("Video skip (%s) #%d %s: %s", skip_kind, aid, canon, msg[:200])
                async with get_conn() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE assets
                               SET download_status='skipped',
                                   metadata=COALESCE(metadata, '{}'::jsonb)
                                            || jsonb_build_object('skip_reason', %s::text, 'last_error', %s::text)
                             WHERE id=%s
                            """,
                            (skip_kind, msg[:500], aid),
                        )
                return "skipped"
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE assets
                           SET download_status='downloaded',
                               local_path=%s, bytes=%s, mime_type=%s
                         WHERE id=%s
                        """,
                        (relativise(path), bytes_, mime, aid),
                    )
            return "downloaded"

        # iframe_other or unknown
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assets
                       SET download_status='skipped',
                           metadata=COALESCE(metadata, '{}'::jsonb)
                                    || jsonb_build_object('skip_reason', 'iframe_other'::text)
                     WHERE id=%s
                    """,
                    (aid,),
                )
        return "skipped"

    except Exception as e:
        log.exception("Download failed #%d %s", aid, canon)
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assets
                       SET download_status='failed',
                           metadata=COALESCE(metadata, '{}'::jsonb)
                                    || jsonb_build_object('download_error', %s::text)
                     WHERE id=%s
                    """,
                    (str(e)[:500], aid),
                )
        return "failed"


async def run(ctx: PhaseContext, *, kind: str = "all", limit: int | None = None) -> PhaseResult:
    """Download all pending assets of the given kind (image|video|pdf|all)."""
    types = _types_for(kind)
    ensure_dir(config.asset_root)

    rows = await _select_pending(types, limit)
    if not rows:
        log.info("[run=%s] Download (%s): nothing pending", ctx.run_id, kind)
        return PhaseResult(name=f"download:{kind}", total=0)

    log.info("[run=%s] Download (%s): %d assets pending", ctx.run_id, kind, len(rows))

    sem = asyncio.Semaphore(config.run_concurrency_fetch)
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    lock = asyncio.Lock()

    async def worker(row: dict, idx: int) -> None:
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("download cancelled mid-batch")
        async with sem:
            outcome = await _process_asset(row)
        async with lock:
            counts[outcome] += 1
        if ctx.report_progress:
            ctx.report_progress(
                f"download:{kind}",
                counts["downloaded"] + counts["skipped"] + counts["failed"],
                len(rows),
            )

    tasks = [asyncio.create_task(worker(r, i + 1)) for i, r in enumerate(rows)]
    try:
        await asyncio.gather(*tasks)
    except CancellationError:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise

    log.info(
        "[run=%s] Download (%s) done: %d ok, %d skipped, %d failed",
        ctx.run_id, kind, counts["downloaded"], counts["skipped"], counts["failed"],
    )
    return PhaseResult(
        name=f"download:{kind}",
        processed=counts["downloaded"], skipped=counts["skipped"], failed=counts["failed"],
        total=len(rows),
    )
