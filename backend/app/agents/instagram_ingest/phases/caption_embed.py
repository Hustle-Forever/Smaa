"""Phase 5 — caption_embed.

For each instagram_posts row missing a caption_embedding:
  1. Compose ai_summary from caption + per-asset descriptions (images:
     assets.ai_description; videos: video_summaries.summary).
  2. Embed ai_summary in 768d via gemini-embedding-001 (same space as
     producer_ideas / producer_scripts → enables Producer retrieval).
  3. UPDATE instagram_posts.caption_embedding + ai_summary.

Runs AFTER image + video phases so per-asset descriptions are available.
Posts with empty captions still get embedded (the composed summary alone
carries the visual signal).
"""
from __future__ import annotations

import logging

from ....db import get_conn
from ....services.vertex_client import gemini_embed
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.ig_caption_embed")

BATCH = 32


async def _pending_posts() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, ig_media_id, account_username, caption,
                       media_type, like_count, comments_count, view_count,
                       ai_summary,
                       (metadata->>'video_deepened_at') AS video_deepened_at
                  FROM instagram_posts
                 WHERE caption_embedding IS NULL
                 ORDER BY timestamp_posted DESC NULLS LAST, id DESC
                """
            )
            return await cur.fetchall()


async def _asset_descriptions(post_id: int) -> list[str]:
    """Image descriptions + video summaries linked to this post."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT a.id, a.asset_type, a.ai_description,
                       vs.summary AS video_summary
                  FROM instagram_post_assets ipa
                  JOIN assets a ON a.id = ipa.asset_id
                  LEFT JOIN video_summaries vs ON vs.asset_id = a.id
                 WHERE ipa.post_id=%s
                 ORDER BY ipa.child_index
                """,
                (post_id,),
            )
            rows = await cur.fetchall()
    out: list[str] = []
    for r in rows:
        if r["asset_type"] == "image" and r.get("ai_description"):
            out.append(str(r["ai_description"]).strip())
        elif r["asset_type"] == "instagram" and r.get("video_summary"):
            out.append(str(r["video_summary"]).strip())
    return [s for s in out if s]


def _compose_summary(post: dict, descriptions: list[str]) -> str:
    caption = (post.get("caption") or "").strip()
    parts: list[str] = []
    if caption:
        parts.append(f"CAPTION: {caption}")
    if descriptions:
        if len(descriptions) == 1:
            parts.append(f"MEDIA: {descriptions[0]}")
        else:
            joined = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(descriptions))
            parts.append(f"MEDIA (carousel of {len(descriptions)}):\n{joined}")
    mt = post.get("media_type") or ""
    parts.append(
        f"TYPE: {mt} | "
        f"likes={post.get('like_count') or 0} "
        f"comments={post.get('comments_count') or 0}"
        + (f" views={post['view_count']}" if post.get("view_count") else "")
    )
    return "\n".join(parts)


async def run(ctx: PhaseContext) -> PhaseResult:
    posts = await _pending_posts()
    if not posts:
        return PhaseResult(name="caption_embed", total=0)

    log.info("[run=%s] caption_embed: %d posts pending", ctx.run_id, len(posts))

    processed = 0
    failed = 0
    skipped = 0

    for batch_start in range(0, len(posts), BATCH):
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("ig_caption_embed cancelled mid-batch")

        batch = posts[batch_start:batch_start + BATCH]

        # Compose summaries — prefer the deepened ai_summary when present
        # (set by the video_deepen phase). Otherwise fall back to caption +
        # per-asset description composition.
        summaries: list[str] = []
        ids: list[int] = []
        for p in batch:
            try:
                if p.get("video_deepened_at") and (p.get("ai_summary") or "").strip():
                    summary = p["ai_summary"]
                else:
                    descs = await _asset_descriptions(int(p["id"]))
                    summary = _compose_summary(p, descs)
                if not summary.strip():
                    skipped += 1
                    continue
                summaries.append(summary)
                ids.append(int(p["id"]))
            except Exception:
                log.exception("compose failed for post #%s", p["id"])
                failed += 1

        if not summaries:
            continue

        # Embed in one batched call
        try:
            vectors = await gemini_embed(summaries)
        except Exception:
            log.exception("gemini_embed failed (batch of %d)", len(summaries))
            failed += len(summaries)
            continue

        # Persist
        async with get_conn() as conn:
            for pid, summary, vec in zip(ids, summaries, vectors):
                if not vec:
                    failed += 1
                    continue
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE instagram_posts
                               SET ai_summary=%s, caption_embedding=%s
                             WHERE id=%s
                            """,
                            (summary, vec, pid),
                        )
                    processed += 1
                except Exception:
                    log.exception("persist failed for post #%d", pid)
                    failed += 1

        if ctx.report_progress:
            ctx.report_progress("caption_embed", processed + failed + skipped, len(posts))

    log.info(
        "[run=%s] caption_embed done: %d ok, %d failed, %d skipped",
        ctx.run_id, processed, failed, skipped,
    )
    return PhaseResult(
        name="caption_embed",
        processed=processed,
        failed=failed,
        skipped=skipped,
        total=len(posts),
    )
