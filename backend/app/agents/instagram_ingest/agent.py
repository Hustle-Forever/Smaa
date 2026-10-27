"""InstagramIngestAgent — orchestrator for Agent 5.

Pipeline (mirrors website_ingest + brand_doc_ingest patterns):

  Full sync:       discover → download → classify → image → video → video_deepen
                   → recipe_extract → caption_embed → metrics_snapshot
  Metrics refresh: metrics_snapshot

The image/video phases are reused VERBATIM from website_ingest — they pick
up new rows in `assets` by status (asset_type='image' / asset_type='instagram'),
so no per-source filtering is needed.

Run state lives in `scrape_runs` (target_domain='instagram-ingest'), matching
the convention of brand_doc_ingest. Cancellation is in-memory only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ...config import config
from ...db import get_conn
from ...services.settings_service import build_hints, load_settings
from ..website_ingest.phases import image as phase_image, video as phase_video
from ..website_ingest.types import CancellationError, PhaseContext, PhaseResult
from .phases import (
    caption_embed as phase_caption_embed,
    classify as phase_classify,
    discover as phase_discover,
    download as phase_download,
    metrics_snapshot as phase_metrics_snapshot,
    recipe_extract as phase_recipe_extract,
    video_deepen as phase_video_deepen,
)

log = logging.getLogger("archivist.agent.instagram_ingest")


@dataclass
class RunOptions:
    target_username: str | None = None  # default = config.instagram_target_username
    max_posts: int | None = None  # None = walk the full paginated history
    metrics_only: bool = False  # skip discover/download/media, only refresh counts
    skip_phases: list[str] | None = None  # e.g. ["video"] for a cheap pilot
    force_re_download: bool = False  # re-fetch media files (Meta CDN URLs rotate)
    force_reclassify: bool = False  # re-classify posts whose content_category is already set
    page_size: int = 100  # Graph API page size for discovery


async def _start_run(notes: str | None = None) -> int:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO scrape_runs (target_domain, status, notes) "
                "VALUES ('instagram-ingest', 'started', %s) RETURNING id",
                (notes or "instagram-ingest agent",),
            )
            row = await cur.fetchone()
    return int(row["id"])


async def _finish_run(run_id: int, status: str, notes: str | None = None) -> None:
    """`status` must satisfy the existing CHECK (started/completed/failed)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE scrape_runs SET status=%s, finished_at=NOW(), "
                "notes=COALESCE(%s, notes) WHERE id=%s",
                (status, notes, run_id),
            )


_cancel_flags: dict[int, bool] = {}


async def request_cancel(run_id: int) -> bool:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM scrape_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if not row or row["status"] != "started":
        return False
    _cancel_flags[run_id] = True
    return True


