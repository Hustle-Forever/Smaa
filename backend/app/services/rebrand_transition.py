"""The intro→main seam, end to end: read it, design it, render it, learn.

    resolve   the intro clip and the first talking-head clip of a finished
              TH v2 reel, normalised onto one canvas
    read      the last frame of the intro THAT HAS SOMETHING ON IT and the
              first such frame of the shot — cards fade to black, so the
              literal boundary frames are routinely useless
    recall    past seams that looked like this one, and how they were rated
    plan      a recipe (transition_planner)
    render    the `pattern-transition` composition (Remotion) — real footage,
              brand graphics on top, no generation anywhere
    apply     optionally rebuild the whole reel around the new seam
    remember  store the recipe against the two moments so the next seam
              retrieves it

Nothing here calls a video model. The transition costs a Remotion render and
two small Gemini calls (one to describe each frame, one to plan), and degrades
to a fully deterministic design when Vertex is unreachable.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import HTTPException

from ..config import config
from . import pattern_kit
from . import rebrand_media as media
from . import rebrand_moments as moments
from . import remotion_client
from . import transition_planner as planner
from . import yas_talking_head_v2 as th

log = logging.getLogger("archivist.rebrand.transition")

SEAM = "intro_to_main"
WORK_ROOT = th.TH_ROOT / "transitions"
TEMPLATE = "pattern-transition"

RENDER_POLL_SEC = 2.0
RENDER_TIMEOUT_SEC = 8 * 60
# How much silence-and-black at the end of an intro card is worth cutting. Veo
# bookends habitually end on a fade; keeping it only delays the seam.
DEAD_TAIL_MIN = 0.12


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def _data_url(p: Path) -> str:
    """A path under backend/data/ as the URL Remotion's headless Chrome uses."""
    try:
        rel = Path(p).resolve().relative_to(config.asset_root.parent.resolve())
    except ValueError as e:
        raise RuntimeError(
            f"{p} is outside backend/data/ — Remotion cannot fetch it"
        ) from e
    joined = "/".join(quote(part) for part in rel.parts)
    return f"{config.remotion_base_url.rstrip('/')}/data/{joined}"


def _pattern_assets() -> list[dict]:
    out = []
    root = config.asset_root.parent
    for p in pattern_kit.kit_or_empty():
        out.append({
            "slug": p.slug,
            "mask": _data_url(root / p.mask),
            "art": _data_url(root / p.art) if p.art else None,
            "aspect": p.aspect,
        })
    return out


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class SeamResult:
    run_id: int
    work: Path
    clip: Path                      # the rendered transition on its own
    recipe: dict
    intro_moment: moments.Moment
    main_moment: moments.Moment
    pre_sec: float
    post_sec: float
    dead_tail_sec: float
    recipe_id: Optional[int] = None
    reel: Optional[Path] = None     # the whole reel rebuilt around the seam
    precedents: list[dict] = field(default_factory=list)
    critique: dict = field(default_factory=dict)
    stills: list[Path] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "run_id": self.run_id,
            "recipe_id": self.recipe_id,
            "recipe": self.recipe,
            "clip": str(self.clip),
            "clip_url": _safe_url(self.clip),
            "reel": str(self.reel) if self.reel else None,
            "reel_url": _safe_url(self.reel) if self.reel else None,
            "stills": [_safe_url(s) for s in self.stills],
            "timing": {
                "pre_sec": round(self.pre_sec, 3),
                "post_sec": round(self.post_sec, 3),
                "dead_tail_trimmed_sec": round(self.dead_tail_sec, 3),
            },
            "moments": {
                "intro_out": self.intro_moment.to_public(),
                "main_in": self.main_moment.to_public(),
            },
            "precedents": [
                {"id": r.get("id"), "stars": r.get("stars"),
                 "style": (r.get("recipe") or {}).get("style")
                 if isinstance(r.get("recipe"), dict) else None}
                for r in self.precedents
            ],
            "critique": self.critique,
        }


def _clamp01(v: float) -> float:
    return max(0.0, min(0.999, v))


def _safe_url(p: Optional[Path]) -> Optional[str]:
    if not p:
        return None
    try:
        return _data_url(p)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Resolving the two clips
# ---------------------------------------------------------------------------

