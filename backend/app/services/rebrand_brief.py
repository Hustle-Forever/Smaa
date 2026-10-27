"""Rebrand experiment — evidence gathering, the creative brief, and the
automatic self-critique.

The brief is ONE Gemini call that sees:
  1. the run itself       — topic, hook/dialogue, intro/outro headlines + their
                            animation styles, and four boundary frames (intro
                            end, talking-head start, talking-head end, outro
                            start) so the transitions are designed to CONNECT;
  2. the 42 Instagram RAG — brand DNA aggregated over every deep-analysed post
                            (logo treatment, palette, transition vocabulary,
                            intro/outro card habits) + the posts nearest this
                            topic, weighted by engagement, + a few real frames;
  3. edit recipes         — cadence of past edits that worked (existing RAG);
  4. its OWN history      — past rebrand experiments, their automatic critique
                            scores and the team's star ratings/notes (the loop
                            that feeds on its own results — rebrand_store);
  5. the 42 pattern       — when Omni edits a scene: the randomly picked brand
                            pattern, coloured in the intro's own colours
                            (rebrand_pattern_ref). It is the ONLY reference
                            image Omni receives, so the brief must be written
                            around that exact picture, not a guess at it;
  6. the logo PNG         — for the Veo keyframe only. Omni never gets a logo.

It returns per-model prompts, because Omni (true video edit) and Veo
(first/last-frame generation) need very differently shaped instructions.

Everything here degrades instead of failing: no IG rows, no frames, no past
experiments or a Gemini error all still produce a usable (fallback) brief.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

from ..config import config
from ..db import get_conn
from . import rebrand_store as store
from .vertex_client import gemini_embed, gemini_multimodal

if TYPE_CHECKING:  # only read for its fields; the pipeline builds it
    from .rebrand_pattern_ref import PatternRef

log = logging.getLogger("archivist.rebrand.brief")

# ---------------------------------------------------------------------------
# Instagram brand evidence
# ---------------------------------------------------------------------------

_IG_SQL = """
SELECT ip.id, ip.ig_media_id, ip.caption, ip.media_type,
       COALESCE(ip.like_count, 0)     AS like_count,
       COALESCE(ip.comments_count, 0) AS comments_count,
       COALESCE(ip.view_count, 0)     AS view_count,
       ip.metadata->'video_analysis'->'brand_presence' AS brand_presence,
       ip.metadata->'video_analysis'->'intro_card'     AS intro_card,
       ip.metadata->'video_analysis'->'outro_card'     AS outro_card,
       ip.metadata->'video_analysis'->>'pacing'        AS pacing,
       jsonb_path_query_array(
         COALESCE(ip.metadata->'video_analysis'->'scene_edit_cues', '[]'::jsonb),
         '$[*].transition_in'
       ) AS transitions,
       {dist} AS dist
  FROM instagram_posts ip
 WHERE ip.metadata ? 'video_analysis'
   AND NOT (ip.metadata->'video_analysis' ? '_raw')
 ORDER BY {order}
 LIMIT 400
