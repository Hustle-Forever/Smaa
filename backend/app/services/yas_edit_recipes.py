"""Edit-recipe RAG — the AI editor learns editing STYLE from past edits.

Mirrors the proven Talking-Head-v2 past-reel-RAG pattern
(yas_talking_head_v2._ensure_script_embedding_column / _nearest_th_scripts /
_format_past_th_block):

  serialize_editor_recipe() — DETERMINISTIC (no LLM) distillation of a saved
                              metadata.video_edit timeline into a compact
                              "recipe": pacing, cut times, popups, intro/outro,
                              transitions, music.
  store_recipe()            — embed (gemini_embed, 768d) + UPSERT into the
                              edit_recipes table (pgvector).
  nearest_recipes()         — top-k recipes by cosine similarity, hybrid
                              re-ranked with engagement_score.
  format_recipe_block()     — a compact prompt section framed "learn the
                              CADENCE/STRUCTURE, NOT the topic" so retrieved
                              recipes influence style only; every concrete op
                              still has to come from the CURRENT video's
                              candidate windows + clip ids.

All DB paths are best-effort: any failure logs and degrades (store → no-op,
retrieve → []) — recipe RAG must never fail or 500 an edit job.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn
from .vertex_client import gemini_embed

log = logging.getLogger("archivist.yas_edit_recipes")

FPS = 30

# Pacing buckets from the average video-clip length (seconds).
_PACING_FAST_MAX_SEC = 2.5
_PACING_MEDIUM_MAX_SEC = 6.0


# ---------------------------------------------------------------------------
# Table — lazy CREATE, once per process (TH-v2 _ensure_* pattern)
# ---------------------------------------------------------------------------

_TABLE_READY = False

_DDL = """
CREATE TABLE IF NOT EXISTS edit_recipes (
    id                   bigserial PRIMARY KEY,
    source               text NOT NULL,
    source_post_id       bigint,
    source_generation_id bigint,
    recipe               jsonb NOT NULL,
    embed_text           text,
    embedding            vector(768),
    engagement_score     real DEFAULT 0,
    created_at           timestamptz DEFAULT now()
)
"""

_IDX_GENERATION = """
CREATE UNIQUE INDEX IF NOT EXISTS edit_recipes_source_generation_uq
    ON edit_recipes (source, source_generation_id)
 WHERE source_generation_id IS NOT NULL
"""

_IDX_POST = """
CREATE UNIQUE INDEX IF NOT EXISTS edit_recipes_source_post_uq
    ON edit_recipes (source, source_post_id)
 WHERE source_post_id IS NOT NULL
