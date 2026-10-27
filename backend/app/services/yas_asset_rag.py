"""Asset knowledge for the video-editor AI.

Two things the editor AI needs to use its toolbox well:

  1. search_assets_svc() — semantic search over the Assets Library
     (producer_assets: stickers, fonts, frames, music), returning each asset's
     description + structured attributes so the AI knows WHAT it has and HOW to
     use it. A reusable service wrapper around the same pgvector search the
     /assets/search endpoint uses; the agent's `search_assets` tool calls this.

  2. popup_recipes — an Instagram-mined RAG of how POPUPS/text-overlays are
     actually used in the brand's own posts (placement, scale, pairing, mood).
     Mirrors yas_edit_recipes (store → embed+UPSERT, nearest → cosine+hybrid,
     format → style-only prompt block). `mine_instagram_popups()` populates it
     by running Gemini over ingested IG text-overlay images; `get_popup_examples()`
     retrieves the nearest exemplars for the agent's `get_popup_examples` tool.

Every DB/model path is BEST-EFFORT: failures log and degrade (search → [],
store → no-op, retrieve → []). Asset knowledge must never fail an edit job.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn
from .storage import resolve_local
from .vertex_client import gemini_embed, gemini_multimodal

log = logging.getLogger("archivist.yas_asset_rag")


# ===========================================================================
# 1) Asset Library semantic search (the agent's `search_assets` tool)
# ===========================================================================

async def search_assets_svc(
    query: str, *, kind: Optional[str] = None, limit: int = 8,
) -> list[dict]:
    """Semantic search over enabled, embedded producer_assets. Returns
    agent-friendly rows: {id, asset_kind, category, display_name, description,
    attributes, similarity}. `kind` filters to 'image'(stickers/frames)/'font'/
    'audio'. Best-effort: [] on any failure (empty query, embed error, no DB)."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        vecs = await gemini_embed([q])
        qvec = vecs[0] if vecs and vecs[0] else None
        if not qvec:
            return []
        where = ["embedding IS NOT NULL", "enabled"]
        params: list[Any] = [qvec]
        if kind:
            where.append("asset_kind = %s")
            params.append(kind)
        lim = max(1, min(int(limit), 25))
        params.append(lim)
        sql = f"""
            SELECT id, asset_kind, category, display_name,
                   LEFT(ai_description, 600) AS description, attributes,
                   1 - (embedding <=> %s::vector) AS similarity
              FROM producer_assets
             WHERE {" AND ".join(where)}
             ORDER BY embedding <=> %s::vector
             LIMIT %s
        """
        # qvec appears in SELECT (similarity) + ORDER BY → splice it twice.
        bind = (params[0], *params[1:-1], params[0], params[-1])
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, bind)
                rows = [dict(r) for r in await cur.fetchall()]
        out: list[dict] = []
        for r in rows:
            out.append({
                "id": r.get("id"),
                "asset_kind": r.get("asset_kind"),
                "category": r.get("category"),
                "display_name": r.get("display_name"),
                "description": (r.get("description") or "").strip(),
                "attributes": r.get("attributes") or {},
                "similarity": round(float(r.get("similarity") or 0.0), 3),
            })
        return out
    except Exception:  # noqa: BLE001 — best-effort by contract
        log.exception("[asset-rag] search_assets_svc failed (non-fatal)")
        return []


# ===========================================================================
# 2) Instagram popup-usage RAG (the agent's `get_popup_examples` tool)
# ===========================================================================

_TABLE_READY = False

_DDL = """
CREATE TABLE IF NOT EXISTS popup_recipes (
    id                bigserial PRIMARY KEY,
    source            text NOT NULL,
    source_post_id    bigint,
    recipe            jsonb NOT NULL,
    embed_text        text,
    embedding         vector(768),
    engagement_score  real DEFAULT 0,
    created_at        timestamptz DEFAULT now()
)
"""

_IDX_POST = """
CREATE UNIQUE INDEX IF NOT EXISTS popup_recipes_source_post_uq
    ON popup_recipes (source, source_post_id)
 WHERE source_post_id IS NOT NULL
"""


async def _ensure_table() -> bool:
    """Idempotently create popup_recipes + its unique index (once per process).
    Returns True when available; False (logged) on failure."""
    global _TABLE_READY
    if _TABLE_READY:
        return True
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_DDL)
                await cur.execute(_IDX_POST)
        _TABLE_READY = True
        return True
    except Exception:  # noqa: BLE001
        log.exception("[asset-rag] could not ensure popup_recipes table (non-fatal)")
        return False


