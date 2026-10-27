"""Phase 2 — download IG media to disk + create `assets` + `instagram_post_assets` rows.

Per post:
  - IMAGE → 1 asset (asset_type='image')
  - VIDEO → 1 asset (asset_type='instagram' — the existing video pipeline
            already includes this in its filter set)
  - CAROUSEL_ALBUM → N assets, one per child (image-or-video each, indexed)

CDN urls from Meta expire ~24h, so this phase MUST run shortly after discover.
If `force_re_download` is set, we delete the prior link rows + asset rows for
this post and start over (caller is expected to have re-run discover first to
pick up fresh CDN urls).

Idempotent: skips (post, child_index) pairs that already have an
instagram_post_assets link row whose asset has `download_status='downloaded'`.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import httpx
from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services.storage import asset_local_path, ensure_dir, relativise
from ....utils.hashing import url_hash as compute_url_hash
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult
from .._common import ensure_placeholder_page_id

log = logging.getLogger("archivist.phase.ig_download")


def _ig_url_hash(ig_media_id: str, child_index: int) -> str:
    """Stable per-(post, child) hash so re-discovery never duplicates."""
    return compute_url_hash(f"ig:{ig_media_id}:{child_index}")


def _ig_asset_type(media_type: str) -> str:
    """Map IG media_type to the existing `assets.asset_type` enum.

    IMAGE → 'image' so it lands in image_phase's pending queue.
    VIDEO → 'instagram' so it lands in video_phase's pending queue (the
    asset_type CHECK already allows 'instagram', and video.py filters on it).
    """
    return "image" if media_type == "IMAGE" else "instagram"


def _guess_ext_from_url(url: str, media_type: str) -> str:
    path = Path(urlparse(url).path).name
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "webp", "heic", "mp4", "mov", "m4v"}:
        return suffix if suffix != "jpeg" else "jpg"
    return "jpg" if media_type == "IMAGE" else "mp4"


def _image_dims_and_phash(path: Path) -> tuple[int | None, int | None, str | None]:
    """Pulled from website_ingest.phases.download — pHash feeds image.py's
    dedup pass so IG images can be deduped against website images too."""
    try:
        from PIL import Image
        try:
            import pillow_avif  # noqa: F401
        except ImportError:
            pass
        import imagehash
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            return rgb.width, rgb.height, str(imagehash.phash(rgb))
    except Exception:
        log.debug("dims/phash failed for %s", path)
        return None, None, None


async def _download_one(url: str, dest: Path) -> tuple[int, str | None]:
    """Async streaming download. Returns (bytes, mime_type)."""
    ensure_dir(dest.parent)
    async with httpx.AsyncClient(
        headers={"User-Agent": config.user_agent},
        timeout=config.request_timeout * 3,
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").split(";")[0].strip() or None
            written = 0
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(64 * 1024):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
    return written, ctype


async def _link_exists(post_id: int, child_index: int) -> bool:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                  FROM instagram_post_assets ipa
                  JOIN assets a ON a.id = ipa.asset_id
                 WHERE ipa.post_id=%s AND ipa.child_index=%s
                   AND a.download_status='downloaded'
                """,
                (post_id, child_index),
            )
            return (await cur.fetchone()) is not None


async def _detach_existing_links(post_id: int) -> int:
    """For force_re_download: delete link rows + their assets for this post."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH gone AS (
                  DELETE FROM instagram_post_assets WHERE post_id=%s
                  RETURNING asset_id
                )
                DELETE FROM assets WHERE id IN (SELECT asset_id FROM gone)
                """,
                (post_id,),
            )
            return cur.rowcount or 0