class InstagramIngestAgent:
    def __init__(self, options: RunOptions, *, on_event=None):
        self.options = options
        self.on_event = on_event or (lambda _e: None)
        self._run_id: int | None = None
        self._cancel_requested = False

    def _emit(self, type_: str, **fields) -> None:
        evt = {"type": type_, "run_id": self._run_id, **fields}
        try:
            self.on_event(evt)
        except Exception:
            log.exception("on_event hook raised")

    def _is_cancelled(self) -> bool:
        if self._cancel_requested:
            return True
        if self._run_id is not None and _cancel_flags.get(self._run_id):
            self._cancel_requested = True
            return True
        return False

    def _report_progress(self, phase: str, processed: int, total: int) -> None:
        if total <= 20 or processed >= total or processed % 5 == 0:
            self._emit("progress", phase=phase, processed=processed, total=total)

    async def _run_phase(self, name: str, coro_factory) -> PhaseResult:
        self._emit("phase_started", phase=name)
        result = await coro_factory()
        self._emit(
            "phase_completed",
            phase=name,
            processed=result.processed,
            failed=result.failed,
            skipped=result.skipped,
            total=result.total,
            extra=result.extra,
        )
        return result

    async def run(self) -> dict:
        username = (self.options.target_username or config.instagram_target_username or "").strip()
        if not username:
            raise ValueError(
                "No target_username provided and INSTAGRAM_TARGET_USERNAME is empty."
            )

        # Make sure the on-disk dir exists for media files.
        config.instagram_root.mkdir(parents=True, exist_ok=True)

        settings = await load_settings()
        hints = build_hints(settings)
        skip = set(self.options.skip_phases or [])

        notes = f"instagram-ingest @{username}"
        if self.options.metrics_only:
            notes += " (metrics-only)"
        self._run_id = await _start_run(notes=notes)
        self._emit(
            "run_started",
            agent="instagram-ingest",
            target_username=username,
            metrics_only=self.options.metrics_only,
            max_posts=self.options.max_posts,
            mode=hints.mode,
        )

        ctx = PhaseContext(
            run_id=self._run_id,
            target_url=f"https://www.instagram.com/{username}/",
            target_domain="instagram-ingest",
            hints=hints,
            is_cancelled=self._is_cancelled,
            report_progress=self._report_progress,
            metadata={
                "instagram": {
                    "target_username": username,
                    "max_posts": self.options.max_posts,
                    "page_size": self.options.page_size,
                    "force_re_download": self.options.force_re_download,
                },
                # Read by phases/classify.py to bypass the
                # "skip already-classified" gate when the Sync UI's
                # Re-classify checkbox is on.
                "force_reclassify": self.options.force_reclassify,
                # Scope the shared video phase to IG-only assets and let it
                # drain a much larger queue than the website-ingest default.
                "video_phase_cap": config.instagram_max_videos_per_run,
                "video_phase_asset_types": ["instagram"],
            },
        )

        try:
            if self.options.metrics_only:
                if "metrics_snapshot" not in skip:
                    await self._run_phase(
                        "metrics_snapshot",
                        lambda: phase_metrics_snapshot.run(ctx),
                    )
            else:
                if "discover" not in skip:
                    await self._run_phase("discover", lambda: phase_discover.run(ctx))

                if "download" not in skip:
                    await self._run_phase("download", lambda: phase_download.run(ctx))

                if "classify" not in skip:
                    await self._run_phase("classify", lambda: phase_classify.run(ctx))

                # Reuse the existing website_ingest image / video phases. They
                # filter by asset_type + status, so newly-inserted IG asset rows
                # are picked up without any per-source branching.
                if not hints.skip_images and "image" not in skip:
                    await self._run_phase("image", lambda: phase_image.run(ctx))

                if not hints.skip_videos and "video" not in skip:
                    await self._run_phase("video", lambda: phase_video.run(ctx))

                # v2 — produce structured per-scene + whole-video understanding
                # for IG videos (uses Gemini 2.5 Pro multimodal). Idempotent:
                # skips posts where metadata.video_deepened_at is set.
                if not hints.skip_videos and "video_deepen" not in skip:
                    await self._run_phase(
                        "video_deepen",
                        lambda: phase_video_deepen.run(ctx),
                    )

                # v3 — distill each deepened reel into an "edit recipe"
                # (cut cadence, popup timing, intro/outro cards, transitions)
                # and store it in the edit-recipe RAG so the AI editor learns
                # editing STYLE from scraped reels. Best-effort + additive:
                # the phase never raises (logs and returns), and it is
                # idempotent via metadata.recipe_extracted_at.
                if not hints.skip_videos and "recipe_extract" not in skip:
                    await self._run_phase(
                        "recipe_extract",
                        lambda: phase_recipe_extract.run(ctx),
                    )

                if "caption_embed" not in skip:
                    await self._run_phase(
                        "caption_embed",
                        lambda: phase_caption_embed.run(ctx),
                    )

                if "metrics_snapshot" not in skip:
                    await self._run_phase(
                        "metrics_snapshot",
                        lambda: phase_metrics_snapshot.run(ctx),
                    )

            await _finish_run(self._run_id, "completed", notes=f"{notes} done")
            self._emit("run_completed", status="completed")
            return {"run_id": self._run_id, "status": "completed"}

        except CancellationError as e:
            await _finish_run(self._run_id, "failed", notes=f"cancelled: {e}")
            _cancel_flags.pop(self._run_id, None)
            self._emit("run_cancelled", reason=str(e))
            return {"run_id": self._run_id, "status": "cancelled"}

        except Exception as e:
            log.exception("Instagram-ingest agent failed")
            await _finish_run(
                self._run_id, "failed",
                notes=f"{type(e).__name__}: {str(e)[:400]}",
            )
            self._emit("run_failed", error=str(e)[:400])
            raise
