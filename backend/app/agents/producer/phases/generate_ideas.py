"""Mode 1 (Auto): generate N video ideas via Gemini structured JSON.

v5.1 changes:
  - Brand requirements + video_type directive both injected into the USER
    message (not the system prompt). LLMs respect user demands far more
    than system flavor.
  - Two-pass validation: after Gemini returns, check (a) subject_type
    adherence against the run's video_type allowed set, and (b) batch
    diversity (multiple distinct moods, and multiple subject_types when
    the type doesn't force a single one). One retry on failure — never
    infinite-loop.
  - mood, subject_type, brand_anchors all persisted to producer_ideas.metadata
    so downstream phases can read them back without re-asking Gemini.

v6 changes (IG-inspiration + freshness):
  - Derives a real `query_text` from run.title + run.notes + video_type
    label so build_context_block actually fires the IG / past-scripts /
    past-ideas similarity blocks (was hardcoded None).
  - Reads producer_runs.metadata.dialogue_language and passes it through
    so IG retrieval can filter by speech language for Arabic runs.
  - Pulls the AVOID block (this run's siblings + 30d cross-run) so the
    model knows what NOT to repeat.
  - Post-generation dedup guard: any new idea with cosine_sim > 0.88 to a
    recent producer_ideas embedding triggers one regeneration retry with
    the offending ones listed in the user prompt.
  - Persists novelty_angle + inspired_by_ig_post_id into metadata.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....services.vertex_client import gemini_embed, gemini_text
from ..memory import (
    build_brand_requirements,
    build_context_block,
    format_brand_requirements,
    format_video_type_directive,
    get_run_dialogue_language,
    get_run_video_type,
    video_type_allowed_subjects,
)
from ..prompts import IDEA_SCHEMA, IDEA_SYSTEM

log = logging.getLogger("archivist.producer.generate_ideas")

DEFAULT_N = 5
# Cosine-similarity floor above which a new idea is considered a repeat of
# a recent one and triggers a regeneration retry. Tuned conservatively —
# 0.88+ means the new idea is essentially a paraphrase of a prior one.
_DEDUP_SIM_THRESHOLD = 0.88
# How many recent ideas to embed-compare against for the dedup guard.
# Bounded so a long backlog doesn't make this O(N²).
_DEDUP_LOOKBACK = 40


async def _build_query_text(run_id: int) -> str:
    """Compose the embedding query that drives IG / past-scripts retrieval.

    Pulls the run's title + any notes from metadata + the video_type's
    human-readable label. Empty title is OK — the type label alone still
    gives the retrieval a useful signal.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT title, metadata FROM producer_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if not row:
        return ""
    title = (row.get("title") or "").strip()
    md = row.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            md = {}
    notes = (md.get("notes") or "").strip() if isinstance(md, dict) else ""
    vt = await get_run_video_type(run_id)
    vt_label = (vt.get("name") if vt else "") or ""
    parts = [p for p in (vt_label, title, notes) if p]
    return ". ".join(parts) if parts else "42 Abu Dhabi short-form social video"


def _cosine_sim(a: Any, b: Any) -> float:
    """Cosine similarity for two iterables of floats. Returns 0.0 on
    mismatched shape / empty input (defensive — embeddings can be None)."""
    try:
        av = list(a or [])
        bv = list(b or [])
    except TypeError:
        return 0.0
    if not av or not bv or len(av) != len(bv):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(av, bv):
        try:
            xf = float(x)
            yf = float(y)
        except (TypeError, ValueError):
            return 0.0
        dot += xf * yf
        na += xf * xf
        nb += yf * yf
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


async def _load_recent_embeddings(run_id: int) -> list[dict]:
    """Pull recent producer_ideas embeddings for the dedup guard. Same-run
    siblings first (highest pressure), then cross-run by recency."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, title, embedding
                  FROM producer_ideas
                 WHERE embedding IS NOT NULL
                   AND (run_id = %s OR created_at >= NOW() - INTERVAL '30 days')
                 ORDER BY (run_id = %s) DESC, created_at DESC
                 LIMIT %s
                """,
                (run_id, run_id, _DEDUP_LOOKBACK),
            )
            rows = await cur.fetchall() or []
    # psycopg returns pgvector columns as numpy.ndarray (or list) — both work
    # with _cosine_sim's defensive coercion.
    return rows


def _dedup_feedback(
    repeats: list[tuple[int, str, str, float]],
) -> str:
    """Build the user-message feedback block for the dedup retry. Each tuple
    is (new_idea_index, new_title, repeat_of_title, similarity)."""
    if not repeats:
        return ""
    lines = [
        "YOUR PREVIOUS OUTPUT WAS REJECTED for repetition. Regenerate the ENTIRE batch.",
        "REPETITION VIOLATION — these new ideas were too similar to recent existing ones:",
    ]
    for new_idx, new_title, old_title, sim in repeats:
        lines.append(
            f"  - New idea #{new_idx} '{new_title}' is "
            f"{sim:.2f} cosine-similar to a recent idea '{old_title}'."
        )
    lines.append(
        "Pick materially different concepts, beats, settings, or angles. "
        "If the same theme keeps surfacing, change the SUBJECT — different "
        "character, different time of day, different camera, different hook. "
        "Every new idea MUST have a clear, articulable `novelty_angle`."
    )
    return "\n".join(lines) + "\n"


