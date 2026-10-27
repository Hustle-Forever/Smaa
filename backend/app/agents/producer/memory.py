"""Brand context + taste memory injected into Gemini calls.

v5 split:
  - `build_context_block()` produces SOFT context (brand summary + taste
    memory) destined for the SYSTEM prompt. LLMs treat this as background.
  - `build_brand_requirements()` produces a structured dict with the actual
    hex codes / voice pillars / motifs / taglines / donts. The phases inject
    this into the USER message as REQUIRED constraints — LLMs respect these
    far better than system context.

v6 (IG-inspiration wiring + dialogue):
  - `_nearest_instagram_posts` now over-fetches by embedding and reranks by
    a 60/40 hybrid of semantic similarity + normalized engagement. A 1-like
    post no longer ranks the same as a 100K-view smash hit.
  - `_format_instagram_block` surfaces more of `video_analysis`
    (narrative_arc, pacing, cta, why_it_worked, audio.speech_language)
    instead of just hook + pattern.
  - `_format_instagram_scenes_block` includes `transcript_excerpt` so the
    SCRIPT phase has real dialogue exemplars to echo. When the run's
    dialogue_language=ar, the SCENES retrieval filters out English-only
    posts so the model doesn't read English transcripts while writing Arabic.
  - `_recent_ideas` powers the DON'T REPEAT THESE block — the run's own
    siblings + the last 30 days across all runs.
  - `build_context_block` now takes `dialogue_language` and `recent_avoid_ids`
    so callers can wire freshness + language-aware retrieval.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Optional, Sequence

from ...db import get_conn
from ...services.vertex_client import gemini_embed
from .prompts import DEFAULT_DIALOGUE_LANGUAGE, get_video_type

log = logging.getLogger("archivist.producer.memory")

# ----- IG rerank tunables --------------------------------------------------
# Over-fetch this many posts before reranking by hybrid score.
_IG_RERANK_OVERFETCH = 20
# Hybrid score weights — semantic relevance vs measured engagement.
_SEMANTIC_WEIGHT = 0.6
_ENGAGEMENT_WEIGHT = 0.4
# Comments are stronger intent than likes; views are common so they're scaled
# down. Tuned for the @42abudhabi corpus where view_count tops 400k while
# like_count tops out near 500.
_COMMENT_BOOST = 3.0
_VIEW_BOOST = 0.01


# -----------------------------------------------------------------------------
# Brand profile loading
# -----------------------------------------------------------------------------

async def _latest_brand_payload() -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT payload FROM brand_profile_latest WHERE kind='final_json' LIMIT 1"
            )
            row = await cur.fetchone()
    if not row:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload


# -----------------------------------------------------------------------------
# v5: HARD brand requirements for the user message
# -----------------------------------------------------------------------------

async def build_brand_requirements() -> dict:
    """Return the structured brand vocabulary the prompts must reference.

    All keys are always present (empty list / empty string when missing) so
    callers can rely on the shape without None-guarding everywhere.
    """
    payload = await _latest_brand_payload() or {}
    vi = payload.get("visual_identity") or {}
    voice = payload.get("voice") or {}
    return {
        "primary_colors":   list(vi.get("primary_colors") or []),
        "secondary_colors": list(vi.get("secondary_colors") or []),
        "fonts":            list(vi.get("fonts") or []),
        "style_pillars":    list(vi.get("style_pillars") or []),
        "voice_pillars":    list(voice.get("pillars") or []),
        "voice_do":         list(voice.get("do") or []),
        "voice_dont":       list(voice.get("dont") or []),
        "brand_dos":        list(payload.get("brand_dos") or []),
        "brand_donts":      list(payload.get("brand_donts") or []),
        "taglines":         list(payload.get("taglines_in_use") or []),
        "summary":          payload.get("brand_summary") or "",
        "notes":            payload.get("notes_for_producer_agent") or "",
    }


def format_brand_requirements(req: dict) -> str:
    """Render the requirements as a directive block for the USER message.

    This goes at the TOP of the user prompt so it gets maximum attention.
    """
    lines = [
        "BRAND REQUIREMENTS (your output MUST reflect these — they are not optional):",
    ]
    if req.get("primary_colors"):
        lines.append(
            "  PRIMARY PALETTE — use ≥2 in each scene's `palette` field, "
            "with HOW each shows up: " + ", ".join(req["primary_colors"])
        )
    if req.get("voice_pillars"):
        lines.append(
            "  VOICE PILLARS — echo ≥2 per idea in `brand_anchors.voice_pillars`: "
            + ", ".join(req["voice_pillars"])
        )
    if req.get("style_pillars"):
        lines.append(
            "  STYLE PILLARS — reflect ≥1 in `brand_motifs` / `brand_anchors.motif`: "
            + ", ".join(req["style_pillars"])
        )
    if req.get("taglines"):
        lines.append(
            "  LIVE TAGLINES — `brand_anchors.tagline_echo` may reference one (or be empty): "
            + ", ".join(req["taglines"])
        )
    if req.get("fonts"):
        # Truncate font list (it can include many weights/styles)
        font_short = req["fonts"][:4]
        lines.append(
            "  BRAND FONTS — use in `typography` when text is visible in-frame: "
            + ", ".join(font_short)
        )
    if req.get("brand_donts"):
        lines.append(
            "  AVOID (brand DONTs — must NOT appear in your output): "
            + " | ".join(req["brand_donts"])
        )
    if req.get("notes"):
        lines.append(f"  PRODUCER NOTES: {req['notes']}")
    lines.append(
        "  CULTURAL ANCHOR — every script scene needs a specific Abu Dhabi "
        "anchor (a named place, view, or interior). Never just \"city\" or \"skyline\"."
    )
    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------
# Soft context block (system prompt) — brand summary + taste memory
# -----------------------------------------------------------------------------

def _format_brand_summary_block(payload: dict | None) -> str:
    """Brief background flavor for the system prompt. The HARD requirements go
    in the user message via format_brand_requirements()."""
    if not payload:
        return (
            "BRAND IDENTITY: (no completed brand profile yet — work from "
            "the inline 42 Abu Dhabi primer)\n"
        )
    lines = ["BRAND BACKGROUND (for context — see user message for hard requirements):"]
    if payload.get("brand_summary"):
        lines.append(f"  Summary: {payload['brand_summary']}")
    voice = payload.get("voice") or {}
    if voice.get("do"):
        lines.append("  Voice DO: " + " | ".join(voice["do"][:5]))
    return "\n".join(lines) + "\n"


async def _nearest_ideas(query_vec: list[float], status: str, k: int = 3) -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, title, concept, visual_direction, decision_reason
                  FROM producer_ideas
                 WHERE status=%s AND embedding IS NOT NULL
                 ORDER BY embedding <=> %s::vector
                 LIMIT %s
                """,
                (status, query_vec, k),
            )
            return await cur.fetchall()


