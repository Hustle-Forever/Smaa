"""Make the small WebP copies (thumbnails, previews) for every image already in
the S3 store, so the first Library or Assets visit is as fast as the next.

Run inside the backend container, whose WORKDIR is /app/backend:

    docker compose exec backend python -m scripts.warm_derivatives

The app makes missing copies by itself the first time a tile asks for one
(services/thumbs.derivative_urls); this only does it up front. Every raster
original in the producer-assets and producer-media buckets gets its 480px
thumbs/ copy, and producer-assets images also get their 1400px previews/ copy
for the asset drawer. Copies that already exist are skipped, so it is safe to
re-run. It only adds derivative objects: no original and no database row is
touched.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

# Same as run.py: keep the selector loop on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import config  # noqa: E402
from app.services import asset_storage, s3_backend, thumbs  # noqa: E402


def plan(keys: list[str], *, previews: bool) -> list[tuple[str, str]]:
    """(original key, kind) for every derivative an object listing calls for."""
    out: list[tuple[str, str]] = []
    for key in keys:
        if key.split("/", 1)[0] in asset_storage.DERIVATIVE_PREFIXES:
            continue
        if not thumbs.is_raster_key(key):
            continue
        out.append((key, "thumb"))
        if previews:
            out.append((key, "preview"))
    return out


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args(argv)

    if asset_storage.backend_name() != "s3":
        print("storage backend is not s3; nothing to do")
        return 2

    jobs: list[tuple[str, str, str]] = []
    for bucket, previews in (
        (config.producer_assets_bucket, True),
        (config.producer_media_bucket, False),
    ):
        keys = await s3_backend.list_keys(bucket)
        wanted = plan(keys, previews=previews)
        print(f"{bucket}: {len(keys)} object(s), {len(wanted)} derivative(s) wanted")
        jobs.extend((bucket, key, kind) for key, kind in wanted)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    made = skipped = failed = 0

    async def _one(bucket: str, key: str, kind: str) -> None:
        nonlocal made, skipped, failed
        async with sem:
            exists = await asset_storage.object_exists(
                bucket=bucket,
                path=(thumbs.thumb_object_path if kind == "thumb" else thumbs.preview_object_path)(key),
            )
            if exists:
                skipped += 1
                return
            if await thumbs.ensure_derivative(bucket, key, kind):
                made += 1
            else:
                failed += 1
                print(f"  failed: {kind} for {bucket}/{key}")

    await asyncio.gather(*(_one(*j) for j in jobs))
    print(f"Done: {made} made, {skipped} already there, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
