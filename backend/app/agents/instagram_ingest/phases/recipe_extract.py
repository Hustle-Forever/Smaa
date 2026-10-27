"""Phase 4c — recipe_extract (runs after video_deepen).

For each deepened IG video post (metadata.video_deepened_at set, not yet
recipe-extracted), compose a per-reel "edit recipe" — pacing, cut times,
popup/on-screen-text timing, intro/outro cards, transitions, music — from:

  - metadata.video_analysis (pacing, hook, cta, on_screen_text,
    intro_card/outro_card, audio_profile, scene_edit_cues), and
  - the instagram_video_scenes rows (cut times from scene boundaries,
    scene_count, duration),

then store it into the edit-recipe RAG (services.yas_edit_recipes.store_recipe,
source="instagram") so the AI editor can learn editing STYLE from scraped
reels that performed well.

HONEST CAVEAT — scraped recipes are CADENCE hints, not ground truth:
  - popup timings come from per-scene still-frame analysis → ±1-2s accurate;
  - transition typing (fade vs swipe vs zoom) inferred from stills is NOISY;
  - older deepened posts have no scene_edit_cues, so popups fall back to the
    whole-video on_screen_text list spread across scene starts (even fuzzier).
Each recipe is marked timing_precision="approximate" and popups carry
approx=True so consumers treat them as style/cadence signal only.

engagement_score = log1p(views) + 3*log1p(likes) + 5*log1p(comments)
(normalized blend; re-ranked later by nearest_recipes' hybrid scorer).

Idempotent via metadata.recipe_extracted_at (same pattern as
video_deepened_at). EVERYTHING here is best-effort and additive: a per-post
failure never aborts the batch, and a phase-level failure never fails the
ingest run (we log + return a PhaseResult instead of raising).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....services import yas_edit_recipes
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.ig_recipe_extract")


# Pacing buckets by average scene length — mirrors yas_edit_recipes' buckets
# so editor-derived and IG-derived recipes speak the same vocabulary.
_PACING_FAST_MAX_SEC = 2.5
_PACING_MEDIUM_MAX_SEC = 6.0

# video_deepen pacing labels → recipe pacing vocabulary (fallback when no
# scene rows are available to derive pacing from durations).
_ANALYSIS_PACING_MAP = {
    "fast-cut": "fast",
    "rhythmic-cuts": "medium",
    "single-take": "slow",
    "long-hold": "slow",
    "mixed": "medium",
}

# Scene-cue transition_in → editor transition preset vocabulary
# (yas_video_edit_ai_ops._TRANSITION_PRESETS). "zoom" has no editor preset;
# keep the raw label — the recipe block is a prompt-side cadence hint and the
# planner validates its own presets anyway.
_TRANSITION_PRESET_MAP = {
    "fade": "fade",
    "swipe": "slide",
    "zoom": "zoom",
}

_DEFAULT_TRANSITION_FRAMES = 8  # ~0.27s @30fps — typical reel cross-cut


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _pending_posts() -> list[dict]:
    """Deepened video posts (with a parsed analysis) not yet recipe-extracted."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ip.id            AS post_id,
                       ip.caption,
                       ip.like_count,
                       ip.comments_count,
                       ip.view_count,
                       ip.metadata->'video_analysis' AS video_analysis
                  FROM instagram_posts ip
                 WHERE COALESCE(ip.metadata->>'video_deepened_at', '') <> ''
                   AND (ip.metadata->'video_analysis' ? 'narrative_arc')
                   AND NOT (ip.metadata->'video_analysis' ? '_raw')
                   AND COALESCE(ip.metadata->>'recipe_extracted_at', '') = ''
                 ORDER BY ip.timestamp_posted DESC NULLS LAST, ip.id DESC
                """
            )
            return await cur.fetchall()


async def _post_scenes(post_id: int) -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT scene_index, start_sec, end_sec
                  FROM instagram_video_scenes
                 WHERE post_id = %s
                 ORDER BY scene_index
                """,
                (post_id,),
            )
            return await cur.fetchall()


