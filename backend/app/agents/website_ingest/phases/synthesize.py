"""Phase 10 — synthesize the brand profile from the ingested corpus.

Five artifacts written under one brand_profile_runs row:
  1. color_palette  — k-means over downloaded image pixels (12 clusters)
  2. voice_tone     — gpt-4o reads top long pages, returns JSON profile
  3. topic_cluster  — k-means over website_pages.embedding (k=10), labelled
  4. visual_cluster — k-means over image_embeddings.embedding (k=12), labelled
  5. final_json + final_markdown — gpt-4o synthesises everything

Settings-aware:
  - hints.limits caps how much data is fed to the prompts
  - hints.emphasis_hint + hints.instructions_hint splice into the final-synthesis system prompt
  - hints.model / temperature / max_tokens are used for the final call

Outputs:
  - brand_profile_artifacts rows (one per artifact)
  - data/brand/brand_profile.{md,json}
  - brand_profile_runs.status flipped to 'completed'
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import numpy as np
from psycopg.types.json import Jsonb
from scipy.cluster.vq import kmeans2

from ....config import config
from ....db import get_conn
from ....services.openai_client import chat
from ....services.storage import resolve_local
from ....utils.tokens import truncate_to_tokens
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.synthesize")


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------

async def _start_run() -> int:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO brand_profile_runs (status) VALUES ('started') RETURNING id"
            )
            row = await cur.fetchone()
    return int(row["id"])


async def _finish_run(run_id: int, status: str, notes: str | None = None) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE brand_profile_runs SET status=%s, finished_at=NOW(), notes=%s WHERE id=%s",
                (status, notes, run_id),
            )


async def _save_artifact(run_id: int, kind: str, payload) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO brand_profile_artifacts (run_id, kind, payload) VALUES (%s, %s, %s)",
                (run_id, kind, Jsonb(payload)),
            )


# ---------------------------------------------------------------------------
# 1. Color palette
# ---------------------------------------------------------------------------

def _rgb_to_hex(rgb) -> str:
    r, g, b = (max(0, min(255, int(v))) for v in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _build_color_palette_sync(local_paths_with_ids: list[tuple[int, str]], *, k_per_image: int = 6, k_global: int = 12) -> dict:
    from PIL import Image
    try:
        import pillow_avif  # noqa: F401
    except ImportError:
        pass

    all_centroids = []
    weights = []
    sample_assets_by_centroid: list[int] = []

    for aid, local_path in local_paths_with_ids:
        p = resolve_local(local_path)
        try:
            with Image.open(p) as im:
                rgb = im.convert("RGB")
                rgb.thumbnail((200, 200))
                arr = np.array(rgb).reshape(-1, 3).astype(np.float32)
                if arr.shape[0] < k_per_image:
                    continue
                centroids, labels = kmeans2(arr, k_per_image, minit="++", seed=0)
                counts = np.bincount(labels, minlength=k_per_image).astype(np.float32)
                for c, w in zip(centroids, counts):
                    if w <= 0:
                        continue
                    all_centroids.append(c)
                    weights.append(float(w))
                    sample_assets_by_centroid.append(aid)
        except Exception:
            log.exception("Color sample failed for %s", p)

    if len(all_centroids) < k_global:
        return {"clusters": [], "image_count": len(local_paths_with_ids)}

    cents_arr = np.asarray(all_centroids, dtype=np.float32)
    final_centroids, final_labels = kmeans2(cents_arr, k_global, minit="++", seed=0)
    weights_arr = np.asarray(weights, dtype=np.float32)

    clusters = []
    total_weight = float(weights_arr.sum())
    for i in range(k_global):
        mask = final_labels == i
        if not mask.any():
            continue
        cluster_weight = float(weights_arr[mask].sum())
        weighted = (cents_arr[mask] * weights_arr[mask, None]).sum(axis=0) / cluster_weight
        member_ids = list({sample_assets_by_centroid[idx] for idx in np.where(mask)[0][:6]})
        clusters.append({
            "rgb": [round(float(x), 1) for x in weighted],
            "hex": _rgb_to_hex(weighted),
            "prevalence": round(cluster_weight / total_weight, 4),
            "sample_asset_ids": member_ids[:6],
        })
    clusters.sort(key=lambda c: c["prevalence"], reverse=True)
    return {"clusters": clusters, "image_count": len(local_paths_with_ids), "k_global": k_global}


async def build_color_palette(*, max_images: int = 200) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT a.id, a.local_path
                  FROM assets a
                  LEFT JOIN image_embeddings ie ON ie.asset_id = a.id
                 WHERE a.asset_type='image'
                   AND a.download_status='downloaded'
                   AND a.local_path IS NOT NULL
                   AND a.analyze_status='completed'
                 ORDER BY a.id ASC
                 LIMIT %s
                """,
                (max_images,),
            )
            rows = await cur.fetchall()
    inputs = [(r["id"], r["local_path"]) for r in rows]
    return await asyncio.to_thread(_build_color_palette_sync, inputs)


