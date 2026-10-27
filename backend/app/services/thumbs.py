"""Stored image derivatives: small WebP copies beside each bucket image.

Two sizes: a 480px `thumbs/` copy for grid and Library tiles, and a 1400px
`previews/` copy for the asset detail drawer. On S3 these are the ONLY small
versions there are, because MinIO has no image transformer (D13): without
them a Library tile downloads the whole ~1.3 MB poster and the drawer a
9-15 MB background, over a ~150 ms link to the server.

Moved out of routers/producer_assets.py so every image writer (yas_producer,
the video-edit agent, the backfill script) shares one implementation without
importing a router module. Writers that know they made an image call
`ensure_thumb_object`; everything else gets its derivative lazily through
`derivative_urls`, which serves what exists and queues the rest.

Derivatives live in the SAME bucket as the original. Same bucket matters: a
derivative IS the picture, so putting it in a public bucket would expose
exactly what keeping producer-assets private protects. Keys come from
`asset_storage.derivative_keys`, which is also what drops them when the
original is rewritten or deleted.
"""
from __future__ import annotations

import asyncio
import logging
import weakref

from . import asset_storage

log = logging.getLogger("archivist.thumbs")

THUMB_PREFIX = "thumbs"
# 480px box, WebP. Grid tiles render at ~250px, so 480 covers 2x displays.
THUMB_BOX = 480
THUMB_WEBP_QUALITY = 78
PREVIEW_PREFIX = "previews"
# The detail drawer is at most ~700px wide; 1400 covers 2x displays.
PREVIEW_BOX = 1400
PREVIEW_WEBP_QUALITY = 80

# Background generation at most this many at a time: PIL runs in threads on
# the same 4 vCPUs that render video.
_GEN_CONCURRENCY = 2
# HEADs per derivative_urls call: a Library page has a few dozen tiles.
_HEAD_CONCURRENCY = 16


def thumb_object_path(storage_path: str) -> str:
    """Derivative key for an original's bucket key. Deterministic, so a backfill
    and the upload hook agree without a lookup."""
    return f"{THUMB_PREFIX}/{storage_path}.webp"


def preview_object_path(storage_path: str) -> str:
    """The detail-drawer derivative's key, beside the thumbnail's."""
    return f"{PREVIEW_PREFIX}/{storage_path}.webp"


def _encode(raw: bytes, box: int, quality: int) -> bytes | None:
    """Downscale to a `box` px box and encode WebP. Returns None if the bytes
    aren't a decodable raster image. CPU-bound: call via asyncio.to_thread."""
    try:
        from PIL import Image, ImageOps
        from io import BytesIO

        with Image.open(BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im)
            # Flatten alpha onto white: WebP keeps alpha, but a P/LA source can
            # otherwise encode oddly.
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.mode else "RGB")
            im.thumbnail((box, box), Image.LANCZOS)
            out = BytesIO()
            im.save(out, format="WEBP", quality=quality, method=4)
            return out.getvalue()
    except Exception:
        return None


def make_thumb_bytes(raw: bytes) -> bytes | None:
    """The 480px tile copy. See _encode."""
    return _encode(raw, THUMB_BOX, THUMB_WEBP_QUALITY)


def make_preview_bytes(raw: bytes) -> bytes | None:
    """The 1400px drawer copy. See _encode."""
    return _encode(raw, PREVIEW_BOX, PREVIEW_WEBP_QUALITY)


_KINDS = {
    "thumb": (thumb_object_path, make_thumb_bytes),
    "preview": (preview_object_path, make_preview_bytes),
}


def is_raster_key(storage_path: str | None) -> bool:
    return bool(storage_path) and storage_path.lower().endswith(asset_storage.DERIVABLE_EXTS)


async def ensure_thumb_object(
    *, raw: bytes, storage_path: str | None, storage_bucket: str | None,
    mime: str | None,
) -> str | None:
    """Generate + store a 480px WebP derivative for a freshly uploaded image.

    Why a STORED derivative: S3 cannot resize at all, and on Supabase the batch
    signing endpoint (one round trip for a whole page) does not honour a
    `transform`, so a batch-signed original only gets RE-ENCODED, not resized.
    Resizing once at upload gives small bytes AND one signing round trip.

    Only runs when the original went to a bucket and is a raster image
    (`asset_storage.can_transform`). The derivative never falls back to local
    disk. Returns its bucket key (record it as attributes._thumb_path), or None;
    the caller then serves the original, which still works, just larger.
    Never raises.
    """
    if not (storage_bucket and storage_path):
        return None
    if not asset_storage.can_transform(mime):
        return None
    try:
        thumb = await asyncio.to_thread(make_thumb_bytes, raw)
        if not thumb:
            return None
        path = thumb_object_path(storage_path)
        if asset_storage.backend_name() == "s3":
            await asset_storage.put_s3_object(
                storage_bucket, path, data=thumb, mime="image/webp",
            )
            asset_storage.mark_derivative(storage_bucket, path)
            return path
        res = await asset_storage.upload(
            data=thumb, path=path, mime="image/webp", bucket=storage_bucket,
        )
        if res.get("storage_path"):
            return res["storage_path"]
        # The bucket write failed and upload() kept a local copy nobody records.
        if res.get("local_path"):
            try:
                asset_storage.local_to_abs(res["local_path"]).unlink(missing_ok=True)
            except OSError:
                pass
        return None
    except Exception:
        log.exception("[thumbs] derivative failed for %s/%s", storage_bucket, storage_path)
        return None


