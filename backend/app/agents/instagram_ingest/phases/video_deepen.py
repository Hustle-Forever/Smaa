"""Phase 4b — video_deepen.

Per Instagram video post that has been through the cheap video phase but not
yet deepened:

  1. Re-detect scene boundaries via PySceneDetect (returns (start, end) ranges)
  2. For each scene:
       - pick (or extract) a representative frame
       - take the transcript text whose timestamps overlap the scene
       - Gemini 2.5 Flash multimodal — 1 frame + transcript excerpt → 2-3
         sentence scene description (subject, action, framing, on-screen text,
         brand elements visible)
       - PLUS (additive, best-effort) frame-by-frame edit cues for the
         edit-recipe RAG: on_screen_text_events (text + approx start ±1-2s +
         position), is_intro_card / is_outro_card, transition_in — aided by an
         optional boundary-straddling second frame just before the cut.
         Stored under metadata.video_analysis.scene_edit_cues; falls back to
         the original prose-only describe if the structured call fails.
       - embed in 768d → instagram_video_scenes
  3. Whole-video structured analysis:
       - 4-6 evenly-spaced frames + full transcript + per-scene descriptions
       - Gemini 2.5 Pro multimodal w/ JSON schema → narrative_arc, hook_type,
         hook_description, pacing, audio_profile, brand_presence, on-screen
         text, CTA, why_this_likely_worked, transferable_pattern
  4. Compose long-form ai_summary (~600-1200 words) from the structured data
  5. UPDATE instagram_posts: ai_summary, metadata.video_deepened_at,
     metadata.video_analysis. NULL caption_embedding so the caption_embed
     phase re-embeds with the new rich summary.

Idempotent: a post is skipped if metadata->>'video_deepened_at' IS NOT NULL.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....services.storage import frames_dir, relativise, resolve_local
from ....services.vertex_client import gemini_embed, gemini_multimodal
from ...website_ingest.phases.video import _save_frame_sync, _probe_duration
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.ig_video_deepen")


# ---------------------------------------------------------------------------
# JSON schema for the whole-video structured analysis
# ---------------------------------------------------------------------------

VIDEO_ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "narrative_arc": {
            "type": "STRING",
            "description": "One-line description of the storytelling shape, e.g. 'hook → 3 student picks → punchline'",
        },
        "hook_type": {
            "type": "STRING",
            "description": "Pick one: verbal_question | visual_reveal | motion | stat | face | text_on_screen | demo | other",
        },
        "hook_description": {
            "type": "STRING",
            "description": "What specifically happens in the first 1-3 seconds.",
        },
        "pacing": {
            "type": "STRING",
            "description": "Pick one: fast-cut | rhythmic-cuts | single-take | long-hold | mixed",
        },
        "scene_count": {"type": "INTEGER"},
        "audio_profile": {
            "type": "OBJECT",
            "properties": {
                "has_speech": {"type": "BOOLEAN"},
                "speech_language": {
                    "type": "STRING",
                    "description": "en | ar | mixed | none",
                },
                "music_present": {"type": "BOOLEAN"},
                "energy": {
                    "type": "STRING",
                    "description": "high | medium | low",
                },
            },
        },
        "brand_presence": {
            "type": "OBJECT",
            "properties": {
                "logo_visible": {"type": "BOOLEAN"},
                "logo_treatment": {"type": "STRING"},
                "palette_called_out": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                },
                "locations": {"type": "ARRAY", "items": {"type": "STRING"}},
                "people_described": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                },
            },
        },
        "on_screen_text": {"type": "ARRAY", "items": {"type": "STRING"}},
        # --- edit-recipe additions (optional, additive) -------------------
        "intro_card": {
            "type": "OBJECT",
            "description": (
                "Whether the reel opens on a dedicated title/intro CARD "
                "(graphic, logo sting or text-only opener — NOT just a person "
                "talking with a caption)."
            ),
            "properties": {
                "present": {"type": "BOOLEAN"},
                "style": {
                    "type": "STRING",
                    "description": "title_card | branded_animation | text_on_footage | none",
                },
                "text": {"type": "STRING"},
            },
        },
        "outro_card": {
            "type": "OBJECT",
            "description": (
                "Whether the reel closes on a dedicated end card (logo / CTA "
                "card) rather than just ending on footage."
            ),
            "properties": {
                "present": {"type": "BOOLEAN"},
                "cta": {"type": "STRING"},
            },
        },
        "cta": {"type": "STRING"},
        "performance_context": {
            "type": "STRING",
            "description": "How this video's engagement compares (will be set by caller; model can leave blank).",
        },
        "why_this_likely_worked": {"type": "STRING"},
        "transferable_pattern": {
            "type": "STRING",
            "description": "One sentence the Producer agent can echo when generating similar ideas.",
        },
    },
    "required": [
        "narrative_arc",
        "hook_type",
        "hook_description",
        "pacing",
        "scene_count",
        "transferable_pattern",
        "why_this_likely_worked",
    ],
}


SCENE_DESC_SYSTEM = (
    "You write scene descriptions for a brand-content archive at 42 Abu Dhabi "
    "(a tuition-free coding school in the UAE). For ONE Instagram-reel scene, "
    "write 2-4 COMPLETE sentences (do not trail off mid-clause) that capture:\n"
    "  1. WHO is on screen — describe the person specifically (e.g. 'young "
    "     man in white kandura and ghutra', 'woman in patterned hijab at a "
    "     workstation', 'bearded man in black suit'). Use their NAME if it "
    "     appears as on-screen text.\n"
    "  2. WHAT they are doing visually (speaking to camera, coding, walking, "
    "     gesturing) and the FRAMING / motion (medium shot, push-in, wide "
    "     pan, cut-away, etc.).\n"
    "  3. WHAT THEY SAY — if the voiceover provided below names them or has "
    "     their voice, paraphrase the key idea in plain English so the "
    "     visual description ties to the audio meaning. If there is no "
    "     voiceover or only ambient sound, say so explicitly ('silent', "
    "     'music only').\n"
    "  4. Brand cues: 42 marks, on-screen Arabic text (translate the gist), "
    "     campus interior signals (atrium, iMacs, workstations), palette.\n"
    "Avoid generic adjectives ('vibrant', 'engaging', 'modern'). Be concrete. "
    "Output prose only — no lists, no headings."
)

# --- Per-scene structured describe: prose description + frame-by-frame edit
# cues for the edit-recipe RAG.
#
# HONEST CAVEAT: the cues are inferred from 1-2 STILL frames per scene, so
#   - popup `approx_start_sec` is only accurate to ±1-2 seconds, and
#   - `transition_in` typing (fade vs swipe vs zoom) from stills is NOISY.
# Downstream these are treated as a CADENCE/style hint, never ground truth.
SCENE_DESC_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "description": {
            "type": "STRING",
            "description": "The 2-4 sentence scene description per the system rules.",
        },
        "on_screen_text_events": {
            "type": "ARRAY",
            "description": (
                "Overlay text / popups visible in this scene's frame(s). "
                "approx_start_sec is an ABSOLUTE timestamp in the video, your "
                "best estimate within the scene's timespan (±1-2s is fine)."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "approx_start_sec": {"type": "NUMBER"},
                    "position": {
                        "type": "STRING",
                        "description": "top | center | lower",
                    },
                },
                "required": ["text"],
            },
        },
        "is_intro_card": {
            "type": "BOOLEAN",
            "description": "True only if this scene IS a dedicated title/intro card (graphic or text-only opener).",
        },
        "is_outro_card": {
            "type": "BOOLEAN",
            "description": "True only if this scene IS a dedicated end/CTA card.",
        },
        "transition_in": {
            "type": "STRING",
            "description": (
                "How this scene is entered from the previous one: "
                "cut | fade | swipe | zoom | none. Use the boundary frame pair "
                "if provided; when unsure, say 'cut'."
            ),
        },
    },
    "required": ["description"],
}

SCENE_DESC_STRUCTURED_SYSTEM = SCENE_DESC_SYSTEM + (
    "\n\nReturn JSON per the provided schema. Put the prose (rules above) in "
    "`description`. ALSO extract edit cues: every overlay/popup text you can "
    "see (with an approximate absolute start time inside the scene's "
    "timespan and a coarse vertical position top/center/lower), whether this "
    "scene is a dedicated intro title card or outro/CTA card, and the "
    "transition style into this scene (cut/fade/swipe/zoom). If two frames "
    "are attached, the FIRST is the last moment of the PREVIOUS scene and "
    "the SECOND is this scene — compare them to judge the transition. These "
    "cues are best-effort estimates; never invent text you cannot read."
)

WHOLE_VIDEO_SYSTEM = (
    "You are analysing an Instagram reel from @42abudhabi (42 Abu Dhabi — a "
    "tuition-free coding school, peer-learning model). Your output trains a "
    "downstream video-generation agent on what works for THIS brand's "
    "audience. Be specific. Reject generic words. Identify the hook, narrative "
    "arc, pacing, brand elements, and — most importantly — the TRANSFERABLE "
    "PATTERN: what structural choice could be replicated for a different topic "
    "and likely succeed again. Also note whether the reel opens on a dedicated "
    "intro/title card and/or closes on an end/CTA card (intro_card / "
    "outro_card fields). Return strictly the JSON shape specified."
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _pending_posts() -> list[dict]:
    """IG posts with at least one completed video asset and not yet deepened.

    DISTINCT ON (ip.id) is required because carousels with multiple video
    children would otherwise return one row per child and we'd process the
    same post twice — second pass would clobber the first's scene rows
    (UNIQUE on post_id, scene_index) and overwrite metadata.video_deepened_at.
    We pick the lowest child_index's asset as the canonical video.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH first_video AS (
                  SELECT DISTINCT ON (ip.id)
                         ip.id          AS post_id,
                         a.id           AS asset_id,
                         a.local_path   AS local_path
                    FROM instagram_posts ip
                    JOIN instagram_post_assets ipa ON ipa.post_id = ip.id
                    JOIN assets a ON a.id = ipa.asset_id
                   WHERE a.asset_type = 'instagram'
                     AND a.analyze_status = 'completed'
                     AND a.local_path IS NOT NULL
                     AND (
                       -- never deepened
                       COALESCE(ip.metadata->>'video_deepened_at', '') = ''
                       -- OR deepened but the analysis fell back to _raw
                       -- (JSON parse failure from old Pro+max_tokens=1500 bug)
                       OR (ip.metadata->'video_analysis' ? '_raw')
                       OR NOT (ip.metadata->'video_analysis' ? 'narrative_arc')
                     )
                   ORDER BY ip.id, ipa.child_index
                )
                SELECT fv.post_id,
                       fv.asset_id,
                       fv.local_path,
                       ip.ig_media_id,
                       ip.caption,
                       ip.like_count,
                       ip.comments_count,
                       ip.view_count
                  FROM first_video fv
                  JOIN instagram_posts ip ON ip.id = fv.post_id
                 ORDER BY ip.timestamp_posted DESC NULLS LAST, ip.id DESC
                """
            )
            return await cur.fetchall()


async def _existing_frames(asset_id: int) -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, frame_index, timestamp_sec, local_path, ai_description
                  FROM video_frames
                 WHERE asset_id = %s
                 ORDER BY frame_index
                """,
                (asset_id,),
            )
            return await cur.fetchall()