async def run(run_id: int, *, count: int = DEFAULT_N) -> list[dict]:
    """Generate `count` ideas for run_id and persist them. Returns the rows."""
    query_text = await _build_query_text(run_id)
    dialogue_language = await get_run_dialogue_language(run_id)
    context = await build_context_block(
        query_text=query_text,
        dialogue_language=dialogue_language,
        avoid_recent_run_id=run_id,
    )
    brand_req = await build_brand_requirements()
    req_block = format_brand_requirements(brand_req)
    vt = await get_run_video_type(run_id)
    vt_directive = format_video_type_directive(vt, "idea")
    allowed_subjects = video_type_allowed_subjects(vt)

    # USER prompt: requirements FIRST (highest LLM attention), then soft
    # taste/brand context, then the task line.
    base_user_prompt = (
        f"{req_block}\n"
        f"{vt_directive}\n"
        f"{context}\n"
        f"Generate exactly {count} short-form video ideas for 42 Abu Dhabi. "
        "Apply the 5 RULES OF A SHOOTABLE BEAT to each. "
        "Fill `novelty_angle` on every idea. When an idea is clearly inspired "
        "by ONE of the IG posts in the context, set `inspired_by_ig_post_id` "
        "to that post's [ig_media_id=...] tag value. "
        "Return a JSON object matching the provided schema, no prose."
    )

    ideas = await _call(base_user_prompt)
    if not ideas:
        return []

    # Validator: subject_type adherence + batch diversity. One retry.
    # When video_type forces a narrow subject set (e.g. talking_head ⇒
    # solo_speaker only), diversity is NOT expected on subject_type.
    expect_subject_diversity = (not allowed_subjects) or len(allowed_subjects) >= 3
    mismatches, lack_div = _validate(ideas, allowed_subjects, expect_subject_diversity)
    if mismatches or lack_div:
        feedback = _feedback_block(mismatches, lack_div, allowed_subjects, expect_subject_diversity)
        log.info(
            "[run=%d] retrying ideas (mismatches=%d, lack_div=%s)",
            run_id, len(mismatches), lack_div,
        )
        retry = await _call(base_user_prompt + "\n" + feedback)
        if retry:
            ideas = retry

    # Embed the new batch ONCE — used for both dedup-vs-recent and final persist.
    embed_texts = [
        f"{i.get('title','')}. {i.get('concept','')} {i.get('visual_direction','')}"
        for i in ideas
    ]
    embeddings = await gemini_embed(embed_texts) if embed_texts else []
    if len(embeddings) != len(ideas):
        log.warning("embedding count mismatch: %d ideas vs %d embeddings", len(ideas), len(embeddings))
        embeddings = embeddings + [None] * (len(ideas) - len(embeddings))

    # Dedup guard — compare each new idea against recent producer_ideas.
    # When ≥ _DEDUP_SIM_THRESHOLD to ANY recent, mark it as a repeat.
    # One retry max (reuse the existing single-retry pattern).
    repeats: list[tuple[int, str, str, float]] = []
    try:
        recent = await _load_recent_embeddings(run_id)
    except Exception:
        log.exception("dedup recent lookup failed (non-fatal)")
        recent = []
    if recent:
        for idx, (idea, emb) in enumerate(zip(ideas, embeddings), start=1):
            if not emb:
                continue
            best_sim = 0.0
            best_title = ""
            for r in recent:
                sim = _cosine_sim(emb, r.get("embedding"))
                if sim > best_sim:
                    best_sim = sim
                    best_title = r.get("title") or ""
            if best_sim >= _DEDUP_SIM_THRESHOLD:
                repeats.append((idx, idea.get("title") or "?", best_title, best_sim))

    if repeats:
        log.info(
            "[run=%d] dedup retry — %d repeat ideas detected (threshold=%.2f)",
            run_id, len(repeats), _DEDUP_SIM_THRESHOLD,
        )
        retry_prompt = base_user_prompt + "\n" + _dedup_feedback(repeats)
        retry_ideas = await _call(retry_prompt)
        if retry_ideas:
            ideas = retry_ideas
            # Re-embed the replacement batch.
            embed_texts = [
                f"{i.get('title','')}. {i.get('concept','')} {i.get('visual_direction','')}"
                for i in ideas
            ]
            try:
                embeddings = await gemini_embed(embed_texts) if embed_texts else []
            except Exception:
                log.exception("re-embed after dedup retry failed")
                embeddings = [None] * len(ideas)
            if len(embeddings) != len(ideas):
                embeddings = embeddings + [None] * (len(ideas) - len(embeddings))

    inserted: list[dict] = []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            for idx, (idea, emb) in enumerate(zip(ideas, embeddings), start=1):
                await cur.execute(
                    """
                    INSERT INTO producer_ideas
                      (run_id, idea_index, title, concept, visual_direction,
                       reason, style_mood, embedding, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, idea_index, title, concept, visual_direction,
                              reason, style_mood, status, created_at
                    """,
                    (
                        run_id,
                        idx,
                        (idea.get("title") or "").strip()[:200],
                        (idea.get("concept") or "").strip(),
                        (idea.get("visual_direction") or "").strip(),
                        (idea.get("reason") or "").strip(),
                        (idea.get("style_mood") or "").strip()[:200],
                        emb,
                        Jsonb({
                            "source": "auto",
                            "mood": (idea.get("mood") or "").strip() or None,
                            "subject_type": (idea.get("subject_type") or "").strip() or None,
                            "brand_anchors": idea.get("brand_anchors") or {},
                            "novelty_angle": (idea.get("novelty_angle") or "").strip() or None,
                            "inspired_by_ig_post_id": (
                                (idea.get("inspired_by_ig_post_id") or "").strip() or None
                            ),
                        }),
                    ),
                )
                inserted.append(await cur.fetchone())

            await cur.execute(
                "UPDATE producer_runs SET status='ideas_pending' WHERE id=%s",
                (run_id,),
            )
    log.info("[run=%d] generated %d ideas", run_id, len(inserted))
    return inserted