# ---------------------------------------------------------------------------
# 2. Voice & tone
# ---------------------------------------------------------------------------

VOICE_TONE_SYSTEM = (
    "You are a brand strategist analyzing the writing style of 42 Abu Dhabi, "
    "a coding school in the UAE. Read the provided pages and return a strict JSON "
    "object with these keys:\n"
    "  voice_descriptors: array of 4-8 short adjectives describing the brand voice\n"
    "  tone_descriptors: array of 4-8 short adjectives describing tone\n"
    "  vocabulary_signatures: array of 6-12 distinctive words/phrases the brand uses\n"
    "  rhythm_notes: a single sentence describing sentence rhythm and pacing\n"
    "  taboos: array of 3-6 things the brand never says or never does\n"
    "  audience: a single sentence about the implied audience\n"
    "Output ONLY the JSON object, no prose."
)


async def build_voice_tone(*, max_pages: int = 20, max_total_tokens: int = 80000) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, url, page_title, ai_summary, extracted_text
                  FROM website_pages
                 WHERE extracted_text IS NOT NULL
                 ORDER BY length(extracted_text) DESC
                 LIMIT %s
                """,
                (max_pages,),
            )
            rows = await cur.fetchall()

    parts: list[str] = []
    used = 0
    for r in rows:
        block = (
            f"--- PAGE {r['url']} ---\n"
            f"TITLE: {r['page_title'] or '(none)'}\n"
            f"SUMMARY: {r['ai_summary'] or '(none)'}\n"
            f"BODY: {r['extracted_text']}\n"
        )
        block = truncate_to_tokens(block, max_total_tokens - used)
        parts.append(block)
        used += min(8000, max_total_tokens - used)
        if used >= max_total_tokens:
            break

    raw = await chat(
        "\n".join(parts),
        system=VOICE_TONE_SYSTEM,
        model=config.openai_model_synthesis,
        max_tokens=1200, temperature=0.2,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Voice/tone JSON parse failed; returning raw")
        return {"raw": raw}


# ---------------------------------------------------------------------------
# 3. Topic clusters (text)
# ---------------------------------------------------------------------------

def _parse_vec(text_repr: str) -> list[float]:
    s = text_repr.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x]


async def build_topic_clusters(*, k: int = 10) -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, url, page_title, ai_summary, embedding::text AS emb "
                "  FROM website_pages "
                " WHERE embedding IS NOT NULL"
            )
            rows = await cur.fetchall()
    if len(rows) < k:
        log.warning("Not enough pages with embeddings (%d < %d)", len(rows), k)
        return []

    vecs = np.asarray([_parse_vec(r["emb"]) for r in rows], dtype=np.float32)
    centroids, labels = await asyncio.to_thread(kmeans2, vecs, k, minit="++", seed=0)

    clusters: list[dict] = []
    for ci in range(k):
        mask = labels == ci
        if not mask.any():
            continue
        idxs = np.where(mask)[0]
        dists = np.linalg.norm(vecs[idxs] - centroids[ci], axis=1)
        order = idxs[np.argsort(dists)[:5]]
        members = [rows[i] for i in order]
        sample = "\n".join(
            f"- {m['url']}: {(m['ai_summary'] or m['page_title'] or '')[:200]}"
            for m in members
        )
        try:
            raw = await chat(
                f"Pages in this cluster:\n{sample}\n\n"
                "Return JSON: {\"label\": \"3-5 word title\", \"description\": \"one sentence\"}",
                system="You label thematic clusters of marketing webpages. JSON only.",
                model=config.openai_model_chat,
                max_tokens=120, temperature=0.2,
                response_format={"type": "json_object"},
            )
            label_obj = json.loads(raw)
        except Exception:
            label_obj = {"label": f"Cluster {ci+1}", "description": ""}
        clusters.append({
            "cluster_index": ci,
            "label": label_obj.get("label", f"Cluster {ci+1}"),
            "description": label_obj.get("description", ""),
            "size": int(mask.sum()),
            "member_page_ids": [int(rows[i]["id"]) for i in idxs.tolist()],
            "sample_urls": [rows[i]["url"] for i in order.tolist()],
        })
    clusters.sort(key=lambda c: c["size"], reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# 4. Visual clusters (CLIP)
# ---------------------------------------------------------------------------

async def build_visual_clusters(*, k: int = 12, samples_per_cluster: int = 4) -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ie.asset_id AS id, a.local_path, a.ai_description,
                       a.canonical_url, ie.embedding::text AS emb
                  FROM image_embeddings ie
                  JOIN assets a ON a.id = ie.asset_id
                 WHERE a.analyze_status='completed'
                """
            )
            rows = await cur.fetchall()
    if len(rows) < k:
        log.warning("Not enough image embeddings (%d < %d)", len(rows), k)
        return []

    vecs = np.asarray([_parse_vec(r["emb"]) for r in rows], dtype=np.float32)
    centroids, labels = await asyncio.to_thread(kmeans2, vecs, k, minit="++", seed=0)

    clusters: list[dict] = []
    for ci in range(k):
        mask = labels == ci
        if not mask.any():
            continue
        idxs = np.where(mask)[0]
        dists = np.linalg.norm(vecs[idxs] - centroids[ci], axis=1)
        order = idxs[np.argsort(dists)[:samples_per_cluster]]
        rep_descs = [rows[i]["ai_description"] or "" for i in order]
        rep_urls = [rows[i]["canonical_url"] for i in order]
        sample = "\n".join(f"- {d}" for d in rep_descs if d)
        try:
            raw = await chat(
                f"Image descriptions in this visual cluster:\n{sample}\n\n"
                "Return JSON: {\"label\": \"3-5 word style title\", "
                "\"description\": \"one sentence about the visual style: composition, lighting, subject, mood\"}",
                system="You label visual style clusters. JSON only.",
                model=config.openai_model_chat,
                max_tokens=120, temperature=0.2,
                response_format={"type": "json_object"},
            )
            label_obj = json.loads(raw)
        except Exception:
            label_obj = {"label": f"Visual cluster {ci+1}", "description": ""}
        clusters.append({
            "cluster_index": ci,
            "label": label_obj.get("label", f"Visual cluster {ci+1}"),
            "description": label_obj.get("description", ""),
            "size": int(mask.sum()),
            "member_asset_ids": [int(rows[i]["id"]) for i in idxs.tolist()],
            "sample_asset_ids": [int(rows[i]["id"]) for i in order.tolist()],
            "sample_urls": rep_urls,
        })
    clusters.sort(key=lambda c: c["size"], reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# 5. Final synthesis
# ---------------------------------------------------------------------------

FINAL_SYSTEM_BASE = (
    "You are a senior brand strategist preparing a brand-identity dossier for "
    "the 42 Abu Dhabi coding school. The input below is structured in priority "
    "order: priority_1 is the CANONICAL brand identity (owner-uploaded brand "
    "guides), priority_6 is the WEAKEST (k-means colour averages of website "
    "photos).\n\n"
    "Priority rules — non-negotiable:\n"
    "  1. When sources disagree on colours, fonts, taglines, or visual style, "
    "the higher-priority source ALWAYS wins. Never let a k-means pixel colour "
    "override an explicit hex code from a brand guide.\n"
    "  2. Do NOT invent colours or fonts that aren't present in any source.\n"
    "  3. visual_identity.primary_colors and visual_identity.secondary_colors "
    "MUST come from priority_1.extracted.colors or page_extractions.hex if any "
    "are present. Only fall back to priority_6_pixel_palette when NO brand "
    "guide colour is found.\n"
    "  4. visual_identity.fonts MUST come from priority_1.extracted.fonts if "
    "present, else priority_2/3 extracted.fonts, else [].\n"
    "  5. Voice descriptors, dos, donts, audience: prefer priority_1, then "
    "priority_3, then priority_4 (website voice_tone).\n"
    "  6. Use uppercase 6-digit hex (#RRGGBB).\n\n"
    "Output a STRICT JSON object with these keys:\n"
    "  brand_summary: 2-3 paragraph overview (concrete, no marketing fluff)\n"
    "  voice: { pillars: [...], do: [...], dont: [...] }\n"
    "  visual_identity: {\n"
    "    primary_colors: [hex, ...],\n"
    "    secondary_colors: [hex, ...],\n"
    "    fonts: [name, ...],\n"
    "    style_pillars: [string, ...]\n"
    "  }\n"
    "  audiences: [{ label, description }, ...]\n"
    "  message_themes: [{ label, description }, ...]\n"
    "  visual_themes: [{ label, description }, ...]\n"
    "  taglines_in_use: [string, ...]\n"
    "  brand_dos: [string, ...]\n"
    "  brand_donts: [string, ...]\n"
    "  notes_for_producer_agent: one paragraph\n"
    "  _sources: { primary_colors: \"priority_N\", fonts: \"priority_N\", "
    "voice: \"priority_N\", message_themes: \"priority_N\" }\n"
    "Output ONLY the JSON object, no prose, no markdown fences."
)

# Extended system prompt that also describes the two newer sources folded into
# the brand profile: owner-uploaded brand videos and the brand's own Instagram.
FINAL_SYSTEM_EXT = (
    FINAL_SYSTEM_BASE
    + "\n\nAdditional sources in the input:\n"
    "  • priority_3b_owner_brand_videos — owner-uploaded brand VIDEOS (manifesto / "
    "culture films), each analysed to { summary, transcript, extracted{ "
    "voice_descriptors, colors, taglines, dos, donts, audience, visual_style_notes } }. "
    "Treat these as CANONICAL owner identity (same trust tier as priority_1 / "
    "priority_3): the transcript is the brand speaking in its OWN words — mine it for "
    "voice pillars, taglines, dos/donts, and message themes.\n"
    "  • priority_7_own_instagram_content — the brand's OWN Instagram posts (captions, "
    "summaries, video_analysis). Shows how the brand actually presents publicly; use it "
    "for voice, message_themes and visual_themes, but it is WEAKER than explicit brand "
    "guides — never let it override priority_1 colours/fonts. Engagement counts are "
    "audience signal, not brand identity — don't treat them as voice.\n"
    "\nOutput ONLY the JSON object, no prose, no markdown fences."
)


async def build_brand_doc_signals() -> dict:
    """Pull brand_documents grouped by source-kind priority.

    Returns a dict with four buckets — canonical (pdf/docx/text), visual
    (image uploads), narrative (free_text), video (analysed brand videos).
    Each bucket is a list of:
      { id, kind, filename, summary, extracted, page_extractions[, transcript] }
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, doc_kind, source_filename, ai_summary, ai_description,
                       raw_text, metadata
                  FROM brand_documents
                 WHERE upload_status IN ('completed', 'processing')
                 ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()

    canonical: list[dict] = []
    visual: list[dict] = []
    narrative: list[dict] = []
    video: list[dict] = []

    for r in rows:
        meta = r.get("metadata") or {}
        entry = {
            "id": r["id"],
            "kind": r["doc_kind"],
            "filename": r.get("source_filename"),
            "summary": (r.get("ai_summary") or r.get("ai_description") or "").strip()[:1500],
            "extracted": meta.get("extracted"),
            "page_extractions": meta.get("page_extractions"),
        }
        if r["doc_kind"] in ("pdf", "docx", "text"):
            canonical.append(entry)
        elif r["doc_kind"] == "image":
            visual.append(entry)
        elif r["doc_kind"] == "free_text":
            narrative.append(entry)
        elif r["doc_kind"] == "video":
            # Brand videos carry a spoken transcript (raw_text) on top of the
            # summary + extracted signals — surface a trimmed transcript so the
            # synthesiser hears the brand's actual words (manifesto, etc.).
            entry["transcript"] = (r.get("raw_text") or "").strip()[:2000]
            video.append(entry)

    return {
        "canonical": canonical,
        "visual": visual,
        "narrative": narrative,
        "video": video,
    }


async def build_own_instagram_signals(limit: int = 40) -> list[dict]:
    """Pull the brand's OWN Instagram posts (account = INSTAGRAM_TARGET_USERNAME)
    as a brand-profile signal — captions + AI summaries + video analysis. This
    is read-only and does NOT touch the generation-time Instagram retrieval; it
    only lets the profile reflect how the brand actually presents on IG.

    Competitor / reference accounts (also in instagram_posts) are excluded by
    the account_username filter.
    """
    own = (config.instagram_target_username or "").strip()
    if not own:
        return []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT caption, ai_summary, media_type,
                       like_count, comments_count, view_count,
                       (metadata->'video_analysis') AS video_analysis
                  FROM instagram_posts
                 WHERE account_username = %s
                   AND (caption IS NOT NULL OR ai_summary IS NOT NULL)
                 ORDER BY (COALESCE(like_count,0) + COALESCE(comments_count,0)) DESC,
                          timestamp_posted DESC NULLS LAST
                 LIMIT %s
                """,
                (own, limit),
            )
            rows = await cur.fetchall()

    signals: list[dict] = []
    for r in rows:
        va = r.get("video_analysis") or {}
        signals.append({
            "media_type": r.get("media_type"),
            "caption": (r.get("caption") or "").strip()[:600],
            "summary": (r.get("ai_summary") or "").strip()[:600],
            # The brand-relevant slices of the per-video analysis.
            "narrative_arc": va.get("narrative_arc") if isinstance(va, dict) else None,
            "brand_presence": va.get("brand_presence") if isinstance(va, dict) else None,
            "on_screen_text": va.get("on_screen_text") if isinstance(va, dict) else None,
            "engagement": {
                "likes": r.get("like_count"),
                "comments": r.get("comments_count"),
                "views": r.get("view_count"),
            },
        })
    return signals