async def _transcript_segments(asset_id: int) -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT segment_index, start_sec, end_sec, text
                  FROM video_transcripts
                 WHERE asset_id = %s
                 ORDER BY segment_index
                """,
                (asset_id,),
            )
            return await cur.fetchall()


async def _insert_scene(
    *,
    post_id: int,
    asset_id: int,
    scene_index: int,
    start_sec: float,
    end_sec: float,
    representative_frame_id: int | None,
    description: str,
    transcript_excerpt: str | None,
    embedding: list[float] | None,
) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO instagram_video_scenes
                  (post_id, asset_id, scene_index, start_sec, end_sec,
                   representative_frame_id, description, transcript_excerpt, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (post_id, scene_index) DO UPDATE SET
                  asset_id = EXCLUDED.asset_id,
                  start_sec = EXCLUDED.start_sec,
                  end_sec = EXCLUDED.end_sec,
                  representative_frame_id = EXCLUDED.representative_frame_id,
                  description = EXCLUDED.description,
                  transcript_excerpt = EXCLUDED.transcript_excerpt,
                  embedding = EXCLUDED.embedding
                """,
                (
                    post_id, asset_id, scene_index, start_sec, end_sec,
                    representative_frame_id, description, transcript_excerpt, embedding,
                ),
            )


