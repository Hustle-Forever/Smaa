"""Phase 8 — analyse + embed downloaded images.

Per image:
  1. pHash dedup pass — Hamming-distance ≤ 6 = duplicate; later rows skipped
  2. CLIP encode (512d) → image_embeddings (when USE_CLIP=true)
  3. OpenAI vision_describe → assets.ai_description (settings-aware preamble)
  4. text-embedding-3-small of description → assets.description_embedding (1536d)

Respects hints.image_processing_cap and hints.image_prompt. That prompt used to
carry a visual-emphasis preamble when the user pushed weight_visual or
weight_visual_identity high; those sliders were deleted, so it is now always the
plain DEFAULT_IMAGE_PROMPT.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ....config import config
from ....db import get_conn
from ....services.clip_service import enabled as clip_enabled, encode_image_paths
from ....services.openai_client import embed_batch, is_vision_supported, vision_describe
from ....services.storage import resolve_local
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.image")

PHASH_HAMMING_THRESHOLD = 6
CLIP_BATCH = 16


# ---------------------------------------------------------------------------
# pHash dedup
# ---------------------------------------------------------------------------

def _hex_to_hash(s: str):
    import imagehash
    return imagehash.hex_to_hash(s)


async def _dedup_pass() -> int:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, perceptual_hash
                  FROM assets
                 WHERE asset_type='image'
                   AND download_status='downloaded'
                   AND perceptual_hash IS NOT NULL
                   AND analyze_status='pending'
                 ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()

    if not rows:
        return 0

    kept: list[tuple[int, object]] = []
    skipped = 0
    for r in rows:
        try:
            ph = _hex_to_hash(r["perceptual_hash"])
        except Exception:
            continue
        dup_of = None
        for keeper_id, keeper_ph in kept:
            try:
                if (ph - keeper_ph) <= PHASH_HAMMING_THRESHOLD:
                    dup_of = keeper_id
                    break
            except Exception:
                continue
        if dup_of is not None:
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE assets
                           SET analyze_status='skipped', embed_status='skipped',
                               metadata=COALESCE(metadata, '{}'::jsonb)
                                        || jsonb_build_object('duplicate_of', %s::int, 'skip_reason', 'phash_dup'::text)
                         WHERE id=%s
                        """,
                        (dup_of, r["id"]),
                    )
            skipped += 1
        else:
            kept.append((r["id"], ph))

    log.info("pHash dedup: %d kept, %d skipped", len(kept), skipped)
    return skipped


# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------

