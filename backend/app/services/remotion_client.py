"""HTTP client for the local Remotion compose service.

The Remotion service runs as a separate Node process at REMOTION_BASE_URL
(default http://127.0.0.1:3001). It shares the filesystem at
`backend/data/producer/` with the FastAPI backend — clips are passed by
URL to Remotion, composed MP4s are written to disk where FastAPI reads them.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..config import config

log = logging.getLogger("archivist.remotion_client")

_HTTP_TIMEOUT = 15.0  # short — render is fire-and-forget, only metadata polled


def _base() -> str:
    return config.remotion_base_url.rstrip("/")


def file_url_for_video(video_local_path: Path | str) -> str:
    """Build the HTTP URL Remotion uses to fetch a clip during render.

    `video_local_path` is the absolute path stored on `producer_videos.local_path`.
    The Express server's `/files` static route is rooted at
    `backend/data/producer/`, so we strip that prefix and URL-encode the
    remainder.
    """
    p = Path(video_local_path)
    try:
        rel = p.relative_to(config.producer_root)
    except ValueError:
        # Caller passed something not under producer_root — fall back to the
        # basename. Worst case the renderer 404s and the user sees a clear
        # error in the composition row.
        rel = Path(p.name)
    # POSIX-style + URL-encode each component
    rel_str = "/".join(quote(part) for part in rel.parts)
    return f"{_base()}/files/{rel_str}"


async def submit_render(
    template: str,
    output_path: Path,
    props: dict[str, Any],
) -> str:
    """POST /render → returns the Express job_id."""
    body = {
        "template": template,
        "output_path": str(output_path),
        "props": props,
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(f"{_base()}/render", json=body)
    if r.status_code >= 400:
        raise RuntimeError(
            f"Remotion /render returned {r.status_code}: {r.text[:300]}"
        )
    data = r.json()
    job_id = data.get("job_id")
    if not job_id:
        raise RuntimeError(f"Remotion /render missing job_id: {data}")
    log.info("Remotion render submitted: template=%s job_id=%s", template, job_id)
    return job_id


async def get_job_status(job_id: str) -> dict[str, Any]:
    """GET /jobs/:id → {status, progress, output_path?, error?}.

    404 means the Express server lost the job (most often a process restart);
    we surface that as a synthesised failure so the poller can mark the row
    accordingly without crashing.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.get(f"{_base()}/jobs/{job_id}")
    if r.status_code == 404:
        return {"status": "failed", "error": "job lost (service restart?)"}
    if r.status_code >= 400:
        raise RuntimeError(
            f"Remotion /jobs/{job_id} returned {r.status_code}: {r.text[:300]}"
        )
    return r.json()


async def health() -> bool:
    """Cheap connectivity check used at startup logging."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{_base()}/health")
        return r.status_code == 200
    except Exception:
        return False


async def render_still(
    template: str,
    output_path: Path,
    props: dict[str, Any],
    *,
    frame: int = 0,
    timeout: float = 180.0,
) -> Path:
    """POST /still — one frame, synchronously.

    Single frames are fast enough to sit inside a request, which is what lets
    a caller LOOK at what it composed instead of inferring it from the props
    it just sent. Raises on failure; callers that treat a preview as optional
    should catch."""
    body = {
        "template": template,
        "output_path": str(output_path),
        "props": props,
        "frame": int(frame),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{_base()}/still", json=body)
    if r.status_code == 404:
        raise RuntimeError("Remotion /still is not available on this build")
    if r.status_code >= 400:
        raise RuntimeError(f"Remotion /still returned {r.status_code}: {r.text[:300]}")
    if not output_path.is_file():
        raise RuntimeError(f"Remotion /still reported success but {output_path} is missing")
    return output_path


_FRAMES_POLL_SEC = 1.5


async def render_frames(
    template: str,
    out_dir: Path,
    props: dict[str, Any],
    *,
    timeout: float = 900.0,
) -> dict[str, Any]:
    """POST /render with `format: "png-sequence"` → a folder of transparent
    PNG frames, then poll /jobs/:id until it settles.

    `out_dir` must live under backend/data (the server refuses anything else).
    Returns the finished job JSON, which carries `frame_count`,
    `frame_pattern` (printf-style name relative to out_dir, e.g.
    ``element-%03d.png``) and `start_number`. Raises RuntimeError on a failed
    or lost job, on timeout, and on a server too old to know the format (it
    would have rendered an h264 file instead of frames)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "template": template,
        "output_path": str(out_dir),
        "props": props,
        "format": "png-sequence",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(f"{_base()}/render", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Remotion /render returned {r.status_code}: {r.text[:300]}")
    job_id = (r.json() or {}).get("job_id")
    if not job_id:
        raise RuntimeError(f"Remotion /render missing job_id: {r.text[:300]}")
    log.info("Remotion frames submitted: template=%s job_id=%s → %s", template, job_id, out_dir)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    hiccups = 0
    while True:
        await asyncio.sleep(_FRAMES_POLL_SEC)
        try:
            job = await get_job_status(job_id)
            hiccups = 0
        except (httpx.HTTPError, RuntimeError) as e:
            # One dropped poll is not a failed render; three in a row is.
            hiccups += 1
            if hiccups >= 3:
                raise RuntimeError(f"Remotion /jobs/{job_id} unreachable: {e}") from e
            job = {"status": "polling"}
        state = job.get("status")
        if state == "done":
            if not job.get("frame_pattern") or job.get("frame_count") is None:
                raise RuntimeError(
                    "Remotion finished without a frame sequence — the service predates "
                    "png-sequence renders; restart it on the current build")
            return job
        if state == "failed":
            raise RuntimeError(f"Remotion frame render failed: {job.get('error') or 'unknown error'}")
        if loop.time() >= deadline:
            raise RuntimeError(f"Remotion frame render did not finish within {timeout:.0f}s")