async def _nearest_scripts(
    query_vec: list[float],
    *,
    k: int = 3,
    only_approved: bool = True,
    video_type: str | None = None,
) -> list[dict]:
    """Top-k semantically similar past scripts. Defaults to approved-only so
    we surface positive examples; flip `only_approved=False` if you also want
    to learn from declined ones.

    When `video_type` is set (e.g. "talking_head"), the retrieval is further
    constrained to scripts whose owning run was tagged with that type — keeps
    retrieval on-genre when the caller knows the target output shape. Falls
    back to cross-type retrieval if no rows match (small video_type buckets).
    """
    where_parts = ["s.embedding IS NOT NULL"]
    if only_approved:
        where_parts.append("s.status = 'approved'")
    # Build params in the ORDER the placeholders appear in the SQL:
    # 1. video_type (when present) — placeholder in the WHERE clause
    # 2. query_vec — placeholder in the ORDER BY clause
    # 3. k — LIMIT placeholder
    params: list = []
    if video_type:
        where_parts.append("r.metadata->>'video_type' = %s")
        params.append(video_type)
    params.append(query_vec)
    params.append(k)
    where = "WHERE " + " AND ".join(where_parts)
    join_runs = (
        "LEFT JOIN producer_runs r ON r.id = i.run_id"
        if video_type
        else ""
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT s.id, s.idea_id, s.veo_prompt, s.status, s.decision_reason,
                       i.title AS idea_title, i.concept AS idea_concept
                  FROM producer_scripts s
                  LEFT JOIN producer_ideas i ON i.id = s.idea_id
                  {join_runs}
                 {where}
                 ORDER BY s.embedding <=> %s::vector
                 LIMIT %s
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
    # Fall back to cross-type when the strict filter found nothing — better
    # to seed with somewhat-related scripts than with nothing at all.
    if not rows and video_type:
        return await _nearest_scripts(
            query_vec, k=k, only_approved=only_approved, video_type=None,
        )
    return rows


def _engagement_score(like_count: int, comments_count: int, view_count: int) -> float:
    """Weighted engagement signal in raw (un-normalized) units."""
    return math.log1p(
        max(like_count, 0)
        + _COMMENT_BOOST * max(comments_count, 0)
        + _VIEW_BOOST * max(view_count, 0)
    )


async def _nearest_instagram_posts(
    query_vec: list[float],
    *,
    k: int = 5,
    min_engagement: int | None = None,
    dialogue_language: str | None = None,
) -> list[dict]:
    """Top-k IG posts by HYBRID similarity (60% caption embedding + 40%
    engagement). Over-fetches `_IG_RERANK_OVERFETCH` candidates by vector
    distance, then reranks in Python.

    Args:
        query_vec: 768d caption-embedding query.
        k: final number of posts to return after rerank.
        min_engagement: optional hard floor on (likes + comments).
            Default None — rerank already biases toward high performers.
        dialogue_language: when set to "ar", filters out posts whose
            audio_profile.speech_language is "en" or None — keeps the
            Arabic-language scripts from being fed English transcripts.

    The SELECT also pulls the richer video_analysis fields (narrative_arc,
    pacing, cta, why_it_worked, audio.speech_language) so the prompt block
    can surface them.
    """
    where = "WHERE caption_embedding IS NOT NULL"
    args: list = []
    if min_engagement is not None:
        where += " AND (COALESCE(like_count,0) + COALESCE(comments_count,0)) >= %s"
        args.append(int(min_engagement))
    if (dialogue_language or "").strip().lower() == "ar":
        # Bias against English-only posts for Arabic runs. Posts whose
        # speech_language is NULL pass through — the IG deepen phase doesn't
        # populate this field reliably today (105/106 deepened are NULL as
        # of writing), so a strict 'ar/mixed' filter would empty the IG
        # context entirely. Once video_deepen starts tagging speech_language
        # consistently, tighten this to IN ('ar','mixed').
        where += (
            " AND ((metadata->'video_analysis'->'audio_profile'->>'speech_language') "
            "IS NULL OR (metadata->'video_analysis'->'audio_profile'->>'speech_language') "
            "IN ('ar','mixed'))"
        )
    overfetch = max(int(k), _IG_RERANK_OVERFETCH)
    args.extend([query_vec, overfetch])

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT ig_media_id, media_type, media_product_type, caption,
                       permalink, timestamp_posted, like_count, comments_count,
                       view_count, ai_summary,
                       metadata->'video_analysis'->>'transferable_pattern'
                         AS transferable_pattern,
                       metadata->'video_analysis'->>'hook_description'
                         AS hook_description,
                       metadata->'video_analysis'->>'narrative_arc'
                         AS narrative_arc,
                       metadata->'video_analysis'->>'pacing'      AS pacing,
                       metadata->'video_analysis'->>'cta'         AS cta,
                       metadata->'video_analysis'->>'why_this_likely_worked'
                         AS why_it_worked,
                       metadata->'video_analysis'->'audio_profile'->>'speech_language'
                         AS speech_language,
                       (caption_embedding <=> %s::vector) AS cos_distance
                  FROM instagram_posts
                 {where}
                 ORDER BY caption_embedding <=> %s::vector
                 LIMIT %s
                """,
                tuple([query_vec, *args]),
            )
            candidates = await cur.fetchall() or []

    if not candidates:
        return []

    # Rerank: hybrid score = α * (1 - cos_distance) + β * (eng / max_eng).
    # max_eng is the largest engagement in THIS candidate set, so a post
    # whose absolute engagement is modest can still rank highly in a
    # niche topic where everything else is even smaller.
    eng_scores = [
        _engagement_score(
            int(c.get("like_count") or 0),
            int(c.get("comments_count") or 0),
            int(c.get("view_count") or 0),
        )
        for c in candidates
    ]
    max_eng = max(eng_scores) or 1.0
    for c, eng in zip(candidates, eng_scores):
        dist = float(c.get("cos_distance") or 1.0)
        # cos_distance is in [0, 2]; clamp + flip to similarity in [0, 1].
        sem = max(0.0, 1.0 - dist)
        c["_hybrid_score"] = (
            _SEMANTIC_WEIGHT * sem + _ENGAGEMENT_WEIGHT * (eng / max_eng)
        )

    candidates.sort(key=lambda c: c.get("_hybrid_score") or 0.0, reverse=True)
    return candidates[: max(k, 1)]


async def _list_enabled_characters() -> list[dict]:
    """All enabled character references, sorted for natural LRU rotation
    (within each archetype, never-used first, then oldest last-used)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, slug, display_name, archetype,
                       LEFT(ai_description, 220) AS short_desc,
                       attributes, usage_count, last_used_at
                  FROM producer_assets
                 WHERE category = 'character' AND enabled = TRUE
                 ORDER BY archetype,
                          last_used_at NULLS FIRST,
                          usage_count ASC,
                          id ASC
                """
            )
            return await cur.fetchall()


async def _nearest_instagram_scenes(
    query_vec: list[float],
    *,
    k: int = 3,
    dialogue_language: str | None = None,
) -> list[dict]:
    """Top-k IG video scenes by embedding similarity. Gives the producer
    beat-level reference (opening shots, payoffs, hook framings) — not just
    whole-video similarity.

    When `dialogue_language='ar'`, filters out scenes whose parent post's
    audio_profile.speech_language isn't Arabic/mixed, so an Arabic script
    isn't seeded with English transcript excerpts.
    """
    extra_where = ""
    if (dialogue_language or "").strip().lower() == "ar":
        # Same relaxed filter as _nearest_instagram_posts — NULL passes
        # through because video_deepen doesn't populate speech_language
        # reliably yet. Tighten to IN ('ar','mixed') once it does.
        extra_where = (
            " AND ((ip.metadata->'video_analysis'->'audio_profile'->>'speech_language') "
            "IS NULL OR (ip.metadata->'video_analysis'->'audio_profile'->>'speech_language') "
            "IN ('ar','mixed'))"
        )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT s.id, s.post_id, s.scene_index, s.start_sec, s.end_sec,
                       s.description, s.transcript_excerpt,
                       ip.ig_media_id, ip.permalink, ip.media_type,
                       ip.like_count, ip.comments_count, ip.view_count,
                       ip.metadata->'video_analysis'->'audio_profile'->>'speech_language'
                         AS speech_language
                  FROM instagram_video_scenes s
                  JOIN instagram_posts ip ON ip.id = s.post_id
                 WHERE s.embedding IS NOT NULL
                   {extra_where}
                 ORDER BY s.embedding <=> %s::vector
                 LIMIT %s
                """,
                (query_vec, k),
            )
            return await cur.fetchall()