"""


async def _ensure_table() -> bool:
    """Idempotently create edit_recipes + its two partial unique indexes
    (upsert targets). Runs the DDL once per process. Returns True when the
    table is available; False (after logging) on any failure."""
    global _TABLE_READY
    if _TABLE_READY:
        return True
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_DDL)
                await cur.execute(_IDX_GENERATION)
                await cur.execute(_IDX_POST)
        _TABLE_READY = True
        return True
    except Exception:  # noqa: BLE001 — recipe RAG is best-effort everywhere
        log.exception("[edit-recipes] could not ensure edit_recipes table (non-fatal)")
        return False


# ---------------------------------------------------------------------------
# Deterministic serialization of an editor timeline → recipe (no LLM)
# ---------------------------------------------------------------------------

def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _clip_source(clip: dict) -> dict:
    src = clip.get("source")
    return src if isinstance(src, dict) else {}


def _first_kf_value(clip: dict, key: str) -> Optional[float]:
    """First keyframe value for `key` (popup x/y live in keyframes)."""
    kfs = clip.get("keyframes")
    pts = kfs.get(key) if isinstance(kfs, dict) else None
    if isinstance(pts, list) and pts and isinstance(pts[0], dict):
        try:
            return float(pts[0].get("v"))
        except (TypeError, ValueError):
            return None
    return None


def _y_position_bucket(y: Optional[float], height: int) -> str:
    """Center-relative y offset → coarse vertical bucket (renderer convention:
    0 = canvas center, +y down)."""
    if y is None:
        return "center"
    if y < -0.12 * height:
        return "top"
    if y > 0.12 * height:
        return "lower"
    return "center"


def _text_content(clip: dict) -> str:
    tx = clip.get("text")
    if isinstance(tx, dict):
        return str(tx.get("content") or tx.get("text") or tx.get("value") or "").strip()
    return ""


def _timeline_duration_f(timeline: dict, clips: list[dict]) -> int:
    dur = _i(((timeline or {}).get("project") or {}).get("durationInFrames"))
    if dur > 0:
        return dur
    end = 0
    for c in clips:
        end = max(end, _i(c.get("from")) + _i(c.get("durationInFrames")))
    return max(end, FPS)


def _topic_summary(timeline: dict, analysis: dict | None) -> str:
    """What the video is ABOUT: transcript first, scene summaries second,
    the project title last. Capped ~400 chars."""
    sources = (analysis or {}).get("sources") or {}
    spoken: list[str] = []
    summaries: list[str] = []
    for src in sources.values():
        if not isinstance(src, dict):
            continue
        t = ((src.get("transcript") or {}).get("text") or "").strip()
        if t:
            spoken.append(t)
        for sc in src.get("scenes") or []:
            s = str((sc or {}).get("summary") or "").strip()
            if s:
                summaries.append(s)
    if spoken:
        return " ".join(spoken)[:400]
    if summaries:
        return " — ".join(summaries[:4])[:400]
    title = str(((timeline or {}).get("project") or {}).get("title") or "").strip()
    return title[:400]


def _music_energy_mood(analysis: dict | None) -> tuple[Optional[str], Optional[str]]:
    """Dominant scene energy + first mood from the analysis (best-effort)."""
    energy_votes: dict[str, int] = {}
    mood: Optional[str] = None
    for src in ((analysis or {}).get("sources") or {}).values():
        if not isinstance(src, dict):
            continue
        for sc in src.get("scenes") or []:
            en = (sc or {}).get("energy")
            if en in ("low", "medium", "high"):
                energy_votes[en] = energy_votes.get(en, 0) + 1
        if mood is None:
            m = (((src.get("visual") or {}).get("mood")) or src.get("mood") or "").strip()
            if m:
                mood = m[:80]
    energy = max(energy_votes, key=lambda k: energy_votes[k]) if energy_votes else None
    return energy, mood


def serialize_editor_recipe(project_meta: dict, analysis: dict | None) -> dict:
    """DETERMINISTIC recipe from a saved editor timeline (metadata.video_edit,
    shape {project, tracks, clips, ...}) — no LLM, no DB. `project_meta` may
    be the full metadata dict (video_edit key) or the timeline itself."""
    project_meta = project_meta if isinstance(project_meta, dict) else {}
    timeline = project_meta.get("video_edit")
    if not isinstance(timeline, dict):
        timeline = project_meta
    proj = timeline.get("project") or {}
    height = _i(proj.get("height")) or 1920
    clips = [c for c in (timeline.get("clips") or []) if isinstance(c, dict)]

    duration_f = _timeline_duration_f(timeline, clips)
    duration_sec = round(duration_f / FPS, 2)

    # --- video clips (the "scenes"), in timeline order ---------------------
    video_clips = sorted(
        (c for c in clips if (c.get("kind") or "video") == "video"),
        key=lambda c: _i(c.get("from")),
    )
    scene_count = len(video_clips)
    cut_times_sec = [
        round(_i(c.get("from")) / FPS, 2) for c in video_clips[1:]
    ]
    if scene_count:
        avg_sec = sum(_i(c.get("durationInFrames")) for c in video_clips) / scene_count / FPS
    else:
        avg_sec = 0.0
    if avg_sec and avg_sec <= _PACING_FAST_MAX_SEC:
        pacing = "fast"
    elif avg_sec and avg_sec <= _PACING_MEDIUM_MAX_SEC:
        pacing = "medium"
    else:
        pacing = "slow"

    # --- popups: text clips (framed popups contribute their TEXT clip) -----
    popups: list[dict] = []
    for c in sorted(clips, key=lambda c: _i(c.get("from"))):
        if c.get("kind") != "text":
            continue
        text = _text_content(c)
        if not text:
            continue
        popups.append({
            "text": text[:120],
            "start_sec": round(_i(c.get("from")) / FPS, 2),
            "dur_sec": round(_i(c.get("durationInFrames")) / FPS, 2),
            "position": _y_position_bucket(_first_kf_value(c, "y"), height),
        })
    popups = popups[:12]

    # --- transitions (preset + duration live on the LATER clip) ------------
    transitions: list[dict] = []
    for idx, c in enumerate(video_clips):
        trans = c.get("transitionIn") or c.get("transition")
        if not isinstance(trans, dict):
            continue
        preset = trans.get("preset")
        if not preset or preset == "none":
            continue
        transitions.append({
            "after_index": max(0, idx - 1),
            "preset": str(preset),
            "dur_frames": _i(trans.get("durationInFrames")),
        })

    # --- music: audio clips with an audioFile source (the AI music bed sets
    # source.audioFile=True and carries duck keyframes on `volume`) ---------
    music_clips = [
        c for c in clips
        if c.get("kind") == "audio" and (
            _clip_source(c).get("audioFile")
            or isinstance((c.get("keyframes") or {}).get("volume"), list)
        )
    ]
    energy, mood = _music_energy_mood(analysis)
    music = {
        "present": bool(music_clips),
        "energy": energy if music_clips else None,
        "mood": mood if music_clips else None,
    }

    # --- intro / outro bookends (best-effort, no DB): an animation-source
    # clip at the very start/end of the timeline (the bookend pipeline's
    # add_clip places exactly that), else a popup at the open / a CTA popup
    # near the close. ---------------------------------------------------------
    def _is_anim(c: dict) -> bool:
        return _clip_source(c).get("kind") == "animation"

    media_clips = sorted(
        (c for c in clips if (c.get("kind") or "video") in ("video", "image")
         and _clip_source(c)),
        key=lambda c: _i(c.get("from")),
    )
    intro = {"present": False, "style": None, "text": None}
    outro = {"present": False, "cta": None}
    if media_clips and _is_anim(media_clips[0]) and _i(media_clips[0].get("from")) <= FPS // 2:
        intro = {"present": True, "style": "branded_animation", "text": None}
    elif popups and popups[0]["start_sec"] <= 2.5:
        intro = {"present": True, "style": "title_card", "text": popups[0]["text"]}
    if media_clips:
        last = media_clips[-1]
        last_end_f = _i(last.get("from")) + _i(last.get("durationInFrames"))
        if _is_anim(last) and last_end_f >= duration_f - FPS // 2:
            outro = {"present": True, "cta": None}
    if not outro["present"] and popups:
        tail = popups[-1]
        if tail["start_sec"] + tail["dur_sec"] >= duration_sec - 4.0:
            outro = {"present": True, "cta": tail["text"]}

    return {
        "source": "editor",
        "topic_summary": _topic_summary(timeline, analysis),
        "duration_sec": duration_sec,
        "scene_count": scene_count,
        "pacing": pacing,
        "cut_times_sec": cut_times_sec[:40],
        "intro": intro,
        "outro": outro,
        "popups": popups,
        "transitions": transitions[:40],
        "music": music,
        "engagement": None,
    }


# ---------------------------------------------------------------------------
# Embed text — the CONTENT/topic of the edit (retrieval key), not mechanics
# ---------------------------------------------------------------------------

def recipe_embed_text(recipe: dict) -> str:
    """Compact retrieval string: what the video was ABOUT (topic + on-screen
    words) first, light structure hints last. ≤2000 chars."""
    recipe = recipe if isinstance(recipe, dict) else {}
    bits: list[str] = []
    topic = str(recipe.get("topic_summary") or "").strip()
    if topic:
        bits.append(topic)
    intro = recipe.get("intro") or {}
    if intro.get("text"):
        bits.append(str(intro["text"]).strip())
    popup_texts = [
        str(p.get("text") or "").strip()
        for p in (recipe.get("popups") or [])
        if isinstance(p, dict) and p.get("text")
    ]
    if popup_texts:
        bits.append("on-screen: " + " | ".join(popup_texts[:10]))
    outro = recipe.get("outro") or {}
    if outro.get("cta"):
        bits.append("cta: " + str(outro["cta"]).strip())
    hints = [f"pacing {recipe.get('pacing') or 'unknown'}"]
    if intro.get("present"):
        hints.append("has intro")
    if outro.get("present"):
        hints.append("has outro")
    bits.append(", ".join(hints))
    return "  ".join(b for b in bits if b)[:2000]


# ---------------------------------------------------------------------------
# Store (UPSERT) — best-effort, never raises
# ---------------------------------------------------------------------------

async def store_recipe(
    *,
    source: str,
    recipe: dict,
    source_generation_id: int | None = None,
    source_post_id: int | None = None,
    engagement_score: float = 0.0,
) -> None:
    """Embed + UPSERT one recipe. Idempotent per (source, source_generation_id)
    or (source, source_post_id) via the partial unique indexes. BEST-EFFORT:
    any failure logs and returns — storing a recipe must never fail a job."""
    try:
        if not isinstance(recipe, dict) or not recipe:
            return
        if not await _ensure_table():
            return
        embed_text = recipe_embed_text(recipe)
        embedding = None
        if embed_text:
            try:
                vecs = await gemini_embed([embed_text])
                embedding = vecs[0] if vecs and vecs[0] else None
            except Exception:  # noqa: BLE001
                log.exception("[edit-recipes] embed failed — storing recipe without vector")

        if source_generation_id is not None:
            conflict = ("(source, source_generation_id) "
                        "WHERE source_generation_id IS NOT NULL")
        elif source_post_id is not None:
            conflict = "(source, source_post_id) WHERE source_post_id IS NOT NULL"
        else:
            conflict = None

        sql = (
            "INSERT INTO edit_recipes "
            "(source, source_post_id, source_generation_id, recipe, embed_text, "
            " embedding, engagement_score) "
            "VALUES (%s, %s, %s, %s, %s, %s::vector, %s)"
        )
        if conflict:
            sql += (
                f" ON CONFLICT {conflict} DO UPDATE SET"
                " recipe = EXCLUDED.recipe,"
                " embed_text = EXCLUDED.embed_text,"
                " embedding = EXCLUDED.embedding,"
                " engagement_score = EXCLUDED.engagement_score"
            )
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    sql,
                    (
                        str(source), source_post_id, source_generation_id,
                        Jsonb(recipe), embed_text or None, embedding,
                        float(engagement_score or 0.0),
                    ),
                )
        log.info("[edit-recipes] stored recipe (source=%s gen=%s post=%s)",
                 source, source_generation_id, source_post_id)
    except Exception:  # noqa: BLE001 — best-effort by contract
        log.exception("[edit-recipes] store_recipe failed (non-fatal)")


# ---------------------------------------------------------------------------
# Retrieve — pgvector cosine + hybrid engagement re-rank; [] on any failure
# ---------------------------------------------------------------------------

async def nearest_recipes(
    query_text: str,
    *,
    k: int = 3,
    exclude_generation_id: int | None = None,
) -> list[dict]:
    """Top-k past recipes nearest to `query_text`. Over-fetches ~3x by cosine
    similarity then hybrid re-ranks in Python (0.6*sim + 0.4*normalized
    engagement). Best-effort: [] on any failure."""
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
        sql = (
            "SELECT recipe, engagement_score, "
            "       1 - (embedding <=> %s::vector) AS sim "
            "  FROM edit_recipes "
            " WHERE embedding IS NOT NULL"
        )
        params: list[Any] = [qvec]
        if exclude_generation_id is not None:
            sql += " AND source_generation_id IS DISTINCT FROM %s"
            params.append(int(exclude_generation_id))
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params += [qvec, max(k * 3, k)]
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return []
        max_eng = max((float(r.get("engagement_score") or 0.0) for r in rows), default=0.0)
        def _score(r: dict) -> float:
            sim = float(r.get("sim") or 0.0)
            eng = float(r.get("engagement_score") or 0.0)
            norm = (eng / max_eng) if max_eng > 0 else 0.0
            return 0.6 * sim + 0.4 * norm
        rows.sort(key=_score, reverse=True)
        out: list[dict] = []
        for r in rows[:k]:
            rec = r.get("recipe")
            if isinstance(rec, dict) and rec:
                out.append(rec)
        return out
    except Exception:  # noqa: BLE001 — best-effort by contract
        log.exception("[edit-recipes] nearest_recipes failed (non-fatal)")
        return []


# ---------------------------------------------------------------------------
# Prompt block — style/cadence ONLY, framed against topic/timestamp copying
# ---------------------------------------------------------------------------

def _recipe_line(recipe: dict) -> str:
    """One recipe compressed to cadence/ratios (never raw timestamps/topics)."""
    dur = float(recipe.get("duration_sec") or 0.0)
    scenes = int(recipe.get("scene_count") or 0)
    popups = [p for p in (recipe.get("popups") or []) if isinstance(p, dict)]
    transitions = [t for t in (recipe.get("transitions") or []) if isinstance(t, dict)]
    intro = recipe.get("intro") or {}
    outro = recipe.get("outro") or {}
    music = recipe.get("music") or {}

    parts: list[str] = [f"pacing {recipe.get('pacing') or 'unknown'}"]
    if scenes:
        parts.append(f"{scenes} scene(s) over ~{dur:.0f}s")
    if popups and dur > 0:
        every_pct = round(100.0 / (len(popups) + 1))
        avg_p = sum(float(p.get("dur_sec") or 0.0) for p in popups) / len(popups)
        positions = sorted({str(p.get("position") or "center") for p in popups})
        parts.append(
            f"~{len(popups)} popup(s) ≈ one every ~{every_pct}% of runtime, "
            f"~{avg_p:.1f}s each, placed {'/'.join(positions)}"
        )
    else:
        parts.append("no popups")
    if intro.get("present"):
        parts.append(f"intro card: yes — {intro.get('style') or 'card'}")
    else:
        parts.append("intro card: no")
    parts.append("outro CTA: " + ("yes" if outro.get("present") else "no"))
    cuts = max(scenes - 1, 0)
    if transitions and cuts:
        preset_votes: dict[str, int] = {}
        for t in transitions:
            p = str(t.get("preset") or "fade")
            preset_votes[p] = preset_votes.get(p, 0) + 1
        top_preset = max(preset_votes, key=lambda p: preset_votes[p])
        frac = "most" if len(transitions) >= max(1, round(cuts * 0.6)) else "some"
        parts.append(f"transitions: {top_preset} on {frac} cuts")
    else:
        parts.append("transitions: none")
    if music.get("present"):
        m = f"music: yes ({music.get('energy') or 'unknown'} energy"
        if music.get("mood"):
            m += f", {music['mood']}"
        parts.append(m + ")")
    else:
        parts.append("music: no")
    return "; ".join(parts)


def format_recipe_block(recipes: list[dict]) -> str:
    """Compact prompt section for ≤3 past recipes, expressed as ratios/cadence
    with an explicit do-not-copy-the-topic frame. Empty string when there is
    nothing usable."""
    items = [r for r in (recipes or []) if isinstance(r, dict)][:3]
    if not items:
        return ""
    lines = [
        "PAST EDITS THAT WORKED — style reference ONLY. Copy the "
        "CADENCE/STRUCTURE (how many popups, their spacing as a fraction of "
        "runtime, intro card + CTA outro, transition frequency), but NEVER "
        "copy their topic, exact text, or timestamps. Every op you emit must "
        "use the CANDIDATE WINDOWS and clip ids from THIS video."
    ]
    for i, rec in enumerate(items, 1):
        lines.append(f"  {i}. {_recipe_line(rec)}")
    return "\n".join(lines)