async def _call(user_prompt: str) -> list[dict]:
    """One Gemini call returning the `ideas` array (possibly empty)."""
    data = await gemini_text(
        user_prompt,
        system=IDEA_SYSTEM,
        json_schema=IDEA_SCHEMA,
        temperature=0.8,
        max_tokens=5000,
    )
    if not isinstance(data, dict):
        log.warning("gemini returned non-dict; raw=%s", str(data)[:300])
        return []
    ideas = data.get("ideas")
    if not isinstance(ideas, list) or not ideas:
        log.warning("gemini returned no ideas; raw=%s", str(data)[:300])
        return []
    return ideas


def _validate(
    ideas: list[dict],
    allowed_subjects: set[str],
    expect_subject_diversity: bool,
) -> tuple[list[tuple[int, str, str]], bool]:
    """Return (subject_type_mismatches, lack_of_diversity).

    mismatches: [(idx, title, found_subject_type), ...] when video_type
                requires a specific set of subject_types and an idea
                drifts outside it.
    lack_diversity: True if all ideas share a single mood, or — when the
                    type permits diverse subjects — all share a single
                    subject_type.
    """
    mismatches: list[tuple[int, str, str]] = []
    if allowed_subjects:
        for i, idea in enumerate(ideas, start=1):
            stype = (idea.get("subject_type") or "").strip()
            if stype and stype not in allowed_subjects:
                mismatches.append((i, idea.get("title") or "?", stype))

    lack_div = False
    if len(ideas) >= 3:
        moods = {(idea.get("mood") or "").strip() for idea in ideas}
        moods.discard("")
        if len(moods) == 1:
            lack_div = True
        if expect_subject_diversity:
            subjects = {(idea.get("subject_type") or "").strip() for idea in ideas}
            subjects.discard("")
            if len(subjects) == 1:
                lack_div = True

    return mismatches, lack_div


def _feedback_block(
    mismatches: list[tuple[int, str, str]],
    lack_diversity: bool,
    allowed_subjects: set[str],
    expect_subject_diversity: bool,
) -> str:
    """Build a strong feedback block telling Gemini what to fix on retry."""
    lines = ["YOUR PREVIOUS OUTPUT WAS REJECTED. Regenerate the ENTIRE batch."]
    if mismatches:
        allowed_str = ", ".join(sorted(allowed_subjects)) or "(any)"
        lines.append(
            f"VIDEO TYPE VIOLATION — these ideas had subject_type outside "
            f"the allowed set ({allowed_str}):"
        )
        for idx, title, stype in mismatches:
            lines.append(f"  - Idea #{idx} '{title}': subject_type='{stype}' is NOT allowed.")
        lines.append("Every idea in the new batch MUST have subject_type ∈ {" + allowed_str + "}.")
    if lack_diversity:
        if expect_subject_diversity:
            lines.append(
                "DIVERSITY VIOLATION — your batch had every idea on the same "
                "mood AND/OR the same subject_type. Spread the batch across "
                "≥3 distinct mood values and ≥3 distinct subject_type values."
            )
        else:
            lines.append(
                "DIVERSITY VIOLATION — your batch had every idea on the same "
                "mood. Spread the batch across ≥3 distinct mood values."
            )
    return "\n".join(lines) + "\n"