async def _recent_ideas(
    run_id: int | None,
    *,
    same_run_limit: int = 10,
    cross_run_days: int = 30,
    cross_run_limit: int = 20,
) -> list[dict]:
    """Return ideas the model should NOT repeat: the run's own siblings +
    recent ideas across all runs. Used to build the AVOID block.

    Same-run ideas come first (highest novelty pressure), then cross-run
    by recency. Each row has id, title, concept (truncated), and run_id.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            same: list[dict] = []
            if run_id is not None:
                await cur.execute(
                    """
                    SELECT id, run_id, title, LEFT(concept, 180) AS concept
                      FROM producer_ideas
                     WHERE run_id = %s
                     ORDER BY id DESC
                     LIMIT %s
                    """,
                    (run_id, same_run_limit),
                )
                same = await cur.fetchall() or []

            # Cross-run: exclude the same-run ids we already pulled.
            same_ids = [r["id"] for r in same]
            await cur.execute(
                f"""
                SELECT id, run_id, title, LEFT(concept, 180) AS concept
                  FROM producer_ideas
                 WHERE created_at >= NOW() - INTERVAL '%s days'
                   {"AND id <> ALL(%s::bigint[])" if same_ids else ""}
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                tuple(
                    [cross_run_days]
                    + ([same_ids] if same_ids else [])
                    + [cross_run_limit],
                ),
            )
            recent = await cur.fetchall() or []
    # Dedup + preserve order (same-run first).
    seen: set[int] = set()
    out: list[dict] = []
    for r in (*same, *recent):
        rid = int(r["id"])
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