async def _resolve(run_id: int, work: Path) -> tuple[Path, Path, str, int, int]:
    """The intro clip and the first talking-head clip, normalised onto one
    canvas so the seam, the slices and the concat all agree on geometry."""
    from . import rebrand_pipeline as pipe   # local: pipeline imports media too

    run = await th._fetch_run_row(run_id)
    md = run["metadata"] or {}
    refs = dict(md.get("refs") or {})
    aspect = th._norm_aspect(md.get("aspect_ratio"))
    width, height = media.dims_for(aspect)
    cache = WORK_ROOT / "_cache"

    intro = await pipe._bookend("intro", run_id=run_id, md=md, refs=refs, cache=cache)
    if not intro:
        raise HTTPException(409, "This reel has no intro — there is no seam to design.")
    scenes = await pipe._scenes(md, cache, strict=False)
    if not scenes:
        raise HTTPException(409, "This reel has no talking-head clip ready yet.")

    base = work / "base"
    loop = asyncio.get_running_loop()
    if intro.is_still:
        intro_norm = await loop.run_in_executor(
            None, lambda: media.still_to_clip(
                intro.source, base / "intro.mp4",
                seconds=th.INTRO_HOLD_SEC, width=width, height=height))
    else:
        intro_norm = await loop.run_in_executor(
            None, lambda: media.normalize(
                intro.source, base / "intro.mp4",
                width=width, height=height, audio="keep"))
    main_norm = await loop.run_in_executor(
        None, lambda: media.normalize(
            scenes[0].source, base / "main.mp4",
            width=width, height=height, audio="keep"))
    return intro_norm, main_norm, aspect, width, height


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

