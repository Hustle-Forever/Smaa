"""Shared helpers for instagram_ingest phases."""
from __future__ import annotations

from ...db import get_conn


PLACEHOLDER_URL = "internal://instagram-ingest/placeholder"


async def ensure_placeholder_page_id() -> int:
    """Return (creating if needed) the website_pages.id used as the FK parent
    for every IG asset.

    `assets.page_id` is NOT NULL in the existing schema. IG assets have no real
    website page, so we maintain ONE shared placeholder row in website_pages
    keyed by a sentinel URL. Idempotent — safe to call concurrently because
    `website_pages.url` is UNIQUE and ON CONFLICT no-ops.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO website_pages (url, page_title, source_type,
                                           fetched_status, ai_analyzed_status, embedded_status,
                                           metadata)
                VALUES (%s, %s, 'manual', 'skipped', 'skipped', 'skipped',
                        '{"source": "instagram_ingest_placeholder"}'::jsonb)
                ON CONFLICT (url) DO UPDATE SET url = EXCLUDED.url
                RETURNING id
                """,
                (PLACEHOLDER_URL, "Instagram ingest placeholder (synthetic FK parent)"),
            )
            row = await cur.fetchone()
    return int(row["id"])