async def _select_pending(limit: int) -> list[dict]:
    sql = (
        "SELECT id, page_id, canonical_url, local_path, metadata "
        "  FROM assets "
        " WHERE asset_type='image' "
        "   AND download_status='downloaded' "
        "   AND analyze_status='pending' "
        "   AND local_path IS NOT NULL "
        " ORDER BY id ASC "
        f"LIMIT {int(limit)}"
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return await cur.fetchall()


async def run(ctx: PhaseContext) -> PhaseResult:
    from ....services.settings_service import Hints
    hints: Hints = ctx.hints  # type: ignore[assignment]

    if hints.skip_images:
        log.info("[run=%s] Image phase skipped (hints.skip_images=true)", ctx.run_id)
        return PhaseResult(name="image", total=0, notes="skipped per settings")

    await _dedup_pass()

    rows = await _select_pending(hints.image_processing_cap)
    if not rows:
        log.info("[run=%s] Image: nothing pending", ctx.run_id)
        return PhaseResult(name="image", total=0)

    log.info(
        "[run=%s] Image: %d to analyse (cap=%d, CLIP=%s)",
        ctx.run_id, len(rows), hints.image_processing_cap, clip_enabled(),
    )

    processed = 0
    failed = 0
    skipped = 0

    for batch_start in range(0, len(rows), CLIP_BATCH):
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("image cancelled mid-batch")

        batch = rows[batch_start:batch_start + CLIP_BATCH]
        paths: list[Path] = [resolve_local(r["local_path"]) for r in batch]

        # 1. CLIP embed (off the event loop)
        clip_vecs = await asyncio.to_thread(encode_image_paths, paths, batch_size=CLIP_BATCH)

        # 2. Vision describe + 3. text-embed-3-small of descriptions
        # Vision is sequential per row (no batching API); fan out with semaphore.
        sem = asyncio.Semaphore(config.run_concurrency_openai)

        async def describe(p: Path) -> str | None:
            if not is_vision_supported(p):
                # SVG, AVIF, etc. — OpenAI vision will reject. Skip without spending.
                log.info("Skipping vision describe (unsupported MIME): %s", p.name)
                return None
            async with sem:
                try:
                    return await vision_describe(
                        p, prompt=hints.image_prompt, max_tokens=200, detail="low",
                    )
                except Exception:
                    log.exception("Vision describe failed for %s", p)
                    return None

        descriptions = await asyncio.gather(
            *(describe(p) for p, cv in zip(paths, clip_vecs)),
            return_exceptions=False,
        )

        non_null = [d for d in descriptions if d]
        desc_vecs: list[list[float] | None] = [None] * len(batch)
        if non_null:
            embedded = await embed_batch(non_null)
            it = iter(embedded)
            for i, d in enumerate(descriptions):
                if d:
                    desc_vecs[i] = next(it)

        # 4. Persist per row
        for row, p, cv, desc, dv in zip(batch, paths, clip_vecs, descriptions, desc_vecs):
            aid = row["id"]
            try:
                async with get_conn() as conn:
                    if cv is None and clip_enabled():
                        # image load failed; mark failed
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                UPDATE assets
                                   SET analyze_status='failed', embed_status='failed',
                                       metadata=COALESCE(metadata, '{}'::jsonb)
                                                || jsonb_build_object('analyze_error', 'image_load_failed')
                                 WHERE id=%s
                                """,
                                (aid,),
                            )
                        failed += 1
                        continue

                    if cv is not None:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                INSERT INTO image_embeddings (asset_id, model, embedding)
                                VALUES (%s, %s, %s)
                                ON CONFLICT (asset_id) DO UPDATE
                                  SET model=EXCLUDED.model, embedding=EXCLUDED.embedding
                                """,
                                (aid, config.clip_model, cv),
                            )

                    # desc=None can mean either "vision skipped (unsupported MIME)"
                    # or "vision failed". Distinguish so SVG/AVIF don't show as failures.
                    if desc:
                        analyze_status = "completed"
                        embed_status = "completed" if dv is not None else "failed"
                    elif not is_vision_supported(p):
                        analyze_status = "skipped"
                        embed_status = "skipped"
                    else:
                        analyze_status = "failed"
                        embed_status = "failed"

                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE assets
                               SET ai_description=%s,
                                   description_embedding=%s,
                                   analyze_status=%s,
                                   embed_status=%s,
                                   metadata=CASE WHEN %s='skipped'
                                            THEN COALESCE(metadata, '{}'::jsonb)
                                                 || jsonb_build_object('skip_reason', 'unsupported_image_mime')
                                            ELSE metadata END
                             WHERE id=%s
                            """,
                            (desc, dv, analyze_status, embed_status, analyze_status, aid),
                        )

                if desc:
                    processed += 1
                    log.info("[run=%s] ✓ asset #%d analysed (desc=%d chars)", ctx.run_id, aid, len(desc or ""))
                elif analyze_status == "skipped":
                    skipped += 1
                    log.info("[run=%s] ⏭ asset #%d skipped (unsupported MIME)", ctx.run_id, aid)
                else:
                    failed += 1
            except Exception:
                failed += 1
                log.exception("Image persist failed for asset #%s", aid)

            if ctx.report_progress:
                ctx.report_progress("image", processed + failed + skipped, len(rows))

    log.info(
        "[run=%s] Image done: %d ok, %d failed, %d skipped",
        ctx.run_id, processed, failed, skipped,
    )
    return PhaseResult(
        name="image", processed=processed, failed=failed, skipped=skipped, total=len(rows),
    )