"""


def _engagement(r: dict) -> float:
    return math.log1p(
        int(r.get("like_count") or 0)
        + 3 * int(r.get("comments_count") or 0)
        + 0.05 * int(r.get("view_count") or 0)
    )


def _clean(s: Any, n: int = 90) -> str:
    return " ".join(str(s or "").split())[:n]


async def _ig_rows(topic_vec: Optional[list[float]]) -> list[dict]:
    if topic_vec:
        sql = _IG_SQL.format(
            dist="(ip.caption_embedding <=> %s::vector)",
            order="(ip.caption_embedding <=> %s::vector) NULLS LAST",
        )
        params: tuple = (topic_vec, topic_vec)
    else:
        sql = _IG_SQL.format(dist="NULL::float", order="ip.like_count DESC NULLS LAST")
        params = ()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]


def _brand_dna(rows: list[dict]) -> dict:
    """Aggregate what the brand ACTUALLY does across every analysed post."""
    logo_seen = 0
    treatments: Counter = Counter()
    palette: Counter = Counter()
    locations: Counter = Counter()
    transitions: Counter = Counter()
    cuts = 0
    intro_styles: Counter = Counter()
    outro_present = 0
    outro_ctas: Counter = Counter()
    for r in rows:
        bp = r.get("brand_presence") or {}
        if isinstance(bp, dict):
            if bp.get("logo_visible"):
                logo_seen += 1
            t = _clean(bp.get("logo_treatment"), 80).lower()
            if t and t not in ("none", "n/a", "no logo"):
                treatments[t] += 1
            for c in bp.get("palette_called_out") or []:
                c = _clean(c, 40).lower()
                if c:
                    palette[c] += 1
            for loc in bp.get("locations") or []:
                loc = _clean(loc, 50).lower()
                if loc:
                    locations[loc] += 1
        for t in r.get("transitions") or []:
            t = _clean(t, 30).lower()
            if not t:
                continue
            cuts += 1
            if t not in ("cut", "none", "hard cut"):
                transitions[t] += 1
        ic = r.get("intro_card") or {}
        if isinstance(ic, dict) and ic.get("present"):
            intro_styles[_clean(ic.get("style"), 40).lower() or "card"] += 1
        oc = r.get("outro_card") or {}
        if isinstance(oc, dict) and oc.get("present"):
            outro_present += 1
            cta = _clean(oc.get("cta"), 70)
            if cta:
                outro_ctas[cta] += 1
    n = len(rows)
    return {
        "posts_analysed": n,
        "logo_visible_share": round(logo_seen / n, 2) if n else 0.0,
        "logo_treatments": treatments.most_common(5),
        "palette": palette.most_common(10),
        "locations": locations.most_common(5),
        "transition_votes": transitions.most_common(6),
        "styled_transition_share": round(sum(transitions.values()) / cuts, 2) if cuts else 0.0,
        "intro_card_styles": intro_styles.most_common(4),
        "outro_card_share": round(outro_present / n, 2) if n else 0.0,
        "outro_ctas": outro_ctas.most_common(3),
    }


def _format_dna(dna: dict) -> str:
    if not dna.get("posts_analysed"):
        return ""

    def _fmt(pairs: list) -> str:
        return ", ".join(f"{k} ×{v}" for k, v in pairs) or "—"

    return (
        f"@42abudhabi BRAND DNA (aggregated over {dna['posts_analysed']} deep-analysed posts — "
        "this is how the brand REALLY looks on its feed):\n"
        f"  • Logo on screen in {int(dna['logo_visible_share'] * 100)}% of posts; treatments: "
        f"{_fmt(dna['logo_treatments'])}\n"
        f"  • Colours called out: {_fmt(dna['palette'])}\n"
        f"  • Settings: {_fmt(dna['locations'])}\n"
        f"  • Styled (non-cut) transitions on {int(dna['styled_transition_share'] * 100)}% of cuts; "
        f"types: {_fmt(dna['transition_votes'])}\n"
        f"  • Intro cards: {_fmt(dna['intro_card_styles'])}; outro/end card in "
        f"{int(dna['outro_card_share'] * 100)}% of posts; CTAs: {_fmt(dna['outro_ctas'])}\n"
    )


def _top_posts(rows: list[dict], k: int = 6) -> list[dict]:
    if not rows:
        return []
    max_eng = max((_engagement(r) for r in rows), default=1.0) or 1.0
    for r in rows:
        d = r.get("dist")
        sim = max(0.0, 1.0 - float(d)) if d is not None else 0.0
        r["_score"] = 0.6 * sim + 0.4 * (_engagement(r) / max_eng)
    return sorted(rows, key=lambda r: r["_score"], reverse=True)[:k]


def _format_top_posts(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = [
        "MOST RELEVANT HIGH-PERFORMING POSTS (topic similarity + engagement) — "
        "copy their BRANDING moves, not their topic:",
    ]
    for r in rows:
        bp = r.get("brand_presence") or {}
        ic = r.get("intro_card") or {}
        oc = r.get("outro_card") or {}
        trans = [t for t in (r.get("transitions") or []) if str(t).lower() not in ("cut", "none", "")]
        lines.append(
            f"  • [♥{r.get('like_count')} 💬{r.get('comments_count')}"
            f"{' ▶' + str(r['view_count']) if r.get('view_count') else ''}] "
            f"\"{_clean(r.get('caption'), 110)}\""
        )
        facts = []
        if isinstance(bp, dict) and bp.get("logo_treatment"):
            facts.append(f"logo: {_clean(bp['logo_treatment'], 70)}")
        if isinstance(bp, dict) and bp.get("palette_called_out"):
            facts.append("palette: " + ", ".join(_clean(c, 20) for c in bp["palette_called_out"][:5]))
        if isinstance(ic, dict) and ic.get("present"):
            facts.append(f"intro card: {_clean(ic.get('style'), 30) or 'yes'}")
        if isinstance(oc, dict) and oc.get("present"):
            facts.append(f"end card: {_clean(oc.get('cta'), 50) or 'yes'}")
        if trans:
            facts.append("transitions: " + ", ".join(_clean(t, 15) for t in trans[:4]))
        if r.get("pacing"):
            facts.append(f"pacing: {_clean(r['pacing'], 20)}")
        if facts:
            lines.append("      " + " | ".join(facts))
    return "\n".join(lines) + "\n"


async def _ig_frames(post_ids: list[int], *, limit: int = 3) -> list[Path]:
    """Up to `limit` real frames from the top posts (first frame of each), when
    the frame extractor has them on this disk. Missing frames are simply
    skipped — the text evidence still carries the brief."""
    if not post_ids:
        return []
    try:
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT post_id, asset_id FROM instagram_post_assets
                     WHERE post_id = ANY(%s) ORDER BY post_id, child_index
                    """,
                    (list(post_ids),),
                )
                rows = await cur.fetchall()
    except Exception:  # noqa: BLE001
        log.exception("[rebrand] IG frame lookup failed (non-fatal)")
        return []
    order = {pid: i for i, pid in enumerate(post_ids)}
    rows = sorted(rows, key=lambda r: order.get(int(r["post_id"]), 999))
    out: list[Path] = []
    seen_posts: set[int] = set()
    for r in rows:
        pid = int(r["post_id"])
        if pid in seen_posts:
            continue
        fdir = config.frames_root / str(int(r["asset_id"]))
        if not fdir.is_dir():
            continue
        frames = sorted(fdir.glob("*.jpg"))
        if not frames:
            continue
        out.append(frames[len(frames) // 3])  # past any black lead-in frame
        seen_posts.add(pid)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def _S(t: str, desc: str = "") -> dict:
    d: dict = {"type": t}
    if desc:
        d["description"] = desc
    return d


def _OBJ(props: dict) -> dict:
    # Every property REQUIRED — Gemini silently drops non-required fields.
    return {"type": "OBJECT", "properties": props, "required": list(props.keys())}


BRIEF_SCHEMA = _OBJ({
    "brand_read": _S("STRING", "2 sentences: what the 42 brand looks like on its feed, from the evidence."),
    "palette": {"type": "ARRAY", "items": _S("STRING"), "description": "3-5 brand colours (names or hex) to use."},
    "background": _OBJ({
        "description": _S("STRING", "The branded background that replaces the talking-head backdrop."),
        "motion": _S("STRING", "Subtle motion in the background (parallax, drifting shapes...)."),
    }),
    "icons": {
        "type": "ARRAY",
        "items": _OBJ({
            "name": _S("STRING"),
            "look": _S("STRING", "Shape/style of the icon — flat, brand colour, no text."),
            "placement": _S("STRING", "Where on screen, away from the face and mouth."),
            "motion": _S("STRING"),
        }),
    },
    "logo": _OBJ({
        "placement": _S("STRING", "Corner/position in the Veo keyframe only (Omni never places a logo), or 'none' if the evidence says the logo is not overlaid on footage."),
        "treatment": _S("STRING"),
    }),
    "transition_in": _OBJ({
        "style": _S("STRING", "Short name, e.g. 'grid wipe', 'logo morph'."),
        "description": _S("STRING", "From the intro's last frame (image 1) into the talking head's first frame (image 2)."),
    }),
    "transition_out": _OBJ({
        "style": _S("STRING"),
        "description": _S("STRING", "From the talking head's last frame (image 3) into the outro's first frame (image 4)."),
    }),
    "scene_transition": _OBJ({
        "style": _S("STRING"),
        "description": _S("STRING", "Between two talking-head scenes (same person, same branded set)."),
    }),
    "omni_scene_prompt": _S("STRING", "Full edit instruction for Omni on ONE talking-head clip. The branded region and the icons' backing are built from the attached 42 brand pattern (when one is attached); no logo."),
    "omni_card_prompt": _S("STRING", "Edit instruction for Omni on the intro/outro CARD clip: keep every letter of its text and the logo exactly; restyle only the background, motion and accents to the brand DNA."),
    "omni_transition_in_prompt": _S("STRING"),
    "omni_transition_out_prompt": _S("STRING"),
    "omni_scene_transition_prompt": _S("STRING"),
    "veo_transition_in_prompt": _S("STRING"),
    "veo_transition_out_prompt": _S("STRING"),
    "veo_scene_transition_prompt": _S("STRING"),
    "keyframe_edit_prompt": _S("STRING", "Image-edit instruction to put the person from the frame onto the branded background (Veo re-render mode)."),
    "veo_background_clause": _S("STRING", "One sentence appended to the original scene prompt describing the new branded set."),
    "lessons_applied": {"type": "ARRAY", "items": _S("STRING"), "description": "Which past lessons this brief acts on (empty if none)."},
    "avoid": {"type": "ARRAY", "items": _S("STRING")},
})

# Told only when the recipe's icons are stamped by the pipeline after Omni
# (options.icons_by_pipeline): the brief must not ask Omni for any.
ICONS_BY_PIPELINE = (
    "ICONS: the pipeline lays the topic icons in itself, after Omni, beside the speaker. "
    "omni_scene_prompt must not ask for, describe or place any icon, symbol, badge or figure: "
    "only the attached pattern is added (the pipeline tells Omni to add nothing else). Leave "
    "the icons list empty."
)

BRIEF_SYSTEM = """You are the creative director of the 42 Abu Dhabi (coding school) Instagram.
You write a REBRAND brief for a FINISHED talking-head reel. Two video models will apply it
side by side as an experiment, so the brief must be concrete and model-ready.

THE ROOM STAYS. These reels are shot inside the real 42 Abu Dhabi campus — the exposed
trusses, the pendant lights, the glass, the wall mark, the people at desks. That room is the
most valuable thing in the frame: it is proof the school exists and that the speaker is
standing in it. Replacing it with a studio backdrop or a neon circuit wall throws that away
and makes the reel look like stock footage. Do not ask for a new background.

Branding is ADDITIVE and PARTIAL. It goes into ONE region of the frame — a strip down the
left or right edge, or an upper corner — as graphics laid OVER the footage. The rest of the
picture is left exactly as shot. Say which region, and say that everything outside it is
untouched.

What may change: graphics ADDED into that one region, and the TRANSITIONS into and out of
the talking head. A small LOGO may be ADDED in the Veo keyframe (keyframe_edit_prompt) ONLY —
Omni gets no logo image, so omni_scene_prompt and the omni transition prompts never mention,
place or ask for one. The one exception is omni_card_prompt: the intro/outro card already
carries the real 42 logo, and that prompt must still tell Omni to keep the card's existing
logo and every letter of its text exactly as they are — keeping a mark that is already there
is not adding one, and without that instruction Omni redraws it.

THE BRAND PATTERN. When a "brand pattern" image is attached (the image list says so), it is
the pattern Omni must reproduce: its lines, already coloured in this reel's intro colour, and
NOTHING else — the rest of that image is truly empty (alpha). If it looks white, grey or like
a checkerboard to you, that is only how emptiness is displayed. Omni itself is shown the
clip's own frame with this pattern already laid into its place, as the target.
The pattern is the one exception to "graphics laid OVER the footage": it is large, sits in its
own fixed place (the image list names it — e.g. the left half of the frame, a band along an
edge, or the whole background), and in the BACKGROUND, BEHIND the speaker — the speaker
stands in front of it and covers it wherever they overlap. The room
still shows between its lines. omni_scene_prompt builds the branded region out of ONLY those
pattern lines, in exactly that colour, behind the speaker, and puts the icons on the
background beside the speaker (never over them). It must say, in so many words: the speaker
covers the pattern, never draw a line over the speaker, and never draw a checkerboard, a
white or grey fill, a solid colour or a panel behind the lines. Call it
"the attached pattern"; do not describe or invent a different texture, and do not recolour
it. The pattern has a fixed place, named in the image list — brand_region.where must be that
place. Do not describe how the pattern enters or moves: the pipeline adds its entrance.
NEVER put a colour code (#...), a number or the word "42" in omni_scene_prompt — Omni paints
them onto the video as text. Name colours in plain words, or not at all.

What must NEVER change: the speaker's face, identity, expression, mouth movement, hands,
body pose, clothing, camera framing, lighting, the room behind them, timing and speech. The
talking-head audio is kept as is.

Ground every choice in the evidence: the brand DNA and posts from the real feed, the brand
requirements, the intro/outro cards of THIS reel (images 1 and 4), and the lessons from past
rebrand experiments. Repeat what scored well; fix what scored badly. Human ratings outrank
automatic critiques.

The transitions are the key move: transition_in must start EXACTLY on image 1 (the intro's
last frame) and land EXACTLY on image 2 (the talking head's first frame); transition_out
starts on image 3 and lands on image 4. Carry the intro card's colours, shapes and motion
language into the reel, then hand over to the outro the same way.

Prompt-writing rules:
- omni_* prompts: imperative EDIT instructions, never descriptions of a new picture. The
  single most common failure is the model re-rendering the whole frame instead of editing it,
  so omni_scene_prompt must open by saying this is an edit of existing footage, name the ONE
  region graphics may enter, and state that every pixel outside that region — the room, the
  lighting, the framing, the person — is returned unchanged. Describing a background at all
  invites a new one; describe only what is ADDED and where.
- veo_* prompts: pure cinematography and motion language (camera move, shapes, light, colour).
  Never use words about identity, faces, likeness, celebrity, real person, deepfake or cloning
  — they trip the video safety filter.
- No generated TEXT or letters anywhere (models misspell). Icons are simple flat shapes.
  The only "text" allowed is the real logo, in keyframe_edit_prompt only, when a logo
  reference image is attached. (A card's own existing text and logo are KEPT, not
  generated — omni_card_prompt protects them, as above.)
- Keep each prompt under 900 characters."""


def _without(text: str, part: str, instead: str = "") -> str:
    """`text` with `part` (which must be in it) replaced by `instead`."""
    if part not in text:
        raise ValueError(f"not in the brief's words: {part[:40]!r}")
    return text.replace(part, instead)


# When our icons are stamped after Omni (options.icons_by_pipeline), the
# brief model is never told to put icons anywhere — not even in its system
# words — or its opening asks Omni for a second set beside ours. The 42
# default keeps BRIEF_SYSTEM and BRIEF_SCHEMA exactly as they are.
BRIEF_SYSTEM_NO_ICONS = _without(_without(
    BRIEF_SYSTEM,
    ", and puts the icons on the\nbackground beside the speaker (never over them)"),
    " Icons are simple flat shapes.")
BRIEF_SCHEMA_NO_ICONS = {**BRIEF_SCHEMA, "properties": {
    **BRIEF_SCHEMA["properties"],
    "icons": {**BRIEF_SCHEMA["properties"]["icons"],
              "description": "Leave empty: the pipeline lays the icons in itself, after Omni."},
    "omni_scene_prompt": _S("STRING", "Full edit instruction for Omni on ONE talking-head clip. The branded "
                                      "region is built from the attached 42 brand pattern (when one is "
                                      "attached) and nothing else is added: no logo, no symbol, no badge."),
}}


def _omni_strip_prompt(
    strip: str, *, pattern: bool, side: str = "right", placement: str = "left",
) -> str:
    """The fallback's Omni scene edit, around what goes in the `side` strip
    (the pattern's own place when there is one — structure is the left)."""
    other = "right" if side == "left" else "left"
    if pattern:
        # The pattern is in its own place, behind the speaker (structure: the
        # whole left half), and the speaker is what it must not touch. Only a
        # side pattern has another half to keep out of.
        keep_out = (f" Nothing enters the {other} half of the frame."
                    if placement in ("left", "right") else "")
        return (
            "This is an EDIT of existing footage, not a new shot. Do NOT replace, restyle or "
            "relight the background: the real room — its ceiling, lights, glass and walls — "
            "stays as shot and shows between the pattern's lines, and the speaker's face, "
            "expression, hands, clothing and the camera framing come back pixel for pixel. "
            f"Lay {strip}. Use exactly the attached pattern in exactly its colours: do not "
            f"invent another texture and do not recolour it.{keep_out} No logo, no text, no "
            "new people."
        )
    fill = (f"Inside that {side}-hand strip only, overlay {strip}, semi-transparent so "
            "the room stays visible through it.")
    return (
        "This is an EDIT of existing footage, not a new shot. Return the video unchanged "
        f"except inside the {side}-hand quarter of the frame. Do NOT replace, restyle or "
        "relight the background: the real room behind the speaker — its ceiling, lights, "
        "glass and walls — must come back pixel for pixel, and so must the speaker's face, "
        f"expression, hands, clothing and the camera framing. {fill} "
        f"Nothing enters the middle or {other} of the frame. No logo, no text, no new people."
    )


def _fallback_brief(req: dict, run_ctx: dict, pattern_ref: Optional[PatternRef] = None, *,
                    icons: bool = True) -> dict:
    """The brief when the brief model cannot answer. `icons` False: our
    icons are stamped after Omni (icons_by_pipeline), so the strip asks Omni
    for none and lists none."""
    colors = [c for c in (req.get("primary_colors") or []) + (req.get("secondary_colors") or []) if c][:5]
    palette = colors or ["deep black", "electric blue", "white"]
    pal = ", ".join(palette)
    # NOT a backdrop. The room the reel was shot in stays; this describes the
    # graphics that are added into one strip of the frame and nothing else.
    accent = palette[1] if len(palette) > 1 else palette[0]
    panel = (f"a narrow vertical strip of flat {pal} brand graphics — a fine measuring grid "
             f"and a column of small pixel beads — down the right edge of the frame")
    bg = panel
    # Omni's strip IS the attached pattern when there is one. Card restyles,
    # the Veo keyframe and the Veo prompts never see that image, so they keep
    # describing the panel in words.
    ref = pattern_ref if (pattern_ref and pattern_ref.path.exists()) else None
    # The pattern owns its place (structure is the left strip); the drawn
    # panel keeps the right.
    placement = ref.layout.placement if ref else "left"
    side = (placement if placement in ("left", "right") else "left") if ref else "right"
    beside = "; the icons on the background beside the speaker" if icons else ""
    strip = (
        f"ONLY the lines of the attached pattern, large, in {ref.where}: in the background, "
        "the speaker in front of it covering it wherever they overlap, never a line over the "
        f"speaker{beside}; between the lines the "
        "video's own background stays visible (the pattern image's empty area is transparent: "
        "never draw a checkerboard, white or grey fill, solid colour or panel)"
    ) if ref else panel
    icon_home = "on the background beside the speaker" if ref else "inside the branded strip"
    return {
        "brand_read": "Fallback brief (the brief model was unavailable): brand colours and a restrained geometric look.",
        "palette": palette,
        "background": {"description": "unchanged — the real 42 campus interior, as shot",
                       "motion": "none; the room is not touched"},
        "brand_region": {"where": (f"{side} edge" if placement in ("left", "right") else ref.where),
                         "description": strip},
        "icons": [
            {"name": "code brackets", "look": f"flat {accent} bracket shape", "placement": f"top-{side} corner, {icon_home}", "motion": "gentle float"},
            {"name": "pixel beads", "look": f"small {palette[-1]} squares in a column", "placement": f"down the {side} edge, {icon_home}", "motion": "slow twinkle"},
        ] if icons else [],
        "logo": {"placement": "small, top corner", "treatment": "white logo, 60% opacity"},
        "transition_in": {"style": "graphic wipe", "description": "the intro card's shapes sweep across and reveal the speaker"},
        "transition_out": {"style": "graphic wipe", "description": "brand-colour shapes sweep in over the speaker and resolve into the outro card"},
        "scene_transition": {"style": "quick grid wipe", "description": "a fast brand-colour grid wipe between scenes"},
        "omni_scene_prompt": _omni_strip_prompt(strip, pattern=ref is not None, side=side,
                                                placement=placement),
        "omni_card_prompt": (
            f"Keep every letter of the text and the logo exactly as they are. Restyle only the "
            f"background and motion: {bg}, gentle brand-colour light sweeps. No new text."
        ),
        "omni_transition_in_prompt": f"Smooth branded graphic wipe in {pal}: the first image's shapes sweep across and reveal the second image. No text.",
        "omni_transition_out_prompt": f"Smooth branded graphic wipe in {pal}: shapes sweep over the first image and resolve into the second image. No text.",
        "omni_scene_transition_prompt": f"A fast {pal} grid wipe from the first image to the second. No text.",
        "veo_transition_in_prompt": f"Dynamic motion-graphics wipe, bold {pal} geometric shapes sweep across frame, smooth camera push, clean studio light, no text.",
        "veo_transition_out_prompt": f"Dynamic motion-graphics wipe, {pal} geometric shapes sweep in and settle into a clean end card composition, no text.",
        "veo_scene_transition_prompt": f"Quick {pal} grid wipe across the frame, crisp motion graphics, no text.",
        "keyframe_edit_prompt": (
            "Keep this photo as it is — the person AND the room behind them, unchanged. "
            f"Overlay {panel}, semi-transparent, so the room reads through it. Nothing is "
            "replaced and nothing enters the middle or left of the frame. No text."
        ),
        "veo_background_clause": (
            "The speaker stays in the same real interior; a narrow column of brand-coloured "
            "graphics sits along the right edge of the frame."
        ),
        "lessons_applied": [],
        "avoid": ["generated text", "icons over the face", "changing the speaker",
                  "replacing the room behind the speaker", "re-rendering the whole frame",
                  "graphics outside the branded strip"],
        "_fallback": True,
        "_topic": run_ctx.get("topic"),
    }


async def build_brief(
    *,
    exp_id: int,
    run_ctx: dict,
    frames: dict[str, Optional[Path]],
    logo: Optional[Path],
    options: dict,
    pattern_ref: Optional[PatternRef] = None,
    extra_patterns: Sequence[PatternRef] = (),
) -> tuple[dict, dict]:
    """Gather evidence and write the brief. Returns (brief, evidence_summary).

    `run_ctx`: {topic, hook, dialogue: [..], intro_headline, intro_subtitle,
    outro_headline, outro_subtitle, intro_style, outro_style, aspect, scenes}.
    `frames`: {intro_last, th_first, th_last, outro_first} → PNG paths (any may
    be None when that bookend is excluded / missing).
    `logo`: the real 42 logo — for the Veo keyframe prompt only.
    `pattern_ref`: the coloured 42 pattern Omni will receive as its ONLY
    reference image (None when Omni edits no scene, or it could not be made).
    The brief model sees the same PNG, so the Omni prompt it writes describes
    the picture Omni actually gets.
    `extra_patterns`: the second and third patterns of a recipe with several
    (each is shown too, and the opening must name every one in its own
    place). Empty — every run but a multi-pattern one — changes nothing."""
    from ..agents.producer.memory import build_brand_requirements
    from .yas_edit_recipes import format_recipe_block, nearest_recipes

    topic = run_ctx.get("topic") or ""
    query = f"{topic}. {run_ctx.get('hook') or ''}".strip()

    # 1) topic vector (shared by the IG query)
    topic_vec: Optional[list[float]] = None
    try:
        vecs = await gemini_embed([query or "42 Abu Dhabi talking head reel"])
        topic_vec = vecs[0] if vecs and vecs[0] else None
    except Exception:  # noqa: BLE001
        log.exception("[rebrand] topic embed failed (non-fatal)")

    # 2) evidence, each piece best-effort
    try:
        req = await build_brand_requirements()
    except Exception:  # noqa: BLE001
        log.exception("[rebrand] brand requirements failed (non-fatal)")
        req = {}
    try:
        ig = await _ig_rows(topic_vec)
    except Exception:  # noqa: BLE001
        log.exception("[rebrand] IG evidence query failed (non-fatal)")
        ig = []
    dna = _brand_dna(ig)
    top = _top_posts(ig)
    ig_frames = await _ig_frames([int(r["id"]) for r in top])
    try:
        recipes = await nearest_recipes(query, k=3)
    except Exception:  # noqa: BLE001
        recipes = []
    lessons = await store.past_lessons(query, exclude_id=exp_id)

    evidence = {
        "ig_posts_analysed": dna.get("posts_analysed", 0),
        "ig_top_posts": [
            {"ig_media_id": r.get("ig_media_id"), "likes": r.get("like_count"),
             "caption": _clean(r.get("caption"), 80)}
            for r in top
        ],
        "ig_frames_used": len(ig_frames),
        "brand_dna": {
            "palette": [k for k, _ in dna.get("palette", [])[:6]],
            "logo_treatments": [k for k, _ in dna.get("logo_treatments", [])[:3]],
            "transitions": [k for k, _ in dna.get("transition_votes", [])[:4]],
        },
        "edit_recipes_used": len(recipes),
        "past_experiments_used": [int(r["id"]) for r in lessons],
        "logo_attached": bool(logo and logo.exists()),
        "pattern_attached": bool(pattern_ref and pattern_ref.path.exists()),
        "boundary_frames": {k: bool(v) for k, v in frames.items()},
    }

    # 3) images, in a fixed order the prompt names
    legend: list[str] = []
    images: list[Path] = []
    for key, label in (
        ("intro_last", "the INTRO card's LAST frame (transition_in starts here)"),
        ("th_first", "the talking head's FIRST frame (transition_in lands here)"),
        ("th_last", "the talking head's LAST frame (transition_out starts here)"),
        ("outro_first", "the OUTRO card's FIRST frame (transition_out lands here)"),
    ):
        p = frames.get(key)
        if p and p.exists():
            images.append(p)
            legend.append(f"Image {len(images)}: {label}")
        else:
            legend.append(f"(no image) {label} — that bookend is not part of this reel")
    pattern_block = ""
    if pattern_ref and pattern_ref.path.exists():
        images.append(pattern_ref.path)
        origin = ("from the intro" if not pattern_ref.fell_back
                  else "(the 42 brand colours — the intro gave no usable colour)")
        legend.append(
            f"Image {len(images)}: the brand pattern ({pattern_ref.slug}) — only its lines are "
            f"content, the rest is truly empty (alpha); coloured {origin}. Omni is shown it "
            f"already laid into {pattern_ref.where} of the clip's own frame"
        )
        # No colour codes here: whatever Gemini is handed it copies into
        # omni_scene_prompt, and Omni then paints a code as on-screen text.
        pattern_block = (
            f"OMNI'S BRAND PATTERN (image {len(images)}): only its lines are content; the rest of "
            "the image is truly empty, even if it looks white, grey or like a checkerboard. "
            "Omni is shown the clip's own frame with this pattern already laid into "
            f"{pattern_ref.where} (its fixed place — brand_region.where must be that). "
            "omni_scene_prompt must say to reproduce that pattern exactly there, large, as lines "
            "in the BACKGROUND BEHIND the speaker (the speaker covers it wherever they overlap; "
            "never a line over the speaker), with the video's own background visible between "
            "them, must explicitly forbid a checkerboard, a white or grey fill, a solid colour "
            "or a panel behind them, and must not mention or place a logo. Call it \"the attached "
            "pattern\". NEVER write a colour code (like #1a2b3c), a number or the word \"42\" "
            "in omni_scene_prompt: Omni paints them onto the video as text.\n"
        )
        extras = [p for p in extra_patterns if p is not None and p.path.exists()]
        for p in extras:
            images.append(p.path)
            legend.append(
                f"Image {len(images)}: another brand pattern ({p.slug}) of the same clip — only "
                f"its lines are content; Omni is shown it already laid into {p.where} of the "
                "clip's own frame, beside the first"
            )
        if extras:
            pattern_block += (
                "SEVERAL PATTERNS: this clip gets every pattern above, each in its own place. "
                "omni_scene_prompt must name every pattern in its own place, keep them separate "
                "and never confine the graphics to one side or half; brand_region.where stays "
                "the first one's.\n"
            )
    if logo and logo.exists():
        images.append(logo)
        legend.append(
            f"Image {len(images)}: the real 42 logo (white PNG) — for the Veo keyframe "
            "(keyframe_edit_prompt) ONLY; Omni never receives it"
        )
    for p in ig_frames:
        images.append(p)
        legend.append(f"Image {len(images)}: a frame from a high-performing @42abudhabi post")

    colors = ", ".join((req.get("primary_colors") or []) + (req.get("secondary_colors") or [])) or "—"
    brand_block = (
        "BRAND REQUIREMENTS (from the brand profile):\n"
        f"  • colours: {colors}\n"
        f"  • fonts: {', '.join(req.get('fonts') or []) or '—'}\n"
        f"  • style pillars: {', '.join(req.get('style_pillars') or []) or '—'}\n"
        f"  • do: {'; '.join((req.get('brand_dos') or [])[:6]) or '—'}\n"
        f"  • don't: {'; '.join((req.get('brand_donts') or [])[:6]) or '—'}\n"
    )
    dialogue = run_ctx.get("dialogue") or []
    run_block = (
        "THIS REEL:\n"
        f"  • topic: {topic}\n"
        f"  • hook: {run_ctx.get('hook') or '—'}\n"
        f"  • scenes: {len(dialogue) or 1}; dialogue: "
        + " / ".join(f'"{_clean(d, 140)}"' for d in dialogue[:5]) + "\n"
        f"  • intro card: \"{run_ctx.get('intro_headline') or ''}\" — {run_ctx.get('intro_subtitle') or ''} "
        f"(animation style: {run_ctx.get('intro_style') or 'unknown'})\n"
        f"  • outro card: \"{run_ctx.get('outro_headline') or ''}\" — {run_ctx.get('outro_subtitle') or ''} "
        f"(animation style: {run_ctx.get('outro_style') or 'unknown'})\n"
        f"  • aspect: {run_ctx.get('aspect')}\n"
    )
    intensity = options.get("intensity") or "balanced"
    target = options.get("target") or "reel"
    target_note = {
        "reel": "",
        "intro": ("TARGET: ONLY THE INTRO CARD is being rebranded now — its restyle "
                  "(omni_card_prompt) and transition_in into the talking head matter most.\n"),
        "outro": ("TARGET: ONLY THE OUTRO CARD is being rebranded now — transition_out from the "
                  "talking head and the card restyle (omni_card_prompt) matter most.\n"),
    }.get(target, "TARGET: ONLY ONE TALKING-HEAD SCENE is being rebranded now — "
                  "omni_scene_prompt, keyframe_edit_prompt and veo_background_clause matter most.\n")
    notes = (options.get("notes") or "").strip()
    if options.get("icons_by_pipeline"):
        # Our icons are stamped after Omni: the brief asks it for none.
        ask = target_note + (
            f"INTENSITY: {intensity} (subtle = quiet graphics, barely moving; "
            "balanced = a clear branded set; bold = a strong graphic set).\n"
            f"{ICONS_BY_PIPELINE}\n"
        )
    else:
        ask = target_note + (
            f"INTENSITY: {intensity} (subtle = backdrop + 1-2 icons, barely moving; "
            "balanced = clear branded set + 2-3 icons; bold = strong graphic set, 3-4 animated icons).\n"
        )
    if notes:
        ask += f"THE TEAM ASKED FOR: {notes[:600]}\n"

    prompt = "\n".join(filter(None, [
        run_block,
        brand_block,
        _format_dna(dna),
        _format_top_posts(top),
        format_recipe_block(recipes),
        store.format_lessons_block(lessons),
        "IMAGES ATTACHED (in this order):\n  " + "\n  ".join(legend),
        pattern_block,
        ask,
        "Write the rebrand brief now.",
    ]))

    # Our icons are stamped after Omni: the brief model's own words never ask
    # for any either (the 42 default's call is exactly as it was).
    ours = bool(options.get("icons_by_pipeline"))
    try:
        brief = await gemini_multimodal(
            prompt,
            images=images,
            system=BRIEF_SYSTEM_NO_ICONS if ours else BRIEF_SYSTEM,
            model=config.vertex_gemini_model,
            json_schema=BRIEF_SCHEMA_NO_ICONS if ours else BRIEF_SCHEMA,
            temperature=0.6,
            max_tokens=16000,  # 2.5-pro thinking tokens share this budget
        )
        if not isinstance(brief, dict) or not brief.get("omni_scene_prompt"):
            raise ValueError("brief missing required prompts")
    except Exception as e:  # noqa: BLE001
        log.exception("[rebrand] brief generation failed — using fallback brief")
        if ours:
            brief = _fallback_brief(req, run_ctx, pattern_ref, icons=False)
        else:
            brief = _fallback_brief(req, run_ctx, pattern_ref)
        evidence["brief_error"] = str(e)[:300]
    return brief, evidence


# ---------------------------------------------------------------------------
# Self-critique (automatic half of the feedback loop)
# ---------------------------------------------------------------------------

CRITIQUE_SCHEMA = _OBJ({
    "score": _S("INTEGER", "Overall 1-10: would the 42 team post this?"),
    "brand_fidelity": _S("INTEGER", "1-10: does it look like the 42 feed / the brief?"),
    "identity_preserved": _S("BOOLEAN", "Same person, face and mouth intact across frames."),
    "transition_quality": _S("INTEGER", "1-10: do intro→reel and reel→outro flow?"),
    "strengths": {"type": "ARRAY", "items": _S("STRING")},
    "issues": {"type": "ARRAY", "items": _S("STRING")},
    "lesson": _S("STRING", "ONE sentence the next rebrand brief should act on."),
})

CRITIQUE_SYSTEM = (
    "You are a strict brand QA reviewer for the 42 Abu Dhabi Instagram. You see frames "
    "sampled in time order from a rebranded talking-head reel plus a reference frame of the "
    "ORIGINAL speaker. Judge honestly; a mediocre result must score mediocre. Check: speaker "
    "unchanged (face, mouth, identity), background/icons match the brief, no garbled text, no "
    "warping or melting artefacts, transitions connect the cards to the reel."
)


async def critique(
    *,
    arm: str,
    brief: dict,
    frames: list[Path],
    original_ref: Optional[Path],
    target: str = "reel",
) -> Optional[dict]:
    """Score one arm's output against its brief. Best-effort (None on failure)."""
    imgs = ([original_ref] if original_ref and original_ref.exists() else []) + frames
    clip_note = {
        "reel": "CLIP: the whole reel (intro card → speaker → outro card).\n",
        "intro": ("CLIP: only the INTRO card + its transition into the speaker — the speaker "
                  "appears only at the very end; judge the card restyle and the hand-over.\n"),
        "outro": ("CLIP: only the transition out of the speaker + the OUTRO card — the speaker "
                  "appears only at the start; judge the hand-over and the card restyle.\n"),
    }.get(target, "CLIP: one talking-head scene on its own (no cards, no transitions).\n")
    if not frames:
        return None
    prompt = (
        clip_note
        + f"ARM: {arm} ({'Gemini Omni video edit' if arm == 'omni' else 'Veo generation'})\n"
        f"BRIEF — background: {(brief.get('background') or {}).get('description')}\n"
        f"icons: {', '.join(i.get('name', '') for i in (brief.get('icons') or []) if isinstance(i, dict))}\n"
        f"transition in: {(brief.get('transition_in') or {}).get('description')}\n"
        f"transition out: {(brief.get('transition_out') or {}).get('description')}\n"
        + ("Image 1 is the ORIGINAL speaker (reference). The rest are output frames in time order.\n"
           if len(imgs) > len(frames) else "All images are output frames in time order.\n")
        + "Review it."
    )
    try:
        out = await gemini_multimodal(
            prompt,
            images=imgs,
            system=CRITIQUE_SYSTEM,
            model=config.vertex_gemini_model,
            json_schema=CRITIQUE_SCHEMA,
            temperature=0.2,
            max_tokens=6000,
        )
        return out if isinstance(out, dict) else None
    except Exception:  # noqa: BLE001
        log.exception("[rebrand] critique of %s failed (non-fatal)", arm)
        return None


def _field(v: Any, key: str) -> str:
    """A brief entry is normally {description|style: ...} but older rows (and
    a hand-edited brief) may carry a plain string — read either."""
    if isinstance(v, dict):
        return str(v.get(key) or "")
    return str(v or "") if isinstance(v, str) else ""


def summarize_for_memory(run_ctx: dict, brief: dict, arms: dict, rating: dict | None) -> tuple[str, str]:
    """(embed_text, lesson) for the experiment's RAG memory row.

    Must hold for every arm mix: a brand-only run has NO brief (the 42 frame is
    a fixed template) and its arm has no auto-review."""
    run_ctx = run_ctx if isinstance(run_ctx, dict) else {}
    brief = brief if isinstance(brief, dict) else {}
    arms = arms if isinstance(arms, dict) else {}
    parts: list[str] = []
    for a in store.ALL_ARMS:
        arm = arms.get(a) or {}
        if not isinstance(arm, dict):
            continue
        rv = arm.get("auto_review") or {}
        if isinstance(rv, dict) and rv.get("lesson"):
            parts.append(f"{a}: {rv['lesson']}")
        elif arm.get("status") == store.ARM_FAILED and arm.get("error"):
            parts.append(f"{a} failed: {str(arm['error'])[:120]}")
        elif a == "brand" and arm.get("status") == store.ARM_READY:
            layouts = sorted({str(s.get("layout")) for s in (arm.get("segments") or []) if isinstance(s, dict)})
            styles = sorted({str(s.get("style")) for s in (arm.get("seams") or []) if isinstance(s, dict)})
            bits = ", ".join(x for x in (
                f"layouts {'/'.join(layouts)}" if layouts else "",
                f"cuts {'/'.join(styles)}" if styles else "",
            ) if x)
            parts.append("brand: 42 frame over the untouched footage" + (f" ({bits})" if bits else ""))
    if isinstance(rating, dict) and rating:
        stars = ", ".join(
            f"{a} {rating[f'{a}_stars']}/5" for a in store.ALL_ARMS if rating.get(f"{a}_stars")
        )
        if stars:
            parts.append(f"team rated {stars}")
        if rating.get("notes"):
            parts.append(f"team notes: {str(rating['notes'])[:240]}")
    lesson = " | ".join(parts)[:900]
    bg = _field(brief.get("background"), "description")
    tin = _field(brief.get("transition_in"), "style")
    tout = _field(brief.get("transition_out"), "style")
    look = (f"Background: {bg}. Transitions: {tin} / {tout}. " if (bg or tin or tout)
            else "Look: the fixed 42 brand frame (violet slabs, beads, grid, logo). ")
    embed_text = (
        f"Rebranded: {run_ctx.get('target') or 'reel'}. "
        f"Topic: {run_ctx.get('topic') or ''}. Hook: {run_ctx.get('hook') or ''}. "
        f"{look}{lesson}"
    )
    return embed_text, lesson