async def _insert_asset_and_link(
    *,
    post_id: int,
    child_index: int,
    ig_media_id: str,
    ig_child_media_id: str | None,
    media_url: str,
    media_type: str,  # 'IMAGE' | 'VIDEO' (per-child)
    page_id: int,
) -> str:
    """Returns 'downloaded' | 'failed'."""
    asset_type = _ig_asset_type(media_type)
    ext = _guess_ext_from_url(media_url, media_type)
    uh = _ig_url_hash(ig_media_id, child_index)
    dest = asset_local_path(asset_type, uh, ext)

    try:
        bytes_, mime = await _download_one(media_url, dest)
    except Exception as e:
        log.warning("IG download failed post=%d child=%d: %s", post_id, child_index, e)
        return "failed"

    # phash for images (feeds image_phase's dedup)
    width = height = phash = None
    if asset_type == "image":
        width, height, phash = await asyncio.to_thread(_image_dims_and_phash, dest)

    rel = relativise(dest)
    md = {
        "source": "instagram",
        "ig_media_id": ig_media_id,
        "ig_child_media_id": ig_child_media_id,
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO assets
                  (page_id, asset_type, source_url, canonical_url, url_hash,
                   perceptual_hash, local_path, width, height, bytes, mime_type,
                   download_status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'downloaded', %s)
                RETURNING id
                """,
                (
                    page_id, asset_type, media_url, media_url, uh,
                    phash, rel, width, height, bytes_, mime or mimetypes.guess_type(rel)[0],
                    Jsonb(md),
                ),
            )
            row = await cur.fetchone()
            asset_id = int(row["id"])

            await cur.execute(
                """
                INSERT INTO instagram_post_assets
                  (post_id, asset_id, child_index, ig_child_media_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (post_id, child_index) DO UPDATE
                  SET asset_id = EXCLUDED.asset_id,
                      ig_child_media_id = EXCLUDED.ig_child_media_id
                """,
                (post_id, asset_id, child_index, ig_child_media_id),
            )
    return "downloaded"


async def _select_pending_posts() -> list[dict]:
    """All instagram_posts where at least one expected asset isn't downloaded yet."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, ig_media_id, media_type, children_count, metadata
                  FROM instagram_posts
                 ORDER BY timestamp_posted DESC NULLS LAST, id DESC
                """
            )
            return await cur.fetchall()


async def run(ctx: PhaseContext) -> PhaseResult:
    ig_meta = (ctx.metadata or {}).get("instagram") or {}
    force_re_download: bool = bool(ig_meta.get("force_re_download"))
    page_id = await ensure_placeholder_page_id()

    posts = await _select_pending_posts()
    if not posts:
        return PhaseResult(name="download", total=0)

    log.info("[run=%s] download: %d posts to check", ctx.run_id, len(posts))

    downloaded = 0
    skipped = 0
    failed = 0
    total_targets = 0

    for post in posts:
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("ig_download cancelled mid-batch")

        post_id = int(post["id"])
        ig_id = post["ig_media_id"]
        media_type = post["media_type"]
        md = post.get("metadata") or {}

        if force_re_download:
            removed = await _detach_existing_links(post_id)
            if removed:
                log.info("force_re_download: detached %d existing assets for post #%d", removed, post_id)

        # Build the list of (child_index, child_media_id, media_url, media_type) to fetch
        targets: list[tuple[int, str | None, str | None, str]] = []
        if media_type == "CAROUSEL_ALBUM":
            for i, child in enumerate(md.get("children_raw") or []):
                targets.append((
                    i,
                    str(child.get("id") or "") or None,
                    child.get("media_url"),
                    child.get("media_type") or "IMAGE",
                ))
        else:
            targets.append((0, None, md.get("media_url"), media_type))

        total_targets += len(targets)

        for child_index, ig_child_id, media_url, child_media_type in targets:
            if not media_url:
                log.warning("post #%d child %d has no media_url — skip", post_id, child_index)
                skipped += 1
                continue

            if not force_re_download and await _link_exists(post_id, child_index):
                skipped += 1
                continue

            outcome = await _insert_asset_and_link(
                post_id=post_id,
                child_index=child_index,
                ig_media_id=ig_id,
                ig_child_media_id=ig_child_id,
                media_url=media_url,
                media_type=child_media_type,
                page_id=page_id,
            )
            if outcome == "downloaded":
                downloaded += 1
            else:
                failed += 1

        if ctx.report_progress:
            ctx.report_progress("download", downloaded + skipped + failed, total_targets)

    log.info(
        "[run=%s] download done: %d new, %d already-had, %d failed (over %d targets)",
        ctx.run_id, downloaded, skipped, failed, total_targets,
    )
    return PhaseResult(
        name="download",
        processed=downloaded,
        skipped=skipped,
        failed=failed,
        total=total_targets,
    )