def _format_avoid_block(rows: list[dict]) -> str:
    """Render the DON'T REPEAT THESE block. Empty when there's nothing to avoid."""
    if not rows:
        return ""
    lines = [
        "DON'T REPEAT THESE — your batch MUST be materially different from "
        "the ideas below (recent ideas already generated for this brand):",
    ]
    for r in rows[:30]:
        title = (r.get("title") or "(untitled)").strip()
        concept = (r.get("concept") or "").strip().replace("\n", " ")
        if len(concept) > 140:
            concept = concept[:137] + "…"
        lines.append(f"  • {title} — {concept}")
    lines.append(
        "Different concept, different beat, different setting, different angle. "
        "Not a reskin. If your draft echoes one of these, change the spin."
    )
    return "\n".join(lines) + "\n"


def _format_taste_block(approved: list[dict], declined: list[dict]) -> str:
    lines: list[str] = []
    if approved:
        lines.append("USER TASTE — APPROVED PATTERNS (videos the user liked):")
        for r in approved:
            lines.append(f"  • {r['title']}: {r['concept']}")
    if declined:
        lines.append("USER TASTE — DECLINED PATTERNS (avoid these — reasons given):")
        for r in declined:
            reason = (r.get("decision_reason") or "").strip()
            tail = f" (reason: {reason})" if reason else ""
            lines.append(f"  • {r['title']}: {r['concept']}{tail}")
    if not lines:
        lines.append("USER TASTE: (no prior decisions yet — first runs)")
    return "\n".join(lines) + "\n"