# ---------------------------------------------------------------------------
# Lazy derivatives (S3 only). Library tiles come from many writers and from
# rows the backfill moved; rather than teach each one to make copies, the read
# side serves the copies that exist and queues the missing ones. A tile is the
# full original once, then small from the next load on.
# ---------------------------------------------------------------------------
_inflight: set[tuple[str, str, str]] = set()
_tasks: set[asyncio.Task] = set()
# One semaphore per event loop: tests run each case in a fresh loop.
_gen_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _gen_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _gen_sems.get(loop)
    if sem is None:
        sem = _gen_sems[loop] = asyncio.Semaphore(_GEN_CONCURRENCY)
    return sem


async def ensure_derivative(bucket: str, storage_path: str, kind: str = "thumb") -> str | None:
    """Make `kind`'s derivative of bucket/storage_path unless it already exists;
    return its key, or None when the original isn't a usable raster image or the
    store failed. Reads the original from the store, never from local disk, so
    the copy always matches what the bucket serves. Never raises."""
    key_for, make = _KINDS[kind]
    if asset_storage.backend_name() != "s3" or not is_raster_key(storage_path):
        return None
    dkey = key_for(storage_path)
    try:
        if asset_storage.derivative_known(bucket, dkey) or await asset_storage.object_exists(
            bucket=bucket, path=dkey,
        ):
            asset_storage.mark_derivative(bucket, dkey)
            return dkey
        raw = await asset_storage.download_bytes(storage_bucket=bucket, storage_path=storage_path)
        out = await asyncio.to_thread(make, raw)
        if not out:
            log.info("[thumbs] %s/%s is not a raster image; no %s", bucket, storage_path, kind)
            return None
        await asset_storage.put_s3_object(bucket, dkey, data=out, mime="image/webp")
        asset_storage.mark_derivative(bucket, dkey)
        return dkey
    except Exception as e:  # noqa: BLE001
        log.warning("[thumbs] %s for %s/%s failed: %s", kind, bucket, storage_path, e)
        return None


def _queue(bucket: str, storage_path: str, kind: str) -> None:
    job = (bucket, storage_path, kind)
    if job in _inflight:
        return
    _inflight.add(job)

    async def _run() -> None:
        try:
            async with _gen_sem():
                await ensure_derivative(bucket, storage_path, kind)
        finally:
            _inflight.discard(job)

    task = asyncio.get_running_loop().create_task(_run())
    # Hold a reference until it finishes, or the loop may drop the task.
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def derivative_urls(
    bucket: str | None, paths: list[str], kind: str = "thumb", *, expires: int = 86400,
) -> dict[str, str]:
    """{original path: signed URL of its `kind` derivative} for every path whose
    derivative exists. Missing ones are queued for background generation and
    left out, so the caller keeps serving the original this time. S3 only (an
    empty dict elsewhere: Supabase resizes on the fly). Never raises."""
    if asset_storage.backend_name() != "s3" or not bucket:
        return {}
    key_for, _make = _KINDS[kind]
    want = {p: key_for(p) for p in set(paths) if is_raster_key(p)}
    unknown = [p for p, d in want.items() if not asset_storage.derivative_known(bucket, d)]
    if unknown:
        sem = asyncio.Semaphore(_HEAD_CONCURRENCY)

        async def _check(p: str) -> tuple[str, bool | None]:
            async with sem:
                try:
                    return p, await asset_storage.object_exists(bucket=bucket, path=want[p])
                except Exception:  # noqa: BLE001 - store trouble: neither known nor queued
                    return p, None

        for p, exists in await asyncio.gather(*(_check(p) for p in unknown)):
            if exists:
                asset_storage.mark_derivative(bucket, want[p])
            elif exists is False:
                _queue(bucket, p, kind)
    ready = {p: d for p, d in want.items() if asset_storage.derivative_known(bucket, d)}
    if not ready:
        return {}
    signed = await asset_storage.signed_render_urls(
        bucket, sorted(set(ready.values())), expires=expires,
    )
    return {p: signed[d] for p, d in ready.items() if d in signed}


__all__ = [
    "THUMB_PREFIX",
    "THUMB_BOX",
    "THUMB_WEBP_QUALITY",
    "PREVIEW_PREFIX",
    "PREVIEW_BOX",
    "thumb_object_path",
    "preview_object_path",
    "make_thumb_bytes",
    "make_preview_bytes",
    "is_raster_key",
    "ensure_thumb_object",
    "ensure_derivative",
    "derivative_urls",
]