def popup_embed_text(recipe: dict) -> str:
    """Retrieval text for a popup recipe: the style/placement/mood/topic that a
    future video would match on (NOT literal popup text)."""
    if not isinstance(recipe, dict):
        return ""
    bits = [
        recipe.get("style"), recipe.get("placement"), recipe.get("scale"),
        recipe.get("pairing"), recipe.get("mood"), recipe.get("topic_hint"),
    ]
    return "  ".join(str(b).strip() for b in bits if b).strip()[:1500]


async def store_popup_recipe(
    *, source: str, recipe: dict, source_post_id: int | None = None,
    engagement_score: float = 0.0,
) -> None:
    """Embed + UPSERT one popup recipe (idempotent per (source, source_post_id)).
    Best-effort — never raises."""
    try:
        if not isinstance(recipe, dict) or not recipe:
            return
        if not await _ensure_table():
            return
        embed_text = popup_embed_text(recipe)
        embedding = None
        if embed_text:
            try:
                vecs = await gemini_embed([embed_text])
                embedding = vecs[0] if vecs and vecs[0] else None
            except Exception:
                log.exception("[asset-rag] embed failed — storing recipe without vector")
        sql = (
            "INSERT INTO popup_recipes "
            "(source, source_post_id, recipe, embed_text, embedding, engagement_score) "
            "VALUES (%s, %s, %s, %s, %s::vector, %s)"
        )
        if source_post_id is not None:
            sql += (
                " ON CONFLICT (source, source_post_id) WHERE source_post_id IS NOT NULL"
                " DO UPDATE SET recipe = EXCLUDED.recipe,"
                " embed_text = EXCLUDED.embed_text,"
                " embedding = EXCLUDED.embedding,"
                " engagement_score = EXCLUDED.engagement_score"
            )
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    str(source), source_post_id, Jsonb(recipe),
                    embed_text or None, embedding, float(engagement_score or 0.0),
                ))
    except Exception:  # noqa: BLE001
        log.exception("[asset-rag] store_popup_recipe failed (non-fatal)")


async def nearest_popup_recipes(query_text: str, *, k: int = 3) -> list[dict]:
    """Top-k popup recipes nearest to `query_text`, hybrid re-ranked
    (0.6*similarity + 0.4*normalized engagement). Best-effort: [] on failure."""
    try:
        q = (query_text or "").strip()
        if not q or k <= 0:
            return []
        if not await _ensure_table():
            return []
        vecs = await gemini_embed([q])
        qvec = vecs[0] if vecs and vecs[0] else None
        if not qvec:
            return []
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT recipe, engagement_score, "
                    "       1 - (embedding <=> %s::vector) AS sim "
                    "  FROM popup_recipes WHERE embedding IS NOT NULL "
                    " ORDER BY embedding <=> %s::vector LIMIT %s",
                    (qvec, qvec, max(k * 3, k)),
                )
                rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return []
        max_eng = max((float(r.get("engagement_score") or 0.0) for r in rows), default=0.0)

        def _score(r: dict) -> float:
            sim = float(r.get("sim") or 0.0)
            eng = float(r.get("engagement_score") or 0.0)
            return 0.6 * sim + 0.4 * ((eng / max_eng) if max_eng > 0 else 0.0)

        rows.sort(key=_score, reverse=True)
        return [r["recipe"] for r in rows[:k]
                if isinstance(r.get("recipe"), dict) and r["recipe"]]
    except Exception:  # noqa: BLE001
        log.exception("[asset-rag] nearest_popup_recipes failed (non-fatal)")
        return []


def format_popup_block(recipes: list[dict]) -> str:
    """Compact prompt section: how popups are used in proven posts. STYLE ONLY —
    placement/scale/pairing/timing — never the literal text or topic."""
    items = [r for r in (recipes or []) if isinstance(r, dict)][:3]
    if not items:
        return ""
    lines = [
        "POPUP STYLE FROM PROVEN POSTS — copy the PLACEMENT / SCALE / PAIRING / "
        "TIMING that works on this brand's Instagram, NEVER the literal text or "
        "topic. Every popup you emit still uses THIS video's candidate windows."
    ]
    for i, r in enumerate(items, 1):
        bits = [
            f"placement={r.get('placement')}" if r.get("placement") else "",
            f"scale={r.get('scale')}" if r.get("scale") else "",
            f"pairing={r.get('pairing')}" if r.get("pairing") else "",
            f"timing={r.get('timing')}" if r.get("timing") else "",
            f"style={r.get('style')}" if r.get("style") else "",
        ]
        lines.append(f"  {i}. " + ", ".join(b for b in bits if b))
    return "\n".join(lines)


async def get_popup_examples(query_text: str, *, k: int = 3) -> str:
    """Convenience: nearest popup recipes for `query_text`, formatted as a prompt
    block. The agent's `get_popup_examples` tool returns this string."""
    return format_popup_block(await nearest_popup_recipes(query_text, k=k))