async def build_final(
    hints,
    color_palette: dict,
    voice_tone: dict,
    topics: list[dict],
    visuals: list[dict],
) -> tuple[dict, str]:
    brand_doc = await build_brand_doc_signals()
    own_ig = await build_own_instagram_signals()

    bundle = {
        "priority_1_canonical_brand_guides": brand_doc["canonical"],
        "priority_2_owner_visual_samples":   brand_doc["visual"],
        "priority_3_owner_narrative":        brand_doc["narrative"],
        # Owner-uploaded brand videos (manifesto / culture films): canonical-grade
        # spoken + visual brand identity.
        "priority_3b_owner_brand_videos":    brand_doc["video"],
        "priority_4_website_voice_tone":     voice_tone,
        "priority_5_website_clusters":       {"topics": topics, "visuals": visuals},
        "priority_6_pixel_palette":          color_palette,
        # The brand's OWN Instagram presence — how it actually sounds/looks in the
        # wild. Strong voice/theme signal; weaker than explicit brand guides.
        "priority_7_own_instagram_content":  own_ig,
    }

    system = FINAL_SYSTEM_EXT
    if hints.emphasis_hint:
        system = f"{FINAL_SYSTEM_BASE}\n\n{hints.emphasis_hint}"
    if hints.instructions_hint:
        system = f"{system}\n\nUSER INSTRUCTIONS:\n{hints.instructions_hint}"

    raw = await chat(
        json.dumps(bundle, ensure_ascii=False),
        system=system,
        model=hints.model,
        max_tokens=hints.max_tokens,
        temperature=hints.temperature,
        response_format={"type": "json_object"},
    )
    # Strip markdown fences if the model added them anyway
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    final_json = json.loads(cleaned)
    md = _render_markdown(final_json, color_palette)
    return final_json, md