async def _stamp_extracted(post_id: int) -> None:
    """metadata.recipe_extracted_at — top-level key, so the shallow `||`
    merge is safe (no sibling-key clobbering)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE instagram_posts SET metadata = metadata || %s::jsonb "
                "WHERE id = %s",
                (
                    Jsonb({
                        "recipe_extracted_at":
                            datetime.now(timezone.utc).isoformat(),
                    }),
                    post_id,
                ),
            )


# ---------------------------------------------------------------------------
# Recipe composition (pure — no DB, no LLM; unit-testable)
# ---------------------------------------------------------------------------

def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def engagement_score(
    likes: int | None, comments: int | None, views: int | None,
) -> float:
    """Normalized engagement blend (re-ranked later by nearest_recipes):
    log1p(views) + 3*log1p(likes) + 5*log1p(comments)."""
    return (
        math.log1p(max(0, int(views or 0)))
        + 3.0 * math.log1p(max(0, int(likes or 0)))
        + 5.0 * math.log1p(max(0, int(comments or 0)))
    )


def compose_instagram_recipe(
    *,
    caption: str | None,
    analysis: dict | None,
    scenes: list[dict],
    likes: int | None = None,
    comments: int | None = None,
    views: int | None = None,
) -> dict:
    """Compose an edit recipe (yas_edit_recipes shape) from a deepened post.

    All timing-bearing fields are APPROXIMATE — popups ±1-2s, transitions
    typed from stills — and flagged as such; the recipe is a cadence/style
    hint for the AI editor, never a frame-accurate ground truth.
    """
    analysis = analysis if isinstance(analysis, dict) else {}
    scenes = sorted(
        (s for s in (scenes or []) if isinstance(s, dict)),
        key=lambda s: int(s.get("scene_index") or 0),
    )

    # --- duration / cuts / pacing from scene boundaries --------------------
    duration_sec = round(max((_f(s.get("end_sec")) for s in scenes), default=0.0), 2)
    scene_count = len(scenes) or int(analysis.get("scene_count") or 0)
    cut_times_sec = [round(_f(s.get("start_sec")), 2) for s in scenes[1:]]

    if scenes and duration_sec > 0:
        avg_sec = duration_sec / len(scenes)
        if avg_sec <= _PACING_FAST_MAX_SEC:
            pacing = "fast"
        elif avg_sec <= _PACING_MEDIUM_MAX_SEC:
            pacing = "medium"
        else:
            pacing = "slow"
    else:
        pacing = _ANALYSIS_PACING_MAP.get(
            str(analysis.get("pacing") or "").strip().lower(), "medium",
        )

    # --- popups from per-scene edit cues (preferred) ------------------------
    cues = [c for c in (analysis.get("scene_edit_cues") or []) if isinstance(c, dict)]
    cues.sort(key=lambda c: int(c.get("scene_index") or 0))
    popups: list[dict] = []
    for cue in cues:
        sc_start = _f(cue.get("start_sec"))
        sc_end = max(_f(cue.get("end_sec")), sc_start)
        for ev in (cue.get("on_screen_text_events") or []):
            if not isinstance(ev, dict):
                continue
            text = str(ev.get("text") or "").strip()
            if not text:
                continue
            start = _f(ev.get("approx_start_sec"), sc_start)
            start = min(max(start, sc_start), sc_end)
            # No observed end time — assume a short overlay capped to the
            # scene (cadence hint; approx by construction).
            dur = min(2.5, max(0.8, sc_end - start)) if sc_end > sc_start else 2.0
            pos = str(ev.get("position") or "center")
            popups.append({
                "text": text[:120],
                "start_sec": round(start, 2),
                "dur_sec": round(dur, 2),
                "position": pos if pos in ("top", "center", "lower") else "center",
                "approx": True,  # ±1-2s — still-frame derived
            })

    # Fallback for posts deepened BEFORE scene_edit_cues existed: spread the
    # whole-video on_screen_text strings over scene starts in order. Even
    # fuzzier — order is right, timing is a guess.
    if not popups:
        texts = [
            str(t or "").strip()
            for t in (analysis.get("on_screen_text") or [])
            if str(t or "").strip()
        ]
        if texts and scenes:
            for i, text in enumerate(texts[: len(scenes)]):
                sc = scenes[i]
                start = _f(sc.get("start_sec"))
                end = max(_f(sc.get("end_sec")), start)
                popups.append({
                    "text": text[:120],
                    "start_sec": round(start, 2),
                    "dur_sec": round(min(2.5, max(0.8, end - start)), 2),
                    "position": "center",
                    "approx": True,
                })
    popups = popups[:12]

    # --- intro / outro cards -------------------------------------------------
    analysis_intro = analysis.get("intro_card") or {}
    analysis_outro = analysis.get("outro_card") or {}
    intro = {"present": False, "style": None, "text": None}
    outro = {"present": False, "cta": None}

    intro_cue = next((c for c in cues if c.get("is_intro_card")), None)
    if intro_cue is not None:
        first_ev = next(
            (e for e in (intro_cue.get("on_screen_text_events") or [])
             if isinstance(e, dict) and str(e.get("text") or "").strip()),
            None,
        )
        intro = {
            "present": True,
            "style": "title_card",
            "text": (str(first_ev["text"]).strip()[:120] if first_ev else None),
        }
    elif analysis_intro.get("present"):
        style = str(analysis_intro.get("style") or "title_card").strip() or "title_card"
        intro = {
            "present": True,
            "style": style if style != "none" else "title_card",
            "text": (str(analysis_intro.get("text") or "").strip()[:120] or None),
        }

    outro_cue = next((c for c in reversed(cues) if c.get("is_outro_card")), None)
    cta_text = str(analysis.get("cta") or "").strip()[:160] or None
    if outro_cue is not None:
        last_ev = next(
            (e for e in (outro_cue.get("on_screen_text_events") or [])
             if isinstance(e, dict) and str(e.get("text") or "").strip()),
            None,
        )
        outro = {
            "present": True,
            "cta": (str(last_ev["text"]).strip()[:160] if last_ev else cta_text),
        }
    elif analysis_outro.get("present"):
        outro = {
            "present": True,
            "cta": str(analysis_outro.get("cta") or "").strip()[:160] or cta_text,
        }

    # --- transitions (typed from boundary stills — NOISY) -------------------
    transitions: list[dict] = []
    for cue in cues:
        idx = int(cue.get("scene_index") or 0)
        if idx <= 0:
            continue
        trans = str(cue.get("transition_in") or "").strip().lower()
        if trans in ("", "cut", "none"):
            continue
        transitions.append({
            "after_index": idx - 1,
            "preset": _TRANSITION_PRESET_MAP.get(trans, trans),
            "dur_frames": _DEFAULT_TRANSITION_FRAMES,
            "approx": True,  # typed from still frames — treat as a hint
        })

    # --- music ---------------------------------------------------------------
    audio = analysis.get("audio_profile") or {}
    music_present = bool(audio.get("music_present"))
    energy = str(audio.get("energy") or "").strip().lower()
    music = {
        "present": music_present,
        "energy": energy if (music_present and energy in ("low", "medium", "high")) else None,
        "mood": None,
    }

    # --- topic summary (retrieval key: what the reel is ABOUT) --------------
    topic_bits = [
        str(caption or "").strip(),
        str(analysis.get("narrative_arc") or "").strip(),
        str(analysis.get("transferable_pattern") or "").strip(),
    ]
    topic_summary = " — ".join(b for b in topic_bits if b)[:400]

    return {
        "source": "instagram",
        "topic_summary": topic_summary,
        "duration_sec": duration_sec,
        "scene_count": scene_count,
        "pacing": pacing,
        "cut_times_sec": cut_times_sec[:40],
        "intro": intro,
        "outro": outro,
        "popups": popups,
        "transitions": transitions[:40],
        "music": music,
        "engagement": {
            "likes": int(likes or 0),
            "comments": int(comments or 0),
            "views": int(views or 0),
        },
        # Cadence hint, NOT ground truth: popup timing ±1-2s, transition
        # typing from stills.
        "timing_precision": "approximate",
    }


# ---------------------------------------------------------------------------
# Phase entry point — best-effort all the way down
# ---------------------------------------------------------------------------

async def _process_post(post: dict) -> str:
    """Returns 'completed' | 'failed'. Never raises."""
    post_id = int(post["post_id"])
    try:
        scenes = await _post_scenes(post_id)
        recipe = compose_instagram_recipe(
            caption=post.get("caption"),
            analysis=post.get("video_analysis"),
            scenes=scenes,
            likes=post.get("like_count"),
            comments=post.get("comments_count"),
            views=post.get("view_count"),
        )
        score = engagement_score(
            post.get("like_count"), post.get("comments_count"), post.get("view_count"),
        )
        # store_recipe is best-effort by contract (logs + no-ops on failure).
        await yas_edit_recipes.store_recipe(
            source="instagram",
            source_post_id=post_id,
            recipe=recipe,
            engagement_score=score,
        )
        await _stamp_extracted(post_id)
        log.info(
            "recipe extracted for post #%d: %d scenes, %d popups, %d transitions, "
            "pacing=%s, engagement=%.2f",
            post_id, recipe["scene_count"], len(recipe["popups"]),
            len(recipe["transitions"]), recipe["pacing"], score,
        )
        return "completed"
    except Exception:  # noqa: BLE001 — one bad post must not abort the batch
        log.exception("recipe_extract failed for post #%d (non-fatal)", post_id)
        return "failed"


async def run(ctx: PhaseContext) -> PhaseResult:
    try:
        pending = await _pending_posts()
    except Exception:  # noqa: BLE001 — additive phase: never fail the run
        log.exception("recipe_extract: pending query failed (non-fatal)")
        return PhaseResult(name="recipe_extract", total=0)

    if not pending:
        log.info("[run=%s] recipe_extract: nothing pending", ctx.run_id)
        return PhaseResult(name="recipe_extract", total=0)

    log.info("[run=%s] recipe_extract: %d posts", ctx.run_id, len(pending))
    processed = 0
    failed = 0
    for post in pending:
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("recipe_extract cancelled mid-batch")
        outcome = await _process_post(post)
        if outcome == "completed":
            processed += 1
        else:
            failed += 1
        if ctx.report_progress:
            ctx.report_progress("recipe_extract", processed + failed, len(pending))

    log.info(
        "[run=%s] recipe_extract done: %d ok, %d failed",
        ctx.run_id, processed, failed,
    )
    return PhaseResult(
        name="recipe_extract",
        processed=processed, failed=failed, total=len(pending),
    )
