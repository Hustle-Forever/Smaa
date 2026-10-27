"""
Host calendar-booked media on a PUBLIC bucket for Instagram publishing.

Producer Roadmap, Step 4 (Calendar) → Step 5 (Approval). When a finished asset is
booked into the calendar, we copy its bytes into a dedicated PUBLIC bucket
(``config.approved_media_bucket``, default ``approved-instagram``) on the
configured object store (S3/MinIO behind ``S3_PUBLIC_BASE_URL``, or Supabase)
and record the permanent public URL on the scheduled-post row. At approval time
Instagram fetches the media from that URL.

Why a separate bucket: it's the staging area for "approved for Instagram" media —
only assets the user actually scheduled land here, kept apart from the working
``producer-assets`` library. The public URL never expires and needs no Cloudflare
tunnel / PUBLIC_MEDIA_BASE_URL.

SELF-GATED: ``configured()`` reports whether public hosting is usable; callers
treat a failure as "media not hosted" (the post still queues, approval just reports
why), never an exception that breaks scheduling.
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from ..config import config
from . import asset_storage

log = logging.getLogger("archivist.approved_media")

# Instagram image publishing accepts JPEG ONLY (PNG/WebP/GIF are rejected with
# subcode 2207052) and wants the width in 320–1440px. We transcode every image
# to a clean JPEG within that range before hosting.
_IG_MAX_EDGE = 1440
# Feed images must sit between 4:5 (tallest) and 1.91:1 (widest); anything
# outside is refused with "aspect ratio is not supported", so a 9:16 poster
# could never post. We pad rather than crop: nothing on the poster is lost.
_IG_MIN_RATIO = 4 / 5
_IG_MAX_RATIO = 1.91


def bucket_name() -> str:
    return config.approved_media_bucket


def _downscale(img, *, edge: int = _IG_MAX_EDGE):
    """Shrink so the long edge is <= `edge`. The short side rounds UP, which
    only ever moves the ratio towards 1:1, so a fitted image stays in range."""
    import math

    w, h = img.size
    if max(w, h) <= edge:
        return img
    if w >= h:
        return img.resize((edge, max(1, math.ceil(h * edge / w))))
    return img.resize((max(1, math.ceil(w * edge / h)), edge))


def _fit_ratio(img, target: float | None = None):
    """Pad `img` to Instagram's allowed shape: `target` (w/h) when given, else
    the nearest ratio inside 4:5..1.91:1. The padding is a blurred, slightly
    dimmed copy of the image, so the bars read as background, not as a frame."""
    import math

    from PIL import ImageEnhance, ImageFilter, ImageOps

    w, h = img.size
    ratio = w / h
    want = target or min(max(ratio, _IG_MIN_RATIO), _IG_MAX_RATIO)
    if abs(ratio - want) < 0.005:
        return img
    # Round the added side UP so the result never lands just outside the range.
    size = (math.ceil(h * want), h) if ratio < want else (w, math.ceil(w / want))
    bg = ImageOps.fit(img, size).filter(ImageFilter.GaussianBlur(max(size) / 30))
    bg = ImageEnhance.Brightness(bg).enhance(0.85)
    bg.paste(img, ((size[0] - w) // 2, (size[1] - h) // 2))
    return bg


def _to_jpeg(data: bytes, *, ratio: float | None = None) -> bytes:
    """Transcode arbitrary image bytes to an Instagram-acceptable JPEG.

    - flattens transparency onto white (JPEG has no alpha channel)
    - pads to an allowed aspect ratio (`ratio` when given, see _fit_ratio)
    - downscales the long edge to <= 1440px (IG's max width)
    """
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(data))
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    # Downscale first so the blur works on fewer pixels; padding can push the
    # long edge back over the limit, hence the second pass.
    small = _downscale(img)
    fitted = _fit_ratio(small, ratio)
    if fitted is not small:
        log.info("approved_media: padded a %dx%d image to %dx%d for Instagram",
                 *small.size, *fitted.size)
    img = _downscale(fitted)

    out = BytesIO()
    img.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()


def configured() -> tuple[bool, str]:
    """(usable, reason). reason is empty when usable. Whichever object store is
    configured decides (asset_storage.public_hosting_configured)."""
    return asset_storage.public_hosting_configured()


def _unreadable(e: Exception) -> asset_storage.AssetStorageError:
    """The store failed to answer, which is not the same as "the object is
    gone". Callers raise this instead of FileNotFoundError so the campaign
    poller retries the item: it fails a FileNotFoundError item for good."""
    if isinstance(e, asset_storage.AssetStorageError):
        return e
    err = asset_storage.AssetStorageError(f"media storage unreadable: {e}")
    err.__cause__ = e
    return err


async def _read_media_bytes(
    *, storage_bucket: str | None, storage_path: str | None, local_path: str | None,
) -> bytes | None:
    """Read generation-media bytes from the object store, else the local copy.

    A row can carry both (the backfill keeps local_path), so a failed or empty
    bucket read falls through to the local file instead of losing the post.
    None only when the media is really gone. A store that could not be read
    (down, or its circuit breaker open) with no local copy to use raises
    AssetStorageError instead."""
    unreadable: Exception | None = None
    if storage_bucket and storage_path:
        try:
            data = await asset_storage.download_bytes(
                storage_bucket=storage_bucket, storage_path=storage_path,
            )
            if data:
                return data
        except asset_storage.AssetNotFoundError as e:
            log.warning(
                "approved_media: %s/%s is missing, trying the local copy: %s",
                storage_bucket, storage_path, e,
            )
        except Exception as e:  # noqa: BLE001
            unreadable = e
            log.warning(
                "approved_media: reading %s/%s failed, trying the local copy: %s",
                storage_bucket, storage_path, e,
            )
    if local_path:
        try:
            p = asset_storage.local_to_abs(local_path)
            if p.exists():
                return p.read_bytes()
        except Exception:  # noqa: BLE001
            pass
    if unreadable is not None:
        raise _unreadable(unreadable)
    return None


async def _composition_bytes(
    what: str, local: Path, *, storage_bucket: str | None, storage_path: str | None,
) -> bytes:
    """A finished render's MP4: the local file, else the bucket backup the
    composition poller made (persist_local_file), so a redeploy that wiped the
    render from disk does not stop the post. FileNotFoundError when neither
    copy exists, AssetStorageError when the backup could not be read."""
    if local.exists():
        return local.read_bytes()
    if storage_bucket and storage_path:
        try:
            data = await asset_storage.download_bytes(
                storage_bucket=storage_bucket, storage_path=storage_path,
            )
            if data:
                return data
        except asset_storage.AssetNotFoundError as e:
            log.warning(
                "approved_media: %s backup %s/%s is missing: %s",
                what, storage_bucket, storage_path, e,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "approved_media: %s backup %s/%s unreadable: %s",
                what, storage_bucket, storage_path, e,
            )
            raise _unreadable(e)
    raise FileNotFoundError(f"{what}: composition missing on disk ({local})")


async def _resolve_media(tool: str, ref_id: int) -> tuple[bytes, str]:
    """Return (file bytes, mime type) for a finished asset, reading from wherever
    that tool stores its output (the object store, with a local fallback)."""
    if tool == "poster":
        from . import yas_local_store as ystore

        gen = await ystore.get(ref_id)
        if not gen:
            raise FileNotFoundError(f"poster #{ref_id}: not found")
        data = await _read_media_bytes(
            storage_bucket=gen.get("storage_bucket"),
            storage_path=gen.get("storage_path"),
            local_path=gen.get("local_path"),
        )
        if data is None:
            raise FileNotFoundError(f"poster #{ref_id}: file not retrievable")
        return data, (gen.get("mime_type") or "image/png")

    if tool == "animation":
        from . import yas_animation_store as anim_store

        rec = await anim_store.get(ref_id)
        if not rec:
            raise FileNotFoundError(f"animation #{ref_id}: not found")
        data = await _read_media_bytes(
            storage_bucket=rec.get("video_storage_bucket"),
            storage_path=rec.get("video_storage_path"),
            local_path=rec.get("video_local_path"),
        )
        if data is None:
            raise FileNotFoundError(f"animation #{ref_id}: file not ready")
        return data, "video/mp4"

    if tool == "poster_human":
        from ..db import get_conn

        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT local_path, storage_bucket, storage_path, mime_type "
                    "FROM yas_generations WHERE id=%s",
                    (ref_id,),
                )
                row = await cur.fetchone()
        if not row:
            raise FileNotFoundError(f"poster_human #{ref_id}: generation not found")
        mime = row.get("mime_type") or "image/png"
        if row.get("local_path"):
            p = Path(row["local_path"])
            if not p.is_absolute():
                p = config.asset_root.parent / p
            if p.exists():
                return p.read_bytes(), mime
        # Fall back to the stored copy, read straight from the object store: a
        # signed URL would have to be fetched back through the public origin.
        if row.get("storage_bucket") and row.get("storage_path"):
            try:
                data = await asset_storage.download_bytes(
                    storage_bucket=row["storage_bucket"],
                    storage_path=row["storage_path"],
                )
                if data:
                    return data, mime
            except asset_storage.AssetNotFoundError as e:
                log.warning(
                    "approved_media: poster_human #%s stored copy is missing: %s", ref_id, e,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "approved_media: poster_human #%s stored copy unreadable: %s", ref_id, e,
                )
                raise _unreadable(e)
        raise FileNotFoundError(f"poster_human #{ref_id}: no retrievable file")

    if tool == "talking_head_v2":
        from . import yas_talking_head_v2 as th_v2

        run = await th_v2._fetch_run_row(ref_id)
        md = (run or {}).get("metadata") or {}
        comp_rel = md.get("composition_path")
        if not comp_rel:
            raise FileNotFoundError(f"talking_head_v2 #{ref_id}: composition not ready")
        # Background music on and made for this reel: the music version is
        # what the Final step and Preview showed, so it is what is posted.
        from . import th_reel_music

        music = await th_reel_music.current_bytes(md)
        if music is not None:
            return music, "video/mp4"
        data = await _composition_bytes(
            f"talking_head_v2 #{ref_id}", th_v2._abs_from_rel(comp_rel),
            storage_bucket=md.get("composition_storage_bucket"),
            storage_path=md.get("composition_storage_path"),
        )
        return data, "video/mp4"

    if tool in ("video_editor", "broll"):
        # A B-roll is a Video-Editor project (the b_roll preset): its ref_id is
        # the project id and its post is the project's export.
        from . import yas_video_editor as ve

        row = await ve._fetch_row(ref_id)
        render = (ve._video_edit(row["metadata"]) or {}).get("render") or {}
        comp_rel = render.get("composition_path")
        if render.get("composition_status") != ve.COMP_READY or not comp_rel:
            raise FileNotFoundError(f"{tool} #{ref_id}: export not ready")
        data = await _composition_bytes(
            f"{tool} #{ref_id}", ve._abs_from_rel(comp_rel),
            storage_bucket=render.get("composition_storage_bucket"),
            storage_path=render.get("composition_storage_path"),
        )
        return data, "video/mp4"

    if tool == "general_video":
        # General AI Video clips live in their OWN table (yas_general_video_store),
        # not as video_editor projects — so they need their own resolver branch.
        from . import yas_general_video_store as gv_store

        rec = await gv_store.get(ref_id)
        if not rec:
            raise FileNotFoundError(f"general_video #{ref_id}: not found")
        data = await _read_media_bytes(
            storage_bucket=rec.get("video_storage_bucket"),
            storage_path=rec.get("video_storage_path"),
            local_path=rec.get("video_local_path"),
        )
        if data is None:
            raise FileNotFoundError(f"general_video #{ref_id}: file not ready")
        return data, "video/mp4"

    raise ValueError(f"unknown tool '{tool}'")


async def upload_for_post(*, tool: str, ref_id: int, media_type: str) -> str:
    """Copy a finished asset into the public bucket; return its permanent public URL.

    Raises AssetStorageError if public hosting isn't configured, the source
    could not be read from the store or the upload fails (all worth a retry),
    FileNotFoundError if the media isn't retrievable, or ValueError on an
    unknown tool. Callers wrap this in try/except and degrade gracefully.
    """
    url, _ratio = await _host(tool=tool, ref_id=ref_id, media_type=media_type)
    return url


async def _host(
    *, tool: str, ref_id: int, media_type: str, ratio: float | None = None,
) -> tuple[str, float | None]:
    """upload_for_post's body. Also returns the hosted image's w/h (None for a
    video) and pads an image to `ratio` when given, which carousels need."""
    ok, reason = configured()
    if not ok:
        raise asset_storage.AssetStorageError(reason)

    data, mime = await _resolve_media(tool, ref_id)
    kind = (media_type or "").lower()
    hosted_ratio = None
    if kind == "image":
        from io import BytesIO

        from PIL import Image

        # Instagram rejects non-JPEG images and out-of-range shapes.
        data = _to_jpeg(data, ratio=ratio)
        w, h = Image.open(BytesIO(data)).size
        hosted_ratio = w / h
        mime, ext = "image/jpeg", "jpg"
    else:
        mime, ext = (mime or "video/mp4"), "mp4"
    # The bucket is anonymous-readable and tool/ref_id are guessable, so 128
    # random bits are what keep a booked but unposted file private.
    key = f"{tool}/{ref_id}_ig_{secrets.token_hex(16)}.{ext}"
    bucket = bucket_name()

    await asset_storage.ensure_public_bucket(bucket)
    url = await asset_storage.upload_public(bucket=bucket, path=key, data=data, mime=mime)
    log.info("approved_media: hosted %s #%s (%d bytes) → %s", tool, ref_id, len(data), url)
    return url, hosted_ratio


async def upload_carousel(items: list[dict]) -> list[dict]:
    """Host every carousel slide on the public bucket. `items` =
    [{tool, ref_id, media_type}]. Returns the same dicts with a `media_url`
    added. Raises on the first failure so the caller can degrade the whole
    carousel gracefully.

    Instagram crops every slide to the FIRST image's shape, so each later image
    is padded to that shape instead of losing its edges."""
    out: list[dict] = []
    first_ratio: float | None = None
    try:
        for it in items:
            url, ratio = await _host(
                tool=it["tool"], ref_id=int(it["ref_id"]),
                media_type=(it.get("media_type") or "image"),
                ratio=first_ratio,
            )
            if first_ratio is None and ratio is not None:
                first_ratio = ratio
            out.append({
                "tool": it["tool"],
                "ref_id": int(it["ref_id"]),
                "media_type": it.get("media_type") or "image",
                "media_url": url,
            })
    except Exception:
        # The caller never sees the slides hosted so far, and a retry hosts
        # them again under new keys, so drop them rather than leave them public.
        await asset_storage.delete_public_urls([o["media_url"] for o in out])
        raise
    return out


__all__ = ["bucket_name", "configured", "upload_for_post", "upload_carousel"]