def _format_script_block(scripts: list[dict]) -> str:
    if not scripts:
        return ""
    lines = [
        "PAST SCRIPTS THAT WORKED FOR THIS BRAND (use as structural reference, do not copy):",
    ]
    for r in scripts:
        title = (r.get("idea_title") or "(untitled)").strip()
        hook = (r.get("veo_prompt") or "").strip().replace("\n", " ")[:200]
        lines.append(f"  • {title} — {hook}")
    return "\n".join(lines) + "\n"


def _format_instagram_block(posts: list[dict]) -> str:
    if not posts:
        return ""
    lines = [
        "REAL @42abudhabi POSTS (audience-validated evidence — engagement in brackets, "
        "ranked by hybrid semantic+engagement score):",
    ]
    for r in posts:
        likes = r.get("like_count") or 0
        comments = r.get("comments_count") or 0
        views = r.get("view_count")
        mt = (r.get("media_type") or "?").lower()
        view_part = f" ▶{views}" if views else ""
        caption = (r.get("caption") or "").strip().replace("\n", " ")
        if len(caption) > 200:
            caption = caption[:197] + "…"
        ig_id = (r.get("ig_media_id") or "").strip()
        # The bracketed [ig_media_id=...] tag is the attribution handle —
        # IDEA_SYSTEM tells Gemini to copy this string into
        # `inspired_by_ig_post_id` when it riffs on this post.
        id_tag = f"[ig_media_id={ig_id}] " if ig_id else ""
        lines.append(
            f"  • {id_tag}[♥{likes} 💬{comments}{view_part}] {mt}: \"{caption}\""
        )
        # If the deep video-analysis is present, surface its dense fields —
        # this is the highest-signal data the producer can act on.
        hook = (r.get("hook_description") or "").strip()
        pattern = (r.get("transferable_pattern") or "").strip()
        arc = (r.get("narrative_arc") or "").strip()
        pacing = (r.get("pacing") or "").strip()
        cta = (r.get("cta") or "").strip()
        why = (r.get("why_it_worked") or "").strip()
        speech = (r.get("speech_language") or "").strip()
        if hook:
            lines.append(f"      HOOK: {hook}")
        if pattern:
            lines.append(f"      PATTERN: {pattern}")
        if arc:
            lines.append(f"      ARC: {arc}")
        if pacing or speech:
            bits = []
            if pacing:
                bits.append(f"pacing={pacing}")
            if speech:
                bits.append(f"speech={speech}")
            lines.append(f"      MEDIA: {', '.join(bits)}")
        if cta:
            lines.append(f"      CTA: {cta}")
        if why:
            # Truncate — sometimes Gemini's why_it_worked is paragraph-long.
            if len(why) > 220:
                why = why[:217] + "…"
            lines.append(f"      WHY IT WORKED: {why}")
    return "\n".join(lines) + "\n"