# ---------------------------------------------------------------------------
# Miner — extract popup-usage exemplars from ingested Instagram text-overlay
# images. Best-effort, callable on demand (runs Gemini → counts against quota,
# so it is NOT auto-run; trigger it from a route/script to populate the RAG).
# ---------------------------------------------------------------------------

_POPUP_RECIPE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "has_text_overlay": {"type": "boolean",
                             "description": "True if the image actually shows popup/caption/sticker text overlay."},
        "placement": {"type": "string",
                      "description": "Where the overlay sits: top / upper_third / center / lower_third / bottom / corner."},
        "scale": {"type": "string", "description": "Overlay size: small / medium / large."},
        "style": {"type": "string", "description": "Short visual style note (font feel, color, box/no-box, emphasis)."},
        "pairing": {"type": "string", "description": "What it pairs with: text-only / sticker+text / arrow at subject / emoji / frame box."},
        "timing": {"type": "string", "description": "Inferred timing role: hook/title / section label / CTA / caption."},
        "mood": {"type": "string"},
        "topic_hint": {"type": "string", "description": "1-4 word topic of the post."},
    },
    "required": ["has_text_overlay", "placement", "scale", "style"],
}

_MINER_SYSTEM = (
    "You analyse a single Instagram image from a school's feed to learn HOW the "
    "brand uses on-screen popup/caption/sticker text — so a video editor AI can "
    "imitate the STYLE. Report placement, scale, pairing, timing role and mood. "
    "If there is no text overlay at all, set has_text_overlay=false. Be concise."
)


async def mine_instagram_popups(*, limit: int = 40, reanalyze: bool = False) -> dict:
    """Populate popup_recipes from ingested IG text-overlay/poster IMAGES. Picks
    the most-engaged posters/text-overlay posts, runs Gemini to extract a usage
    exemplar, and UPSERTs one recipe per post. Returns {scanned, stored, skipped}.
    Best-effort throughout; runs Gemini per image (quota-aware via the gateway)."""
    if not await _ensure_table():
        return {"scanned": 0, "stored": 0, "skipped": 0, "error": "no table"}
    lim = max(1, min(int(limit), 200))
    rows: list[dict] = []
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT p.id AS post_id, p.caption,
                           COALESCE(p.like_count, 0) + COALESCE(p.comments_count, 0) AS engagement,
                           a.local_path
                      FROM instagram_posts p
                      JOIN instagram_post_assets pa ON pa.post_id = p.id
                      JOIN assets a ON a.id = pa.asset_id
                     WHERE a.asset_type = 'image'
                       AND a.local_path IS NOT NULL
                       AND (p.metadata->>'content_category' LIKE 'img_poster%%'
                            OR p.metadata->'video_analysis'->>'dominant_content' = 'text_overlay')
                       AND (%s OR NOT EXISTS (
                            SELECT 1 FROM popup_recipes r
                             WHERE r.source = 'instagram' AND r.source_post_id = p.id))
                     ORDER BY engagement DESC, p.id DESC
                     LIMIT %s
                    """,
                    (reanalyze, lim),
                )
                # DISTINCT-ish per post: first asset wins (carousels).
                seen: set[int] = set()
                for r in await cur.fetchall():
                    pid = int(r["post_id"])
                    if pid in seen:
                        continue
                    seen.add(pid)
                    rows.append(dict(r))
    except Exception:  # noqa: BLE001
        log.exception("[asset-rag] mine: candidate query failed")
        return {"scanned": 0, "stored": 0, "skipped": 0, "error": "query failed"}

    stored = skipped = 0
    for r in rows:
        try:
            local = resolve_local(r["local_path"])
            if not local or not local.exists():
                skipped += 1
                continue
            caption = (r.get("caption") or "")[:600]
            recipe = await gemini_multimodal(
                f"Caption: {caption}\n\nAnalyse the popup/text-overlay STYLE in this image.",
                images=[local],
                system=_MINER_SYSTEM,
                json_schema=_POPUP_RECIPE_SCHEMA,
                model="gemini-2.5-flash",
                temperature=0.2,
                max_tokens=600,
            )
            if not isinstance(recipe, dict) or "_raw" in recipe or not recipe.get("has_text_overlay"):
                skipped += 1
                continue
            await store_popup_recipe(
                source="instagram", recipe=recipe, source_post_id=int(r["post_id"]),
                engagement_score=float(r.get("engagement") or 0.0),
            )
            stored += 1
        except Exception:  # noqa: BLE001 — one bad post never stops the miner
            log.exception("[asset-rag] mine: post %s failed", r.get("post_id"))
            skipped += 1
    log.info("[asset-rag] mined IG popups: scanned=%d stored=%d skipped=%d",
             len(rows), stored, skipped)
    return {"scanned": len(rows), "stored": stored, "skipped": skipped}