async def _render(props: dict, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    job_id = await remotion_client.submit_render(TEMPLATE, out, props)
    waited = 0.0
    while waited < RENDER_TIMEOUT_SEC:
        await asyncio.sleep(RENDER_POLL_SEC)
        waited += RENDER_POLL_SEC
        status = await remotion_client.get_job_status(job_id)
        state = status.get("status")
        if state == "done":
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("Remotion reported done but wrote no file")
            return out
        if state == "failed":
            raise RuntimeError(f"Remotion render failed: {status.get('error')}")
    raise TimeoutError(f"Remotion render did not finish within {RENDER_TIMEOUT_SEC:.0f}s")


async def preview_stills(props: dict, out_dir: Path, *, at: list[float]) -> list[Path]:
    """A few frames of the transition, rendered synchronously — so the seam can
    be looked at (by a person or by a critic) without waiting for a video."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fps = 30
    total = max(1, round((props["recipe"]["duration_sec"]) * fps))
    stills: list[Path] = []
    for i, frac in enumerate(at):
        dest = out_dir / f"still_{i}.jpg"
        try:
            await remotion_client.render_still(
                TEMPLATE, dest, props, frame=int(max(0, min(total - 1, frac * total))),
            )
            if dest.exists() and dest.stat().st_size > 0:
                stills.append(dest)
        except Exception as e:  # noqa: BLE001 — a missing still is not a failure
            log.warning("[transition] still at %.2f failed: %s", frac, str(e)[:160])
    return stills


# ---------------------------------------------------------------------------
# Critique
# ---------------------------------------------------------------------------

_CRITIQUE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "score": {"type": "INTEGER", "description": "1-10, how well the seam reads."},
        "reads_as_transition": {"type": "BOOLEAN",
                                "description": "Does the graphic carry the cut, or is it a sticker on a hard cut?"},
        "covers_face": {"type": "BOOLEAN", "description": "Does anything sit on the speaker's face or mouth?"},
        "palette_continuous": {"type": "BOOLEAN",
                               "description": "Do the graphics look like they came out of the card?"},
        "problems": {"type": "ARRAY", "items": {"type": "STRING"}},
        "lesson": {"type": "STRING", "description": "One sentence the next seam should act on."},
    },
    "required": ["score", "lesson"],
}

_CRITIQUE_SYSTEM = """You are reviewing frames taken across ONE transition, in order: the end of a
title card, the moment of the cut, and the first beats of the talking-head shot that follows.

Judge only the seam. Say whether the graphics carry the cut or merely sit on top of it, whether
the colour continues out of the card into the shot, and whether anything covers the speaker's
face, mouth or the captions. Be blunt — this feeds the next design, so a polite score is a
wasted one."""


async def critique(stills: list[Path], recipe: dict) -> dict:
    if not stills:
        return {}
    try:
        from .vertex_client import gemini_multimodal

        out = await gemini_multimodal(
            f"The recipe called this '{recipe.get('style')}': {recipe.get('rationale', '')}\n"
            f"These frames are in time order across the transition. Review the seam.",
            images=stills[:6],
            system=_CRITIQUE_SYSTEM,
            json_schema=_CRITIQUE_SCHEMA,
            temperature=0.3,
            max_tokens=900,
        )
        return out if isinstance(out, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("[transition] critique unavailable: %s", str(e)[:160])
        return {}


# ---------------------------------------------------------------------------
# The whole reel, rebuilt around the new seam
# ---------------------------------------------------------------------------

async def _rebuild_reel(
    intro: Path, main: Path, transition: Path, work: Path,
    *, intro_keep_to: float, main_resume_at: Optional[float],
    width: int, height: int,
) -> Optional[Path]:
    """intro up to the seam + the transition + the rest of the shot.

    Lossless apart from the intro's dead black tail, which is dropped on
    purpose. Only the first scene is involved; the remaining scenes and the
    outro are untouched by design — this is the intro→main seam and nothing
    else."""
    loop = asyncio.get_running_loop()
    parts: list[Path] = []
    head = await loop.run_in_executor(
        None, lambda: media.slice_clip(intro, work / "reel_intro.mp4",
                                       start=0.0, end=intro_keep_to))
    if head:
        parts.append(head)
    # The transition goes in as rendered; concat_exact() conforms every piece
    # to the shared canvas as it joins them, so there is no separate re-encode
    # to lose a frame in.
    parts.append(transition)
    if main_resume_at is not None:
        tail = await loop.run_in_executor(
            None, lambda: media.slice_clip(main, work / "reel_main.mp4",
                                           start=main_resume_at))
        if tail:
            parts.append(tail)
    if len(parts) < 2:
        return None
    return await loop.run_in_executor(
        None, lambda: media.concat_exact(parts, work / "seam_preview.mp4",
                                         width=width, height=height))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def design_seam(
    run_id: int,
    *,
    exp_id: Optional[int] = None,
    notes: str = "",
    pre_sec: float = planner.PRE_SEC,
    post_sec: float = planner.POST_SEC,
    cover: str = "shot",
    side: str = "auto",
    hold_sec: float = planner.PANEL_HOLD_SEC,
    recipe_override: Optional[dict] = None,
    build_reel: bool = True,
    do_critique: bool = True,
    debug_safe_zones: bool = False,
) -> SeamResult:
    """Design and render the intro→main seam for a finished TH v2 reel."""
    if not pattern_kit.kit_or_empty():
        raise HTTPException(
            503,
            "The pattern kit has not been prepared. Run "
            "`python -m app.services.pattern_kit <patterns dir>` in backend/.",
        )

    work = WORK_ROOT / str(run_id) / (str(exp_id) if exp_id else "latest")
    work.mkdir(parents=True, exist_ok=True)
    intro, main, aspect, width, height = await _resolve(run_id, work)

    intro_dur = media.probe_duration(intro) or 0.0
    main_dur = media.probe_duration(main) or 0.0

    # --- read both sides of the seam ---------------------------------------
    frames_dir = work / "frames"
    intro_moment, main_moment = await asyncio.gather(
        moments.read_moment(intro, frames_dir, kind="intro_out", ref="intro", edge="last"),
        moments.read_moment(main, frames_dir, kind="main_in", ref="scene_0", edge="first"),
    )

    # The intro's dead tail: everything after its last frame with content. A
    # card that fades to black for half a second is half a second of nothing
    # standing between the card and the shot.
    dead_tail = 0.0
    if not intro_moment.fell_back and intro_dur > 0:
        dead_tail = max(0.0, intro_dur - intro_moment.at_sec - 0.04)
        if dead_tail < DEAD_TAIL_MIN:
            dead_tail = 0.0
    intro_end = max(0.1, intro_dur - dead_tail)
    if dead_tail:
        log.info("[transition] run=%d trimming %.2fs of dead intro tail", run_id, dead_tail)

    pre = max(0.2, min(pre_sec, intro_end - 0.05))
    if cover == "shot":
        # Cover the whole talking head, so the branded panel has somewhere to
        # arrive, hold and then LEAVE. Covering only the join would strand it
        # on screen at the cut of the render.
        post = main_dur if main_dur else post_sec
    else:
        post = max(0.4, min(post_sec, main_dur - 0.05)) if main_dur else post_sec
    duration = pre + post

    # --- recall, then design ------------------------------------------------
    await asyncio.gather(
        moments.store_moment(intro_moment, run_id=run_id),
        moments.store_moment(main_moment, run_id=run_id),
    )
    precedents = await moments.similar_seams(intro_moment, main_moment, exclude_run=run_id)

    if recipe_override:
        recipe = planner.enforce(recipe_override, intro_moment, main_moment,
                                 duration=duration, cut_at=pre, cover=cover,
                                 side=side, hold_sec=hold_sec)
        recipe["planner"] = "override"
    else:
        recipe = await planner.plan(
            intro_moment, main_moment, duration=duration, cut_at=pre,
            precedents=precedents, notes=notes, cover=cover, side=side,
            hold_sec=hold_sec,
        )
    log.info("[transition] run=%d recipe=%r by %s, %d layers",
             run_id, recipe.get("style"), recipe.get("planner"), len(recipe.get("layers") or []))

    # --- render -------------------------------------------------------------
    props = {
        "introUrl": _data_url(intro),
        "mainUrl": _data_url(main),
        "introTrimBeforeSec": round(max(0.0, intro_end - pre), 3),
        "recipe": recipe,
        "patterns": _pattern_assets(),
        "width": width,
        "height": height,
        "debugSafeZones": bool(debug_safe_zones),
    }
    rendered = await _render(props, work / "render.mp4")
    # The renderer's own audio track sits ~45 ms behind its video; rebuild it
    # from the same two clips so the voice lands with the lips.
    loop = asyncio.get_running_loop()
    clip = await loop.run_in_executor(
        None, lambda: media.mux_seam_audio(
            rendered, work / "transition.mp4",
            intro=intro, intro_from=max(0.0, intro_end - pre),
            main=main, cut_at=pre, duration=duration,
        ),
    )

    result = SeamResult(
        run_id=run_id, work=work, clip=clip, recipe=recipe,
        intro_moment=intro_moment, main_moment=main_moment,
        pre_sec=pre, post_sec=post, dead_tail_sec=dead_tail,
        precedents=precedents,
    )

    if build_reel:
        try:
            result.reel = await _rebuild_reel(
                intro, main, clip, work,
                intro_keep_to=max(0.0, intro_end - pre),
                # `shot` mode already rendered the shot to its last frame, so
                # there is no remainder to append.
                main_resume_at=None if cover == "shot" else post,
                width=width, height=height,
            )
        except Exception:  # noqa: BLE001 — the clip itself is the deliverable
            log.exception("[transition] rebuilding the reel failed (non-fatal)")

    if do_critique:
        # Sample where the DECISIONS are: before the cut, through it, with the
        # panel held, and after it has gone.
        marks = (
            [0.12, 0.42, 0.55, 0.78, 0.95] if cover == "seam"
            else [
                _clamp01((pre - 0.3) / duration),
                _clamp01((pre + 0.15) / duration),
                _clamp01((pre + 1.0) / duration),
                _clamp01((pre + planner.PANEL_IN_SEC + hold_sec * 0.5) / duration),
                _clamp01((pre + planner.PANEL_IN_SEC + hold_sec
                          + planner.PANEL_FADE_SEC + 0.8) / duration),
            ]
        )
        result.stills = await preview_stills(props, work / "stills", at=marks)
        result.critique = await critique(result.stills, recipe)

    # --- remember -----------------------------------------------------------
    result.recipe_id = await moments.store_recipe(
        run_id=run_id, exp_id=exp_id, seam=SEAM, recipe=recipe,
        planner=str(recipe.get("planner") or "composed"),
        from_moment=intro_moment.row_id, to_moment=main_moment.row_id,
        render_path=str(clip),
        embed_text=moments.seam_text(intro_moment, main_moment),
    )
    if result.recipe_id and result.critique:
        await moments.rate_recipe(result.recipe_id, critique=result.critique)
    return result


async def rate(recipe_id: int, *, stars: Optional[int], notes: str = "") -> dict:
    """The team's verdict, folded back in so retrieval starts preferring it."""
    if stars is not None and not (1 <= int(stars) <= 5):
        raise HTTPException(400, "stars must be between 1 and 5")
    await moments.rate_recipe(recipe_id, stars=stars, notes=notes or None)
    return {"ok": True, "recipe_id": recipe_id, "stars": stars}