def _format_character_library_block(chars: list[dict]) -> str:
    """A roster of available characters grouped by archetype, with one-line
    tags. The Producer is told to NAME a specific character (#<id> <slug>)
    in the scene.subject — generate_script then resolves that to a
    reference_image_paths entry for Veo."""
    if not chars:
        return ""

    # Group by archetype (input is already sorted by archetype)
    by_arch: dict[str, list[dict]] = {}
    for c in chars:
        by_arch.setdefault(c["archetype"], []).append(c)

    lines = [
        "CHARACTER LIBRARY (rotate through — DO NOT repeat the same character "
        "back-to-back; pick by name in scene.subject as '#<id> <slug>'; "
        "least-recently-used is your default):",
    ]
    # Stable archetype order
    arch_order = ["emirati_male", "emirati_female", "casual_male", "casual_female"]
    for arch in arch_order:
        rows = by_arch.get(arch) or []
        if not rows:
            continue
        lines.append(f"  {arch.replace('_', ' ').upper()}:")
        for r in rows:
            tag_bits: list[str] = []
            attrs = r.get("attributes") or {}
            if arch == "emirati_male":
                k = attrs.get("kandura") or {}
                g = attrs.get("ghutra") or {}
                if r.get("attributes", {}).get("facial_hair"):
                    tag_bits.append(attrs["facial_hair"])
                if k.get("color"):
                    tag_bits.append(f"{k['color']} kandura")
                if g.get("pattern"):
                    tag_bits.append(f"{g['pattern']} ghutra")
            elif arch == "emirati_female":
                h = attrs.get("hijab") or {}
                a = attrs.get("abaya") or {}
                if h.get("color") or h.get("pattern"):
                    tag_bits.append(f"{h.get('color') or ''} {h.get('pattern') or ''}".strip() + " hijab")
                if a.get("color"):
                    tag_bits.append(f"{a['color']} abaya")
            else:  # casual_male / casual_female
                ar = attrs.get("attire") or {}
                if ar.get("top"):
                    tag_bits.append(ar["top"])
                if attrs.get("hair"):
                    tag_bits.append(attrs["hair"])
            if attrs.get("glasses") and attrs["glasses"] not in ("none", "None", None):
                tag_bits.append(attrs["glasses"])
            tag = "; ".join(t for t in tag_bits if t) or (r.get("short_desc") or "")
            usage = r.get("usage_count") or 0
            last_used = (
                fmt_rel := (lambda d: f", last used {d.isoformat()[:10]}" if d else "")
            )(r.get("last_used_at"))
            name = r.get("display_name") or r["slug"]
            lines.append(
                f"    • #{r['id']} {r['slug']} ({name}) — {tag} "
                f"[used {usage}×{last_used}]"
            )
    return "\n".join(lines) + "\n"