def _render_markdown(final_json: dict, color_palette: dict) -> str:
    lines = ["# 42 Abu Dhabi — Brand Profile", "", "_Generated automatically; revise before public use._", ""]
    lines += ["## Brand summary", "", final_json.get("brand_summary", ""), ""]

    voice = final_json.get("voice") or {}
    lines.append("## Voice")
    if voice.get("pillars"):
        lines.append("**Pillars:** " + ", ".join(voice["pillars"]))
    if voice.get("do"):
        lines.append("\n**Do:**")
        for d in voice["do"]:
            lines.append(f"- {d}")
    if voice.get("dont"):
        lines.append("\n**Don't:**")
        for d in voice["dont"]:
            lines.append(f"- {d}")
    lines.append("")

    vi = final_json.get("visual_identity") or {}
    lines.append("## Visual identity")
    if vi.get("primary_colors"):
        lines.append("**Primary colors:** " + ", ".join(vi["primary_colors"]))
    if vi.get("secondary_colors"):
        lines.append("**Secondary colors:** " + ", ".join(vi["secondary_colors"]))
    if vi.get("fonts"):
        lines.append("**Fonts:** " + ", ".join(vi["fonts"]))
    if vi.get("style_pillars"):
        lines.append("\n**Style pillars:**")
        for s in vi["style_pillars"]:
            lines.append(f"- {s}")
    lines.append("")

    for section_key, section_title in [
        ("audiences", "Audiences"),
        ("message_themes", "Message themes"),
        ("visual_themes", "Visual themes"),
    ]:
        items = final_json.get(section_key)
        if items:
            lines.append(f"## {section_title}")
            for item in items:
                lines.append(f"- **{item.get('label','')}** — {item.get('description','')}")
            lines.append("")

    if final_json.get("taglines_in_use"):
        lines.append("## Taglines / phrases in use")
        for t in final_json["taglines_in_use"]:
            lines.append(f"- {t}")
        lines.append("")

    for section_key, section_title in [
        ("brand_dos", "Brand do's"),
        ("brand_donts", "Brand don'ts"),
    ]:
        items = final_json.get(section_key)
        if items:
            lines.append(f"## {section_title}")
            for s in items:
                lines.append(f"- {s}")
            lines.append("")

    if final_json.get("notes_for_producer_agent"):
        lines += ["## Notes for the Producer Agent", "", final_json["notes_for_producer_agent"], ""]

    if color_palette.get("clusters"):
        lines.append("## Detected color swatches (by prevalence)")
        for c in color_palette["clusters"][:12]:
            lines.append(f"- `{c['hex']}` ({c['prevalence']*100:.1f}%)")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run(ctx: PhaseContext, *, notes_prefix: str = "settings-aware synth") -> PhaseResult:
    """Build the brand profile artifacts + final synthesis.

    `notes_prefix` becomes the prefix of brand_profile_runs.notes — passing
    e.g. "doc-ingest synth" or "manual resynth" lets later queries tell which
    agent produced a given run.
    """
    from ....services.settings_service import Hints
    hints: Hints = ctx.hints  # type: ignore[assignment]

    config.brand_root.mkdir(parents=True, exist_ok=True)
    bp_run_id = await _start_run()
    log.info("[run=%s] Synthesis run #%d started (mode=%s)", ctx.run_id, bp_run_id, hints.mode)

    try:
        # Limits derived from hints
        page_limit = max(1, hints.limits.get("pageSummaries", 12))
        image_limit = max(1, hints.limits.get("imageDescriptions", 10) // 2)  # we doubled earlier
        max_imgs_for_palette = max(50, image_limit * 4)

        color_palette = await build_color_palette(max_images=max_imgs_for_palette)
        await _save_artifact(bp_run_id, "color_palette", color_palette)

        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("synth cancelled before voice_tone")

        voice_tone = await build_voice_tone(max_pages=page_limit)
        await _save_artifact(bp_run_id, "voice_tone", voice_tone)

        topics = await build_topic_clusters()
        for t in topics:
            await _save_artifact(bp_run_id, "topic_cluster", t)

        visuals = await build_visual_clusters()
        for v in visuals:
            await _save_artifact(bp_run_id, "visual_cluster", v)

        final_json, md = await build_final(hints, color_palette, voice_tone, topics, visuals)
        await _save_artifact(bp_run_id, "final_json", final_json)
        await _save_artifact(bp_run_id, "final_markdown", {"markdown": md})

        (config.brand_root / "brand_profile.json").write_text(
            json.dumps(final_json, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (config.brand_root / "brand_profile.md").write_text(md, encoding="utf-8")

        notes = f"{notes_prefix} · {hints.mode}"
        await _finish_run(bp_run_id, "completed", notes=notes)
        log.info("[run=%s] Synthesis #%d completed", ctx.run_id, bp_run_id)
        return PhaseResult(
            name="synthesize", processed=1, total=1,
            extra={"brand_profile_run_id": bp_run_id, "notes": notes},
        )
    except CancellationError:
        await _finish_run(bp_run_id, "failed", notes="cancelled")
        raise
    except Exception as e:
        log.exception("Synthesis failed")
        await _finish_run(bp_run_id, "failed", notes=str(e)[:500])
        raise