async def _persist_deepening(
    *, post_id: int, ai_summary: str, video_analysis: dict,
) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE instagram_posts
                   SET ai_summary = %s,
                       caption_embedding = NULL,   -- invalidate so caption_embed re-runs
                       metadata = metadata || %s::jsonb
                 WHERE id = %s
                """,
                (
                    ai_summary,
                    Jsonb({
                        "video_deepened_at": datetime.now(timezone.utc).isoformat(),
                        "video_analysis": video_analysis,
                    }),
                    post_id,
                ),
            )


async def _insert_new_frame(
    *, asset_id: int, frame_index: int, timestamp_sec: float, local_path: str,
) -> int:
    """Insert a freshly-extracted frame into video_frames. Returns row id."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO video_frames
                  (asset_id, timestamp_sec, frame_index, local_path)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (asset_id, frame_index) DO UPDATE
                  SET local_path = EXCLUDED.local_path,
                      timestamp_sec = EXCLUDED.timestamp_sec
                RETURNING id
                """,
                (asset_id, float(timestamp_sec), frame_index, local_path),
            )
            row = await cur.fetchone()
            return int(row["id"])


# ---------------------------------------------------------------------------
# Scene detection (returns ranges, not midpoints — different from the existing
# `_detect_scenes` helper which throws away the ranges).
# ---------------------------------------------------------------------------

def _detect_scene_ranges(video_path: Path) -> list[tuple[float, float]]:
    try:
        from scenedetect import detect, ContentDetector
        scenes = detect(str(video_path), ContentDetector(threshold=27.0))
        ranges = [(s.get_seconds(), e.get_seconds()) for s, e in scenes]
        # Filter zero-duration spurious detections
        return [(s, e) for s, e in ranges if e - s > 0.4]
    except Exception as e:
        log.debug("Scene detect failed: %s", e)
        return []


def _fallback_chunks(duration: float, target_seconds: float = 5.0) -> list[tuple[float, float]]:
    """When PySceneDetect returns nothing (silent / static reels), chunk into
    fixed slices ~5s each. Caps total chunks at 12 to keep cost predictable."""
    if duration <= 0:
        return []
    n = max(1, min(12, int(round(duration / target_seconds))))
    step = duration / n
    return [(i * step, min(duration, (i + 1) * step)) for i in range(n)]


def _pick_or_extract_frame(
    *, video_path: Path, asset_id: int, target_sec: float,
    existing_frames: list[dict], next_frame_index: int,
) -> tuple[Path, int | None, int]:
    """Find an existing frame within ±1.5s of `target_sec`, or extract a new
    one. Returns (path, frame_id_in_db_or_None, next_index_to_use).
    """
    for r in existing_frames:
        if abs(float(r["timestamp_sec"]) - target_sec) <= 1.5 and r["local_path"]:
            local = resolve_local(r["local_path"])
            if local.exists():
                return local, int(r["id"]), next_frame_index

    # Extract a new frame
    fdir = frames_dir(asset_id)
    dest = fdir / f"deepen-{next_frame_index:03d}.jpg"
    _save_frame_sync(video_path, target_sec, dest)
    return dest, None, next_frame_index + 1


def _extract_boundary_frame_sync(
    *, video_path: Path, asset_id: int, boundary_sec: float, scene_index: int,
) -> Path | None:
    """OPTIONAL second frame just BEFORE a scene boundary (the other half of
    the boundary-straddling pair used for transition typing). Best-effort and
    cheap: on any failure return None and the describe call proceeds with the
    single representative frame exactly as before. Not registered in
    video_frames — it is an ephemeral analysis aid, not a catalogued frame."""
    try:
        target = max(0.0, float(boundary_sec) - 0.3)
        fdir = frames_dir(asset_id)
        dest = fdir / f"deepen-bnd-{scene_index:03d}.jpg"
        _save_frame_sync(video_path, target, dest)
        return dest if dest.exists() else None
    except Exception as e:  # noqa: BLE001 — strictly optional extra signal
        log.debug("boundary frame extract failed (scene %d): %s", scene_index, e)
        return None


def _segments_in_range(
    segments: list[dict], start: float, end: float,
) -> str:
    """Transcript segments whose audio meaningfully sits in [start, end].

    The old "any overlap" rule attributed the same VO to 3-4 adjacent scenes
    when a long voiceover spanned multiple short cuts. Tighter rule:
      - the segment's midpoint falls inside the scene, OR
      - at least 50% of the segment overlaps the scene, OR
      - at least 60% of the scene is covered by this single segment.
    """
    parts: list[str] = []
    scene_dur = max(0.05, end - start)
    for seg in segments:
        seg_start = float(seg.get("start_sec") or 0)
        seg_end = float(seg.get("end_sec") or seg_start)
        seg_dur = max(0.05, seg_end - seg_start)
        seg_mid = (seg_start + seg_end) / 2.0

        overlap_start = max(start, seg_start)
        overlap_end = min(end, seg_end)
        overlap = max(0.0, overlap_end - overlap_start)
        if overlap <= 0:
            continue

        seg_pct = overlap / seg_dur     # how much of the segment is in scene
        scene_pct = overlap / scene_dur  # how much of the scene is covered

        keep = (
            (start <= seg_mid <= end)
            or seg_pct >= 0.5
            or scene_pct >= 0.6
        )
        if not keep:
            continue
        text = (seg.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# Composers
# ---------------------------------------------------------------------------

def _fmt_dur(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def _compose_ai_summary(
    *,
    post: dict,
    duration_sec: float,
    scenes: list[dict],   # each: {start, end, description, transcript}
    full_transcript: str,
    video_analysis: dict,
) -> str:
    likes = post.get("like_count") or 0
    comments = post.get("comments_count") or 0
    views = post.get("view_count")
    eng_bits = [f"{likes} likes", f"{comments} comments"]
    if views is not None:
        eng_bits.append(f"{views} views")

    audio = (video_analysis.get("audio_profile") or {})
    brand = (video_analysis.get("brand_presence") or {})

    lines: list[str] = [
        f"[REEL · {_fmt_dur(duration_sec)} · {' / '.join(eng_bits)}]",
    ]
    if post.get("caption"):
        lines.append(f"CAPTION: {post['caption'].strip()}")

    if video_analysis.get("hook_description"):
        lines.append(f"HOOK [{video_analysis.get('hook_type') or '?'}]: {video_analysis['hook_description']}")
    if video_analysis.get("narrative_arc"):
        lines.append(f"NARRATIVE: {video_analysis['narrative_arc']}")
    if video_analysis.get("pacing"):
        lines.append(
            f"PACING: {video_analysis['pacing']} · {video_analysis.get('scene_count') or len(scenes)} scenes"
        )

    aud_bits: list[str] = []
    if audio.get("speech_language"):
        aud_bits.append(f"speech={audio['speech_language']}")
    if audio.get("music_present"):
        aud_bits.append("music")
    if audio.get("energy"):
        aud_bits.append(f"energy={audio['energy']}")
    if aud_bits:
        lines.append("AUDIO: " + ", ".join(aud_bits))

    ost = video_analysis.get("on_screen_text") or []
    if ost:
        lines.append("ON-SCREEN TEXT: " + " | ".join(ost[:6]))

    brand_bits: list[str] = []
    if brand.get("logo_visible"):
        brand_bits.append(f"logo: {brand.get('logo_treatment') or 'visible'}")
    if brand.get("palette_called_out"):
        brand_bits.append("palette: " + ", ".join(brand["palette_called_out"][:4]))
    if brand.get("locations"):
        brand_bits.append("locations: " + ", ".join(brand["locations"][:3]))
    if brand.get("people_described"):
        brand_bits.append("people: " + " | ".join(brand["people_described"][:3]))
    if brand_bits:
        lines.append("BRAND: " + " · ".join(brand_bits))

    if video_analysis.get("cta"):
        lines.append(f"CTA: {video_analysis['cta']}")

    if scenes:
        lines.append("SCENE-BY-SCENE:")
        for i, sc in enumerate(scenes):
            lines.append(
                f"  {_fmt_dur(sc['start'])}–{_fmt_dur(sc['end'])} — {sc['description']}"
            )

    if full_transcript:
        lines.append("TRANSCRIPT:")
        lines.append(f"  {full_transcript.strip()}")

    if video_analysis.get("why_this_likely_worked"):
        lines.append(f"WHY THIS LIKELY WORKED: {video_analysis['why_this_likely_worked']}")
    if video_analysis.get("transferable_pattern"):
        lines.append(f"TRANSFERABLE PATTERN: {video_analysis['transferable_pattern']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-video processing
# ---------------------------------------------------------------------------

def _normalise_scene_cues(
    raw: dict, *, start: float, end: float, scene_index: int,
) -> dict:
    """Sanitise the model's edit cues. Event times are clamped into the scene
    span; remember these are ±1-2s APPROXIMATE (cadence hint, not ground
    truth). transition_in for scene 0 is forced to 'none' (nothing precedes it).
    """
    events: list[dict] = []
    for ev in (raw.get("on_screen_text_events") or [])[:6]:
        if not isinstance(ev, dict):
            continue
        text = str(ev.get("text") or "").strip()
        if not text:
            continue
        try:
            t = float(ev.get("approx_start_sec"))
        except (TypeError, ValueError):
            t = start
        pos = str(ev.get("position") or "center").strip().lower()
        if pos not in ("top", "center", "lower"):
            pos = "center"
        events.append({
            "text": text[:120],
            "approx_start_sec": round(min(max(t, start), end), 2),
            "position": pos,
        })
    trans = str(raw.get("transition_in") or "").strip().lower()
    if trans not in ("cut", "fade", "swipe", "zoom", "none"):
        trans = "cut"
    if scene_index == 0:
        trans = "none"
    return {
        "scene_index": scene_index,
        "start_sec": round(start, 2),
        "end_sec": round(end, 2),
        "on_screen_text_events": events,
        "is_intro_card": bool(raw.get("is_intro_card")),
        "is_outro_card": bool(raw.get("is_outro_card")),
        "transition_in": trans,
    }


async def _describe_scene(
    *,
    frame_path: Path,
    transcript_excerpt: str,
    start: float,
    end: float,
    scene_index: int = 0,
    boundary_frame: Path | None = None,
) -> tuple[str, dict | None]:
    """Describe one scene. Returns (prose_description, edit_cues|None).

    Tries the structured call first (description + frame-by-frame edit cues
    for the edit-recipe RAG); falls back to the original prose-only call so
    the existing deepen behaviour can never regress. `boundary_frame` is an
    OPTIONAL still from just before this scene's start (boundary-straddling
    pair → better transition typing; still noisy from stills alone).
    """
    vo_block = (
        f"\"{transcript_excerpt}\""
        if transcript_excerpt
        else "(no voiceover in this scene — silent, ambient, or music only)"
    )
    base_prompt = (
        f"Scene timespan: {_fmt_dur(start)}–{_fmt_dur(end)} "
        f"({end - start:.1f}s long).\n\n"
        f"Voiceover heard during this exact scene:\n  {vo_block}\n\n"
    )
    images = [frame_path]
    frames_note = "the attached frame is its representative still"
    if boundary_frame is not None:
        images = [boundary_frame, frame_path]
        frames_note = (
            "TWO frames attached: the FIRST is the final moment of the "
            "PREVIOUS scene (just before the boundary), the SECOND is this "
            "scene's representative still"
        )
    structured_prompt = base_prompt + (
        f"Describe THIS scene ({frames_note}). Tie the person on screen to "
        f"what is being said. 2-4 complete sentences in `description`, plus "
        f"the edit cues per the schema (absolute times within the timespan "
        f"above; estimates are fine)."
    )
    # max_tokens=1400: the original prose needed 900; the JSON wrapper + cue
    # fields add a little. Flash burns no thinking tokens (true output budget).
    try:
        result = await gemini_multimodal(
            structured_prompt,
            images=images,
            system=SCENE_DESC_STRUCTURED_SYSTEM,
            model="gemini-2.5-flash",
            json_schema=SCENE_DESC_SCHEMA,
            temperature=0.3,
            max_tokens=1400,
        )
        if isinstance(result, dict) and "_raw" not in result:
            desc = str(result.get("description") or "").strip()
            if desc:
                cues = _normalise_scene_cues(
                    result, start=start, end=end, scene_index=scene_index,
                )
                return desc, cues
        log.warning(
            "structured scene describe did not parse (scene %d) — "
            "falling back to prose-only", scene_index,
        )
    except Exception:
        log.exception(
            "structured scene describe failed (scene %d) — "
            "falling back to prose-only", scene_index,
        )

    # --- fallback: the original prose-only call (pre-edit-cues behaviour) ---
    prose_prompt = base_prompt + (
        f"Describe THIS scene (the attached frame is its representative "
        f"still). Tie the person on screen to what is being said. "
        f"Remember: 2-4 complete sentences, no trailing fragments."
    )
    try:
        desc = await gemini_multimodal(
            prose_prompt,
            images=[frame_path],
            system=SCENE_DESC_SYSTEM,
            model="gemini-2.5-flash",
            temperature=0.3,
            max_tokens=900,
        )
        return (desc or "").strip(), None
    except Exception:
        log.exception("Scene describe failed at %s", frame_path)
        return "", None


async def _analyse_whole_video(
    *,
    representative_frames: list[Path],
    full_transcript: str,
    scene_descriptions: list[str],
    duration_sec: float,
) -> dict:
    scene_block = ""
    if scene_descriptions:
        scene_block = "Scene-by-scene descriptions (already produced):\n" + "\n".join(
            f"  {i + 1}. {d}" for i, d in enumerate(scene_descriptions)
        )
    prompt = (
        f"Reel duration: {_fmt_dur(duration_sec)}.\n"
        f"Full Whisper transcript (may be empty):\n\"\"\"\n{full_transcript or '(no speech)'}\n\"\"\"\n\n"
        f"{scene_block}\n\n"
        f"You are also shown {len(representative_frames)} sampled frames in order. "
        f"Return the structured analysis exactly per the schema."
    )
    # Use 2.5 Flash, NOT Pro: Pro burns invisible thinking tokens that count
    # against the output budget, causing the JSON to truncate mid-string and
    # fail to parse. Flash is plenty capable for this structured-extraction
    # task and produces clean JSON deterministically. max_tokens=4000 leaves
    # generous headroom for the deepest analysis we'd ever expect.
    from ....config import config as _config
    try:
        result = await gemini_multimodal(
            prompt,
            images=representative_frames,
            system=WHOLE_VIDEO_SYSTEM,
            model=_config.vertex_gemini_fast_model,
            json_schema=VIDEO_ANALYSIS_SCHEMA,
            temperature=0.3,
            max_tokens=4000,
        )
        if isinstance(result, dict) and "_raw" not in result:
            return result
        # _raw means JSON parse failed downstream; surface it for debugging
        # but return empty so the composed summary doesn't carry broken data.
        raw = result.get("_raw") if isinstance(result, dict) else str(result)
        log.warning(
            "video_analysis JSON did not parse cleanly; raw len=%d head=%r",
            len(raw or ""), (raw or "")[:240],
        )
        return {}
    except Exception:
        log.exception("Whole-video Gemini call failed")
        return {}


async def _process_post(ctx: PhaseContext, post: dict) -> str:
    """Returns 'completed' | 'skipped' | 'failed'."""
    post_id = int(post["post_id"])
    asset_id = int(post["asset_id"])
    video_path = resolve_local(post["local_path"])
    if not video_path.exists():
        log.warning("post #%d: local video missing at %s", post_id, video_path)
        return "failed"

    duration = await asyncio.to_thread(_probe_duration, video_path) or 0.0
    if duration <= 0.5:
        log.warning("post #%d: zero/unknown duration, skipping deepen", post_id)
        return "skipped"

    # 1. Scene ranges
    scene_ranges = await asyncio.to_thread(_detect_scene_ranges, video_path)
    if not scene_ranges:
        scene_ranges = _fallback_chunks(duration)
    if not scene_ranges:
        log.warning("post #%d: no scenes detected and no fallback", post_id)
        return "failed"

    log.info(
        "[run=%s] deepen post #%d: %.1fs, %d scenes",
        ctx.run_id, post_id, duration, len(scene_ranges),
    )

    # 2. Existing frames + transcript
    existing_frames = await _existing_frames(asset_id)
    segments = await _transcript_segments(asset_id)
    full_transcript = " ".join((s.get("text") or "").strip() for s in segments).strip()

    # Track next available frame_index when we extract new frames
    next_idx = max([int(r["frame_index"]) for r in existing_frames], default=-1) + 1
    if next_idx < 100:
        next_idx = 100  # avoid colliding with existing 0..99 frame indices

    # 3. Per-scene: pick frame, build transcript excerpt, describe (parallel)
    scene_payloads: list[dict] = []
    scene_frame_meta: list[dict] = []  # {path, db_frame_id or None, idx, scene_i, transcript_excerpt}
    for scene_i, (start, end) in enumerate(scene_ranges):
        mid = (start + end) / 2.0
        try:
            frame_path, frame_db_id, next_idx = await asyncio.to_thread(
                _pick_or_extract_frame,
                video_path=video_path,
                asset_id=asset_id,
                target_sec=mid,
                existing_frames=existing_frames,
                next_frame_index=next_idx,
            )
        except Exception:
            log.exception("frame pick/extract failed for scene %d", scene_i)
            continue
        # If we just extracted (no db id yet), persist it
        if frame_db_id is None:
            try:
                frame_db_id = await _insert_new_frame(
                    asset_id=asset_id,
                    frame_index=next_idx - 1,  # the index we just consumed
                    timestamp_sec=mid,
                    local_path=relativise(frame_path),
                )
            except Exception:
                log.exception("frame DB insert failed for scene %d", scene_i)

        # OPTIONAL boundary-straddling pair for transition typing: a still
        # just before this scene's start (scene 0 has no incoming boundary).
        # Best-effort — None simply means single-frame describe as before.
        boundary_frame: Path | None = None
        if scene_i > 0:
            boundary_frame = await asyncio.to_thread(
                _extract_boundary_frame_sync,
                video_path=video_path,
                asset_id=asset_id,
                boundary_sec=start,
                scene_index=scene_i,
            )

        scene_frame_meta.append({
            "scene_index": scene_i,
            "start": start,
            "end": end,
            "path": frame_path,
            "boundary_frame": boundary_frame,
            "frame_db_id": frame_db_id,
            "transcript_excerpt": _segments_in_range(segments, start, end),
        })

    # Describe scenes in parallel (Flash, fast). Concurrency=2 — we saw 429s
    # at 3 when a deepen run is back-to-back with the whole-video Pro call.
    sem = asyncio.Semaphore(2)

    async def _describe(meta: dict) -> tuple[str, dict | None]:
        async with sem:
            if ctx.is_cancelled and ctx.is_cancelled():
                raise CancellationError("video_deepen cancelled mid-scene")
            return await _describe_scene(
                frame_path=meta["path"],
                transcript_excerpt=meta["transcript_excerpt"],
                start=meta["start"],
                end=meta["end"],
                scene_index=meta["scene_index"],
                boundary_frame=meta.get("boundary_frame"),
            )

    describe_results = await asyncio.gather(
        *(_describe(m) for m in scene_frame_meta),
        return_exceptions=False,
    )
    descriptions = [d for d, _cues in describe_results]
    # Frame-by-frame edit cues (popup timing ±1-2s, transition typing from
    # stills — NOISY; consumed as a cadence hint by the edit-recipe RAG).
    scene_edit_cues = [cues for _d, cues in describe_results if cues is not None]

    # Embed descriptions in one batched call
    non_empty = [d for d in descriptions if (d or "").strip()]
    embed_map: dict[int, list[float] | None] = {}
    if non_empty:
        try:
            vecs = await gemini_embed(non_empty)
            it = iter(vecs)
            for i, d in enumerate(descriptions):
                if d and d.strip():
                    embed_map[i] = next(it)
        except Exception:
            log.exception("scene embed batch failed for post #%d", post_id)

    # Persist scene rows
    for i, (meta, desc) in enumerate(zip(scene_frame_meta, descriptions)):
        if not (desc or "").strip():
            continue
        try:
            await _insert_scene(
                post_id=post_id,
                asset_id=asset_id,
                scene_index=meta["scene_index"],
                start_sec=meta["start"],
                end_sec=meta["end"],
                representative_frame_id=meta["frame_db_id"],
                description=desc.strip(),
                transcript_excerpt=meta["transcript_excerpt"] or None,
                embedding=embed_map.get(i),
            )
            scene_payloads.append({
                "start": meta["start"],
                "end": meta["end"],
                "description": desc.strip(),
                "transcript": meta["transcript_excerpt"],
            })
        except Exception:
            log.exception("scene insert failed for post #%d scene %d", post_id, i)

    # 4. Whole-video analysis — pick up to 6 evenly-spaced frames
    rep_paths = _pick_representative_frames(
        [m["path"] for m in scene_frame_meta], max_frames=6,
    )
    if not rep_paths:
        log.warning("post #%d: no frames available for whole-video analysis", post_id)
        video_analysis = {}
    else:
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("video_deepen cancelled before whole-video analysis")
        video_analysis = await _analyse_whole_video(
            representative_frames=rep_paths,
            full_transcript=full_transcript,
            scene_descriptions=[s["description"] for s in scene_payloads],
            duration_sec=duration,
        )

    # 4b. Attach the per-scene edit cues to the analysis blob (additive —
    # readers that don't know the key ignore it). The recipe_extract phase
    # turns these + the scene rows into an edit recipe. Timing is APPROXIMATE.
    if scene_edit_cues:
        video_analysis = {
            **(video_analysis if isinstance(video_analysis, dict) else {}),
            "scene_edit_cues": scene_edit_cues,
        }

    # 5. Compose long-form summary + persist
    composed = _compose_ai_summary(
        post=post,
        duration_sec=duration,
        scenes=scene_payloads,
        full_transcript=full_transcript,
        video_analysis=video_analysis,
    )
    await _persist_deepening(
        post_id=post_id,
        ai_summary=composed,
        video_analysis=video_analysis,
    )
    log.info(
        "[run=%s] ✓ post #%d deepened — %d scenes, summary=%d chars, analysis_keys=%s",
        ctx.run_id, post_id, len(scene_payloads), len(composed),
        sorted(list((video_analysis or {}).keys())),
    )
    return "completed"


def _pick_representative_frames(
    paths: list[Path], *, max_frames: int = 6,
) -> list[Path]:
    """Pick up to max_frames evenly-spaced frames from a list (including first
    and last). Dedupes paths."""
    seen: list[Path] = []
    for p in paths:
        if p not in seen and p.exists():
            seen.append(p)
    if len(seen) <= max_frames:
        return seen
    step = (len(seen) - 1) / (max_frames - 1)
    indices = sorted({int(round(i * step)) for i in range(max_frames)})
    return [seen[i] for i in indices if 0 <= i < len(seen)]


# ---------------------------------------------------------------------------
# Phase entry point
# ---------------------------------------------------------------------------

async def run(ctx: PhaseContext) -> PhaseResult:
    pending = await _pending_posts()
    if not pending:
        log.info("[run=%s] video_deepen: nothing pending", ctx.run_id)
        return PhaseResult(name="video_deepen", total=0)

    log.info("[run=%s] video_deepen: %d posts to deepen", ctx.run_id, len(pending))

    processed = 0
    failed = 0
    skipped = 0

    for i, post in enumerate(pending, start=1):
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("video_deepen cancelled mid-batch")
        try:
            outcome = await _process_post(ctx, post)
            if outcome == "completed":
                processed += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
        except CancellationError:
            raise
        except Exception:
            failed += 1
            log.exception("video_deepen failed for post #%s", post.get("post_id"))
        if ctx.report_progress:
            ctx.report_progress(
                "video_deepen", processed + failed + skipped, len(pending),
            )

    log.info(
        "[run=%s] video_deepen done: %d ok, %d failed, %d skipped",
        ctx.run_id, processed, failed, skipped,
    )
    return PhaseResult(
        name="video_deepen",
        processed=processed, failed=failed, skipped=skipped,
        total=len(pending),
    )