def _format_instagram_scenes_block(scenes: list[dict]) -> str:
    if not scenes:
        return ""
    lines = [
        "SCENES THAT MATCH THIS CONCEPT (specific beats from real @42abudhabi reels — "
        "SAYS: lines are real on-camera dialogue, echo the rhythm/vocabulary, do NOT copy):",
    ]
    for r in scenes:
        likes = r.get("like_count") or 0
        comments = r.get("comments_count") or 0
        start = float(r.get("start_sec") or 0)
        end = float(r.get("end_sec") or 0)
        desc = (r.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 240:
            desc = desc[:237] + "…"
        lines.append(
            f"  • [♥{likes} 💬{comments}] {int(start // 60)}:{int(start % 60):02d}"
            f"–{int(end // 60)}:{int(end % 60):02d} — {desc}"
        )
        # SAYS: per-scene transcript excerpt (when the IG video had speech).
        # This is the highest-signal input for the SCRIPT phase when writing
        # talking-head / voiceover_narrative dialogue.
        excerpt = (r.get("transcript_excerpt") or "").strip().replace("\n", " ")
        if excerpt:
            if len(excerpt) > 220:
                excerpt = excerpt[:217] + "…"
            speech = (r.get("speech_language") or "").strip()
            lang_tag = f" ({speech})" if speech else ""
            lines.append(f'      SAYS{lang_tag}: "{excerpt}"')
    return "\n".join(lines) + "\n"


async def build_context_block(
    query_text: str | None = None,
    *,
    k: int = 3,
    dialogue_language: str | None = None,
    avoid_recent_run_id: int | None = None,
    video_type: str | None = None,
    include_character_library: bool = True,
) -> str:
    """SOFT context for the system prompt: brand summary + DON'T REPEAT block
    + taste memory + similar past scripts + high-performing @42abudhabi posts
    + IG scene transcripts + character library.

    Hard brand requirements (hex codes, donts, etc.) live in
    `build_brand_requirements()` and are injected into the USER message.

    Args:
        query_text: free-text query (typically the run's title + brief).
            When None, all similarity-based blocks return empty.
        k: how many rows per nearest-neighbor block.
        dialogue_language: when "ar", filters IG retrieval to Arabic/mixed
            posts so an Arabic script isn't seeded with English transcripts.
        avoid_recent_run_id: run_id whose ideas should appear in the DON'T
            REPEAT block (plus the last 30 days across all runs). None to
            skip the AVOID block (e.g. for the SCRIPT phase, which works
            from one approved idea — repetition pressure is on ideas, not
            scripts).
        include_character_library: False leaves the CHARACTER LIBRARY roster
            out. Talking Head v2 passes False: it never resolves the
            '#<id> <slug>' tags the roster asks for, and the roster rows are
            frames of real students' interviews.
    """
    brand_payload = await _latest_brand_payload()
    brand_block = _format_brand_summary_block(brand_payload)

    # AVOID block — independent of query_text. Powers anti-repetition.
    avoid_block = ""
    if avoid_recent_run_id is not None:
        try:
            avoid_rows = await _recent_ideas(avoid_recent_run_id)
            avoid_block = _format_avoid_block(avoid_rows)
        except Exception:
            log.exception("recent-ideas lookup failed (non-fatal)")

    approved: list[dict] = []
    declined: list[dict] = []
    scripts: list[dict] = []
    ig_posts: list[dict] = []
    ig_scenes: list[dict] = []
    if query_text:
        try:
            vecs = await gemini_embed([query_text])
            if vecs and vecs[0]:
                qvec = vecs[0]
                approved = await _nearest_ideas(qvec, "approved", k=k)
                declined = await _nearest_ideas(qvec, "declined", k=k)
                # New: pull approved past scripts and high-engagement IG posts.
                # Both helpers degrade gracefully (empty list) if the source
                # table is empty or all embeddings are NULL.
                try:
                    scripts = await _nearest_scripts(
                        qvec, k=k, only_approved=True, video_type=video_type,
                    )
                except Exception:
                    log.exception("script retrieval failed (non-fatal)")
                try:
                    ig_posts = await _nearest_instagram_posts(
                        qvec, k=5, dialogue_language=dialogue_language,
                    )
                except Exception:
                    log.exception("instagram retrieval failed (non-fatal)")
                # v2 — beat-level retrieval. Only populated after `video_deepen`
                # has run on at least some IG videos. Silent no-op otherwise.
                try:
                    ig_scenes = await _nearest_instagram_scenes(
                        qvec, k=3, dialogue_language=dialogue_language,
                    )
                except Exception:
                    log.exception("instagram scene retrieval failed (non-fatal)")
        except Exception:
            log.exception("Taste-memory lookup failed (non-fatal)")

    taste_block = _format_taste_block(approved, declined)
    script_block = _format_script_block(scripts)
    ig_block = _format_instagram_block(ig_posts)
    ig_scenes_block = _format_instagram_scenes_block(ig_scenes)

    # v3 — character library is always loaded (small table; no query_text
    # dependency since rotation works without a semantic match). Producer
    # picks named characters by archetype-LRU.
    chars: list[dict] = []
    if include_character_library:
        try:
            chars = await _list_enabled_characters()
        except Exception:
            log.exception("character library lookup failed (non-fatal)")
    character_block = _format_character_library_block(chars)

    parts = [brand_block]
    if avoid_block:
        parts.append(avoid_block)
    parts.append(taste_block)
    if script_block:
        parts.append(script_block)
    if ig_block:
        parts.append(ig_block)
    if ig_scenes_block:
        parts.append(ig_scenes_block)
    if character_block:
        parts.append(character_block)
    return "\n" + "\n".join(parts) + "\n"


# -----------------------------------------------------------------------------
# Video type lookup + USER-message directive (v5.1)
# -----------------------------------------------------------------------------

async def _load_run_metadata(run_id: int) -> dict:
    """Fetch producer_runs.metadata as a dict (handles JSONB-as-string fallback)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT metadata FROM producer_runs WHERE id=%s", (run_id,)
            )
            row = await cur.fetchone()
    if not row:
        return {}
    md = row.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            return {}
    return md if isinstance(md, dict) else {}


async def get_run_video_type(run_id: int) -> dict | None:
    md = await _load_run_metadata(run_id)
    return get_video_type(md.get("video_type"))


async def get_run_video_type_key(run_id: int) -> str | None:
    """Return the raw video_type key string (e.g. 'talking_head') stored on
    the run, or None for generic / missing. Lets callers check key-based
    flags like DIALOGUE_REQUIRED_VIDEO_TYPES without re-deriving from the
    dict shape."""
    md = await _load_run_metadata(run_id)
    raw = (md.get("video_type") or "").strip()
    if not raw or raw == "generic":
        return None
    return raw


async def get_run_dialogue_language(run_id: int) -> str:
    """Return the run's dialogue_language ∈ {en, ar, mixed}.

    Falls back to DEFAULT_DIALOGUE_LANGUAGE when unset (backward-compatible
    with existing runs created before the flag existed).
    """
    md = await _load_run_metadata(run_id)
    raw = (md.get("dialogue_language") or "").strip().lower()
    from .prompts import DIALOGUE_LANGUAGE_ENUM  # local to dodge the obvious cycle
    if raw in DIALOGUE_LANGUAGE_ENUM:
        return raw
    return DEFAULT_DIALOGUE_LANGUAGE


def video_type_addon(vt: dict | None, key: str) -> str:
    """DEPRECATED — kept only so legacy callers don't break.

    v5.1 moves the video_type directive to the USER message via
    `format_video_type_directive()`. The old pattern of appending the addon
    to the SYSTEM prompt was the failure mode this v5.1 release fixes —
    LLMs treated it as background flavor. Always return empty so any lingering
    callers get a no-op."""
    return ""


def format_video_type_directive(vt: dict | None, kind: str) -> str:
    """Render the run's video_type addon as a USER-message directive block.

    Goes into the user prompt RIGHT AFTER the brand requirements (and before
    the idea/script payload). Gemini respects user-message demands far more
    than system-prompt context — this is the same pattern that made brand
    requirements actually land in v5 P0.

    Args:
        vt: the VIDEO_TYPES dict (or None for generic runs).
        kind: "idea" or "script" — picks idea_addon vs script_addon.

    Returns:
        A short block ready to interpolate into the user prompt, or empty
        string when there's no type set or no addon for this kind.
    """
    if not vt:
        return ""
    key = f"{kind}_addon"
    addon = (vt.get(key) or "").strip()
    if not addon:
        return ""
    return f"\n{addon}\n"


def video_type_allowed_subjects(vt: dict | None) -> set[str]:
    """Return the set of subject_type strings legal under this video_type.

    Empty set = no constraint (cinematic_hero, generic). Used by the
    ideas-validator to reject + retry batches that drift outside the
    requested type.
    """
    if not vt:
        return set()
    return set(vt.get("allowed_subject_types") or [])


def single_idea_subject_mismatch(
    idea: dict | None,
    vt: dict | None,
) -> tuple[bool, str]:
    """Check if a single idea's subject_type violates the run's video_type.

    Used by `user_idea` and `reference_idea` (which produce ONE idea each, so
    they don't run through the batch validator in `generate_ideas`).

    Returns (mismatched, feedback_block). When mismatched=True, the caller
    fires ONE retry passing `feedback_block` into the user prompt.
    """
    if not idea or not isinstance(idea, dict):
        return False, ""
    allowed = video_type_allowed_subjects(vt)
    if not allowed:
        return False, ""
    stype = (idea.get("subject_type") or "").strip()
    if not stype or stype in allowed:
        return False, ""
    allowed_str = ", ".join(sorted(allowed))
    feedback = (
        "YOUR PREVIOUS OUTPUT WAS REJECTED. Regenerate.\n"
        f"VIDEO TYPE VIOLATION — your idea had subject_type='{stype}' which "
        f"is NOT in the allowed set ({allowed_str}) for this video_type.\n"
        f"The new idea MUST have subject_type ∈ {{{allowed_str}}}. If the "
        "user's rough idea seems incompatible with the type, bend it to fit "
        "the type — don't drop the type. The type was chosen deliberately."
    )
    return True, feedback
