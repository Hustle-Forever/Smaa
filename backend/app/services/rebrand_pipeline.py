"""Rebrand experiment — orchestration (Gemini Omni, plus the 42 brand frame).

One click on "Rebrand" under a finished Talking Head v2 reel:

  1. READ THE REEL   resolve intro / scenes / outro exactly as the TH v2 stitch
                     does (excluded clips stay out), pull four boundary frames,
                     normalise every source segment once.
  2. BRIEF           rebrand_brief.build_brief — IG brand DNA + edit recipes +
                     past rebrand lessons + the intro/outro cards → the Omni
                     prompts.
  3. THE OMNI ARM
       omni  Gemini Omni EDITS each ≤10 s talking-head clip (branded background
             + icons, speaker kept), the ORIGINAL audio is re-muxed so lip-sync
             holds, then Omni interpolates intro→reel and reel→outro from the
             exact boundary frames. Its one reference image is NOT the logo:
             it is one of the five 42 patterns, picked at random per
             experiment and coloured in the intro's own colours
             (rebrand_pattern_ref), so the strip the icons sit on is real
             brand art that matches the card it grows out of.
     It time-compresses its generated transitions to a snappy length and lays
     a synthetic whoosh under them, then concats intro + in + scenes + out +
     outro. (A Veo arm stood beside it until 2026-09-25, when the owner retired
     it; old experiments still show its results — rebrand_store.LEGACY_ARMS.)
  4. CRITIQUE + MEMORY  each finished arm is scored by Gemini against the brief;
                     the score, its one-line lesson and (later) the team's star
                     rating are embedded so the NEXT brief learns from them.

A studio recipe MADE IN HOUSE (engine "house" — every new studio version
since 2026-09-29) regenerates nothing either: its scene is made by
rebrand_harness.house (our own graphics laid over the untouched footage, one
encode), on the "omni" arm's key so every reader of a version reads it as
ever — no brief, no Omni, no critique, no memory, no Omni provider needed,
the local matte only: $0. An intro or outro made in house (target "intro" |
"outro": the Rebrand stage's bookends) is made the same way, over the
studio's own clip of it (the reel's source, on the canvas as the reel shows
it); nothing else of the reel is read. A run made in house that ever
reached Omni would stop with an error (_never_omni) rather than pay.

OMNI IS SWITCHED OFF for new work (the owner, 2026-09-30: "no need Omni";
rebrand_harness.recipe.omni_on, REBRAND_OMNI=on brings it back): start()
refuses every Omni run — the whole-reel arm, the intro and outro cards, a
scene from the classic panel, a studio recipe for Omni — with a plain 400
saying what to use instead (omni_off_message). The brand frame and every
scene made in house run as ever. The Omni code below is kept, not deleted:
the versions it made still list, play, rate and download, a studio pass is
added on top of their video for $0, and tests/test_rebrand_golden.py still
locks it call for call. Rating one then embeds nothing (the memory only
ever fed Omni's brief).

A second arm, "brand" (whole reel only), regenerates NOTHING: Remotion draws
42's slabs, beads, grid and logo on a transparent canvas and ffmpeg lays them
over the untouched RAW clips in one pass (services/brand_overlay.py). It is
deterministic, so it needs no brief, no critique and no Vertex — a brand-only
run skips all three.

Nothing here touches the TH v2 run row or its files — every artefact lives
under TH_ROOT/rebrand/<experiment id>/ and the state in yas_rebrand_experiments.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from ..db import get_conn
from . import asset_storage
from . import brand_overlay
from . import omni_client as omni
from . import pattern_library
from . import pattern_planner
from . import person_matte
from . import speaker_front
from . import rebrand_brief
from . import rebrand_media as media
from . import rebrand_pattern_ref as pattern_ref
from . import rebrand_store as store
from . import th_reel
from . import yas_talking_head_v2 as th
from .rebrand_harness import base as hbase
from .rebrand_harness import guards as hguards
from .rebrand_harness import house as hhouse
from .rebrand_harness import prompt as hprompt
from .rebrand_harness import recipe as hrecipe
from .rebrand_harness import runner as hrunner
from .rebrand_harness import stages as hstages
from .rebrand_harness.elements import pattern as pattern_el

log = logging.getLogger("archivist.rebrand.pipeline")

REBRAND_ROOT = th.TH_ROOT / "rebrand"
GEN_TRANSITION_SEC = 4          # what we ask Omni for, then time-compress
INTRO_STILL_SEC = th.INTRO_HOLD_SEC
OUTRO_STILL_SEC = th.OUTRO_HOLD_SEC
# Brand arm output height class → canvas (portrait; landscape swaps them).
BRAND_RESOLUTIONS = {"1080": (1080, 1920), "720": (720, 1280)}
# Auto text from the script (see _brand_text). The heading is used whole or
# not at all; this is only the typed heading's own cap. The sub-line is cut
# at a word past 80 characters: Jost at the frame's 60% floor holds 85+
# characters of real hooks on three lines in a TH reel's bottom bar (the
# longest recent hook is 70), measured with the composition's glyph widths.
BRAND_HEADING_AUTO_MAX = 40
BRAND_SUB_AUTO_MAX = 80

# exp_id → running task (this process only).
_TASKS: dict[int, asyncio.Task] = {}


def live_ids() -> set[int]:
    return {i for i, t in _TASKS.items() if not t.done()}


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def normalize_options(raw: dict | None) -> dict:
    raw = raw or {}

    def _b(key: str, default: bool) -> bool:
        v = raw.get(key)
        return default if v is None else bool(v)

    intensity = str(raw.get("intensity") or "balanced").lower()
    if intensity not in ("subtle", "balanced", "bold"):
        intensity = "balanced"
    try:
        tsec = float(raw.get("transition_sec") or 1.6)
    except (TypeError, ValueError):
        tsec = 1.6
    target = str(raw.get("target") or "reel").strip().lower()
    if not (target in ("reel", "intro", "outro") or re.fullmatch(r"scene_\d{1,2}", target)):
        target = "reel"

    def _text(key: str, limit: int) -> Optional[str]:
        # None = "auto from the script"; "" (or blank) = no text at all.
        v = raw.get(key)
        if v is None:
            return None
        return " ".join(str(v).split())[:limit].strip()

    brand_style = str(raw.get("brand_transitions") or "auto").strip().lower()
    if brand_style not in brand_overlay.STYLE_OPTIONS:
        brand_style = "auto"
    resolution = str(raw.get("brand_resolution") or "1080").strip().lower().rstrip("p")
    if resolution not in BRAND_RESOLUTIONS:
        resolution = "1080"
    pattern = str(raw.get("pattern") or pattern_library.DEFAULT_SLUG).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", pattern):
        pattern = pattern_library.DEFAULT_SLUG
    return {
        "target": target,
        "run_omni": _b("run_omni", True),
        # Veo is retired from Rebrand (owner, 2026-09-25: "remove the veo
        # option and only put the omni"): run_veo / veo_* are no longer
        # options; a request that still sends them is simply not read.
        # The pattern Omni lays behind the speaker (pattern_library): on by
        # default, structure (2.png) unless another is picked, entering with
        # the pattern's own animation unless one is chosen.
        "pattern_on": _b("pattern_on", True),
        "pattern": pattern,
        "pattern_animation": pattern_library.normalize_animation(raw.get("pattern_animation")),
        # Off unless asked (API back-compat); the brand frame dresses a whole
        # reel, so a per-clip run never gets it.
        "run_brand": _b("run_brand", False) and target == "reel",
        "intensity": intensity,
        "notes": str(raw.get("notes") or "").strip()[:600],
        "scene_transitions": _b("scene_transitions", False),
        "transition_sec": max(0.8, min(3.0, tsec)),
        "brand_transitions": brand_style,
        "brand_heading": _text("brand_heading", 40),
        "brand_sub": _text("brand_sub", 140),
        "brand_logo_on_cards": _b("brand_logo_on_cards", False),
        "brand_resolution": resolution,
        # Topic icons from each scene's script, and the pattern sliding over
        # the picture's left edge: both on unless switched off.
        "brand_icons": _b("brand_icons", True),
        "brand_edge_patterns": _b("brand_edge_patterns", True),
        # "legacy" sends a scene run down the old loop even when the harness
        # is on (rebrand_harness.runner.enabled); absent otherwise.
        **({"path": "legacy"} if raw.get("path") == "legacy" else {}),
    }


# ---------------------------------------------------------------------------
# Reading the reel
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    kind: str                 # intro | scene | outro
    index: int
    source: Path              # video or still on disk
    is_still: bool
    prompt_used: str = ""
    duration: float = 0.0
    quality: str = "normal"
    base: Optional[Path] = None   # normalised copy (shared by both arms)


@dataclass
class Ctx:
    exp_id: int
    run_id: int
    aspect: str
    width: int
    height: int
    work: Path
    options: dict
    run_ctx: dict
    intro: Optional[Segment]
    scenes: list[Segment]
    outro: Optional[Segment]
    frames: dict[str, Optional[Path]] = field(default_factory=dict)
    logo: Optional[Path] = None            # off (owner): passed to build_brief as None
    brief: dict = field(default_factory=dict)
    target_seg: Optional[Segment] = None   # per-clip runs: the clip being rebranded
    # Omni's one reference image: a 42 pattern coloured from the intro. None
    # when Omni edits no scene, or when building it failed (`pattern_error`
    # says why) — Omni then runs with no reference at all, never the logo.
    pattern_ref: Optional[pattern_ref.PatternRef] = None
    pattern_error: str = ""
    # An auto-placed pattern (pattern_library: several candidate places): how
    # pattern_planner chose this run's place — for the step and the evidence.
    placement_report: Optional[dict] = None
    # Every pattern of the recipe, in draw order (up to three; pattern_ref is
    # the first), and each one's build (element id, place, report, notes).
    pattern_refs: list = field(default_factory=list)
    pattern_results: list = field(default_factory=list)
    # The recipe the run follows (rebrand_harness.recipe): the studio's own,
    # or the one the legacy options imply. `harness`: a scene is edited by
    # rebrand_harness.runner; `prior_stages`: the parent version's manifest
    # a retry may reuse from.
    recipe: Optional[hrecipe.Recipe] = None
    harness: bool = False
    prior_stages: dict = field(default_factory=dict)
    # The finished version this one builds on (options.base, checked):
    # "rebuild" hands its manifest on as prior_stages; "stack" adds the
    # recipe's layers on top of it (rebrand_harness.base). None: a fresh one.
    base: Optional[hbase.BaseVersion] = None


async def _anim_row(anim_id: int) -> Optional[dict]:
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status, local_path, storage_bucket, storage_path,
                       prompt_used, duration_sec
                  FROM yas_animations WHERE id = %s
                """,
                (int(anim_id),),
            )
            r = await cur.fetchone()
    return dict(r) if r else None


async def _anim_video(anim_id: int, cache: Path) -> tuple[Optional[Path], dict]:
    """Local path for a ready Veo clip, by the one lookup the reel's stitch
    uses too (th_reel.locate_anim: its local_path, the same file under
    animations/ — a row made on another machine carries its path — then
    `cache`), else recovered from the bucket into `cache`."""
    row = await _anim_row(anim_id)
    if not row or row.get("status") != th.STATUS_READY:
        return None, row or {}
    return await th_reel.fetch_anim(int(anim_id), row, cache=cache), row


async def _bookend(
    role: str, *, run_id: int, md: dict, refs: dict, cache: Path, force: bool = False,
) -> Optional[Segment]:
    """Same priority as the TH v2 stitch: uploaded video as-is → Veo bookend
    animation → still card (held on screen). `force` = this bookend is the
    per-clip target, so its stitch exclusion does not apply."""
    if md.get(f"{role}_excluded") and not force:
        return None
    vid_upload = refs.get(f"{role}_video_upload_id")
    if vid_upload:
        try:
            p = await th._resolve_upload_path(int(vid_upload))
            return Segment(kind=role, index=0, source=p, is_still=False)
        except Exception:  # noqa: BLE001
            log.warning("[rebrand] run=%d %s upload video missing", run_id, role)
    anim_id = md.get(f"{role}_animation_id")
    if anim_id:
        p, row = await _anim_video(int(anim_id), cache)
        if p:
            return Segment(kind=role, index=0, source=p, is_still=False,
                           prompt_used=row.get("prompt_used") or "")
    resolver = th._resolve_intro_card_path if role == "intro" else th._resolve_outro_card_path
    try:
        still = await resolver(run_id=run_id, md=md, refs=refs)
    except Exception:  # noqa: BLE001
        still = None
    if still and still.exists():
        return Segment(kind=role, index=0, source=still, is_still=True)
    return None


async def _scenes(
    md: dict, cache: Path, *, strict: bool = True, force_index: Optional[int] = None,
) -> list[Segment]:
    """Included scenes, in order. `strict` (whole reel): every included scene
    must have its video. Per-clip runs are best-effort for the neighbours but
    strict for `force_index` (the target), which is kept even if excluded."""
    out: list[Segment] = []
    scene_count = int(md.get("scene_count") or 1)
    if scene_count > 1 and (md.get("scenes") or []):
        for i, sm in enumerate(md["scenes"]):
            is_target = force_index == i
            if (sm.get("excluded") and not is_target) or not sm.get("animation_id"):
                if is_target:
                    raise HTTPException(409, f"Scene {i + 1} has no video yet.")
                continue
            p, row = await _anim_video(int(sm["animation_id"]), cache)
            if not p:
                if strict or is_target:
                    raise HTTPException(409, f"Scene {i + 1} video is not available on disk or in the bucket.")
                continue
            out.append(Segment(
                kind="scene", index=i, source=p, is_still=False,
                prompt_used=row.get("prompt_used") or "",
                quality=th._norm_quality(sm.get("video_quality")),
            ))
    else:
        aid = md.get("main_animation_id") or md.get("veo_animation_id")
        if aid:
            p, row = await _anim_video(int(aid), cache)
            if not p:
                raise HTTPException(409, "The main video is not available on disk or in the bucket.")
            out.append(Segment(
                kind="scene", index=0, source=p, is_still=False,
                prompt_used=row.get("prompt_used") or "",
                quality=th._norm_quality(md.get("main_video_quality") or md.get("video_quality")),
            ))
    if not out:
        raise HTTPException(409, "This reel has no talking-head clip ready yet.")
    return out


def _run_ctx(run: dict, md: dict, scenes: list[Segment], aspect: str) -> dict:
    script = md.get("script") if isinstance(md.get("script"), dict) else {}
    dialogue: list[str] = []
    # Per scene INDEX (the brand frame's topic icons need each clip's own
    # line; `dialogue` below drops the empty ones, so its positions drift).
    scene_dialogue: dict[int, str] = {}
    if int(md.get("scene_count") or 1) > 1:
        for seg in scenes:
            sm = (md.get("scenes") or [])[seg.index] if seg.index < len(md.get("scenes") or []) else {}
            dialogue.append(str(sm.get("dialogue") or ""))
            scene_dialogue[seg.index] = dialogue[-1]
    else:
        dialogue.append(str(script.get("dialogue") or ""))
        for seg in scenes:
            scene_dialogue[seg.index] = dialogue[-1]
    return {
        "topic": run.get("topic") or "",
        "hook": script.get("hook") or "",
        "dialogue": [d for d in dialogue if d],
        "scene_dialogue": scene_dialogue,
        "intro_headline": script.get("intro_headline") or "",
        "intro_subtitle": script.get("intro_subtitle") or "",
        "outro_headline": script.get("outro_headline") or "",
        "outro_subtitle": script.get("outro_subtitle") or "",
        "intro_style": md.get("intro_animation_style"),
        "outro_style": md.get("outro_animation_style"),
        "aspect": aspect,
        "scenes": len(scenes),
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _step(ctx: Ctx, arm: str, name: str, status: str = "running", detail: str = "") -> None:
    try:
        await store.append_arm_step(ctx.exp_id, arm, {"name": name, "status": status, "detail": detail[:300]})
    except Exception:  # noqa: BLE001 — progress reporting must never kill a run
        log.exception("[rebrand] step report failed")


async def _thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _frame(video_or_still: Path, out: Path, at: str) -> Path:
    if video_or_still.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        return video_or_still
    return await _thread(media.extract_frame, video_or_still, out, at=at)


async def _transition_clip(
    ctx: Ctx, generated: Path, out: Path,
) -> Path:
    """Time-compress a generated 4 s transition to the requested length, keep
    both end frames, and lay a whoosh under it."""
    dur = await _thread(media.probe_duration, generated) or float(GEN_TRANSITION_SEC)
    speed = max(1.0, dur / float(ctx.options["transition_sec"]))
    return await _thread(
        media.normalize, generated, out,
        width=ctx.width, height=ctx.height, audio="whoosh", speed=speed,
    )


def _assemble_list(
    ctx: Ctx,
    *,
    t_in: Optional[Path],
    scenes: list[Path],
    between: list[Optional[Path]],
    t_out: Optional[Path],
) -> list[Path]:
    segs: list[Path] = []
    if ctx.intro and ctx.intro.base:
        segs.append(ctx.intro.base)
        if t_in:
            segs.append(t_in)
    for i, s in enumerate(scenes):
        segs.append(s)
        if i < len(scenes) - 1 and i < len(between) and between[i]:
            segs.append(between[i])  # type: ignore[arg-type]
    if ctx.outro and ctx.outro.base:
        if t_out:
            segs.append(t_out)
        segs.append(ctx.outro.base)
    return segs


async def _finish_arm(ctx: Ctx, arm: str, final: Path, parts: dict[str, Path], extra: dict) -> None:
    """Persist + critique a finished arm and mark it ready.

    The brand arm is NOT critiqued: the critic scores against the Omni
    brief, which a deterministic 42 template neither has nor follows (and a
    brand-only run must not need Vertex at all). Nor is a scene made in
    house (extra engine "house"): there is no brief, and it costs nothing."""
    bucket, key = await asset_storage.persist_local_file(
        abs_path=final, bucket_key=f"yas/rebrand/{ctx.exp_id}/{arm}.mp4",
    )
    review = None
    if arm != "brand" and extra.get("engine") != "house":
        await _step(ctx, arm, "Self-review (brand QA)")
        try:
            samples = await _thread(media.sample_frames, final, ctx.work / arm / "review", count=6)
            review = await rebrand_brief.critique(
                arm=arm, brief=ctx.brief, frames=samples, original_ref=ctx.frames.get("th_first"),
                target=ctx.options["target"],
            )
        except Exception:  # noqa: BLE001
            log.exception("[rebrand] critique failed for %s (non-fatal)", arm)
    duration = await _thread(media.probe_duration, final)
    frames = (((extra.get("stages") or {}).get("render") or {}).get("c0") or {}).get("frames")
    if extra.get("engine") == "house" and frames:
        # Made in house: its length is its frames (a copied sound can run a
        # packet past a picture cut short at a user end).
        duration = int(frames) / float(media.FPS)
    await store.patch_arm(ctx.exp_id, arm, {
        **extra,
        "status": store.ARM_READY,
        "final_path": th._to_rel(final),
        "parts": {k: th._to_rel(v) for k, v in parts.items()},
        "storage_bucket": bucket,
        "storage_path": key,
        "duration_sec": round(duration or 0.0, 2),
        "auto_review": review,
        "finished_at": _now(),
    })
    if review:
        done_detail = f"auto-score {review.get('score')}/10"
    elif extra.get("engine") == "house":
        done_detail = f"made in house · {round(duration or 0.0, 1)} s · $0"
    elif arm == "brand":
        done_detail = (f"{extra.get('resolution') or ''} · {round(duration or 0.0, 1)} s · graphics "
                       f"{extra.get('graphics_seconds', 0)} s, composite {extra.get('composite_seconds', 0)} s")
    else:
        done_detail = ""
    await _step(ctx, arm, "Done", status="done", detail=done_detail)


async def _fail_arm(ctx: Ctx, arm: str, err: BaseException | str) -> None:
    msg = str(err)[:500] or type(err).__name__
    await store.patch_arm(ctx.exp_id, arm, {
        "status": store.ARM_FAILED, "error": msg, "finished_at": _now(),
    })
    await _step(ctx, arm, "Failed", status="failed", detail=msg)


# ---------------------------------------------------------------------------
# Shared arm helpers (used by the whole-reel AND the per-clip flows)
# ---------------------------------------------------------------------------

def _with_pattern_clause(ctx: Ctx, p: str, *, entrance: bool = False) -> str:
    """`p` followed by the run's pattern clause (rebrand_harness.prompt, where
    the approved words live); `p` alone when the run has no pattern."""
    ref = ctx.pattern_ref
    if not ref:
        return p
    return p + hprompt.pattern_clause(ref, entrance=entrance)


# Things Omni paints when a prompt names them (rebrand_harness.prompt).
_PAINTABLE = hprompt.PAINTABLE
_unpaintable = hprompt.unpaintable


def _entrance_prompt(ctx: Ctx) -> Optional[str]:
    """The first chunk's prompt when the pattern has an entrance; None when
    there is no pattern or its animation is "none" (and then no chunk is
    guarded for an entrance veil either)."""
    if not hprompt.has_entrance(ctx.pattern_ref):
        return None
    return _omni_scene_prompt(ctx, entrance=True)


def _omni_scene_prompt(ctx: Ctx, *, entrance: bool = False) -> str:
    return _with_pattern_clause(
        ctx, _unpaintable(ctx.brief.get("omni_scene_prompt") or ""), entrance=entrance,
    )


def _omni_card_prompt(ctx: Ctx) -> str:
    # The card already carries its own logo + headline and gets no reference
    # image (it IS the source of the pattern's colours) — no clause, the
    # prompt itself says to keep every letter and mark exactly.
    return ctx.brief.get("omni_card_prompt") or ""


def _slug(name: str) -> str:
    return name.replace(" ", "_").lower()


HOUSE_NEVER_OMNI = "A version made in house reached Omni — stopped before any call."


def _never_omni(ctx: Ctx) -> None:
    """A run made in house never calls Omni: were one ever to get here, it
    stops (a RuntimeError, before any call) rather than pay."""
    if _made_in_house(ctx) or (ctx.options or {}).get("engine") == "house":
        raise RuntimeError(HOUSE_NEVER_OMNI)


async def _omni_edit(
    ctx: Ctx, arm: str, seg: Segment, prompt: str, out_dir: Path, label: str,
    *, first_prompt: Optional[str] = None,
) -> Path:
    """Omni-edit one clip (≤10 s chunks), then re-mux the clip's ORIGINAL audio
    so speech/lip-sync (or the card's sound) is untouched. Raises on failure.
    `first_prompt`, when given, replaces `prompt` for the first chunk only
    (the pattern's entrance)."""
    _never_omni(ctx)
    await _step(ctx, arm, f"{label}: Omni edit")
    # The normalised 720p copy: small enough to send inline and already on
    # the output canvas.
    src = seg.base or seg.source
    chunks = await _thread(media.split_chunks, src, out_dir / f"chunks_{_slug(label)}")
    # Scenes get the coloured pattern (never the logo); cards get nothing.
    # Each scene chunk is shown its own first frame with the pattern already
    # in the strip (the same pattern window every time, so a >10 s clip cannot
    # change texture at its chunk seam).
    with_pattern = bool(ctx.pattern_ref and seg.kind == "scene")
    fitted: list[Path] = []
    for j, chunk in enumerate(chunks):
        refs: list[Path] = []
        if with_pattern:
            first = await _frame(chunk, out_dir / f"{_slug(label)}_c{j}_first.png", "first")
            # The pattern goes BEHIND the speaker: their mask cuts it out of
            # the target. Without one it is laid over everything, and the
            # prompt alone says where it belongs.
            person = await person_matte.person_mask(first)
            if person is None:
                await _step(ctx, arm, f"{label}: no speaker mask", status="failed",
                            detail="the pattern is shown over the speaker; the prompt still "
                                   "puts it behind them")
            refs = [await _thread(
                pattern_ref.strip_preview, ctx.pattern_ref, first,
                out_dir / f"{_slug(label)}_c{j}_target.png", person=person,
            )]
        res = await omni.edit_video(
            chunk, first_prompt if (j == 0 and first_prompt) else prompt, aspect=ctx.aspect,
            images=refs,
            out=out_dir / f"{_slug(label)}_c{j}_raw.mp4",
        )
        clip = await _thread(
            media.fit_video_to_audio, res["path"], chunk,
            out_dir / f"{_slug(label)}_c{j}.mp4",
            width=ctx.width, height=ctx.height,
        )
        if with_pattern:
            # Omni draws the pattern over the speaker whatever it is told, so
            # the speaker is put back in front of it here, frame by frame,
            # from the original chunk (speaker_front). A failure keeps Omni's
            # clip rather than losing the edit.
            # The chunk carrying the entrance is also cleared of the frosted
            # panel Omni sometimes slides in with the pattern (speaker_front
            # .veil_restore_mask); no other chunk has an entrance to guard.
            entering = j == 0 and bool(first_prompt)
            try:
                clip = await _thread(
                    speaker_front.restore_speaker, clip, chunk,
                    out_dir / f"{_slug(label)}_c{j}_front.mp4",
                    clear_veil_sec=speaker_front.VEIL_GUARD_SEC if entering else 0.0,
                    # The bars were ignored when the target was cut
                    # (strip_preview); ignored here too, or MODNet's specks
                    # there paste the black bar back over Omni's pattern.
                    clear_bars=ctx.pattern_ref.clears_bars,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("[rebrand] putting the speaker in front failed on %s chunk %d",
                              label, j)
                await _step(ctx, arm, f"{label}: speaker not put in front", status="failed",
                            detail=f"Omni's clip kept as is ({str(e)[:160]})")
        fitted.append(clip)
    out = fitted[0] if len(fitted) == 1 else await _thread(
        media.concat, fitted, out_dir / f"{_slug(label)}.mp4",
    )
    await store.patch_arm(ctx.exp_id, arm, {"provider": omni._preferred})
    await _step(ctx, arm, f"{label}: edited", status="done", detail=f"via {omni._preferred}")
    return out


async def _omni_transition(
    ctx: Ctx, arm: str, first: Path, last: Path, prompt_key: str, name: str, out_dir: Path,
) -> Optional[Path]:
    """Omni first→last-frame clip, time-compressed + whoosh. None on a
    per-transition failure; OmniUnavailable propagates (no provider at all)
    — and so does a run made in house that got here (_never_omni: outside
    the per-transition try, so it is never taken for a skipped one)."""
    _never_omni(ctx)
    await _step(ctx, arm, f"{name} (Omni first→last frame)")
    try:
        res = await omni.interpolate(
            first, last, ctx.brief.get(prompt_key) or "", aspect=ctx.aspect,
            duration_sec=GEN_TRANSITION_SEC, out=out_dir / f"{_slug(name)}_raw.mp4",
        )
        clip = await _transition_clip(ctx, res["path"], out_dir / f"{_slug(name)}.mp4")
        await store.patch_arm(ctx.exp_id, arm, {"provider": omni._preferred})
        await _step(ctx, arm, f"{name}: done", status="done")
        return clip
    except omni.OmniUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("[rebrand] omni %s failed", name)
        await _step(ctx, arm, f"{name}: skipped", status="failed", detail=str(e))
        return None


async def _gather_jobs(jobs: dict[str, Any]) -> dict[str, Optional[Path]]:
    """Run independent generations concurrently; re-raise the first hard error
    (e.g. OmniUnavailable) only after every job has settled."""
    if not jobs:
        return {}
    outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
    results: dict[str, Optional[Path]] = {}
    for key, res in zip(jobs.keys(), outcomes):
        if isinstance(res, BaseException):
            raise res
        results[key] = res
    return results


# ---------------------------------------------------------------------------
# Omni arm — whole reel (switched off for new work: start() refuses it while
# Omni is off, so only an older experiment ever shows it)
# ---------------------------------------------------------------------------

async def _omni_arm(ctx: Ctx) -> None:
    arm = "omni"
    await store.patch_arm(ctx.exp_id, arm, {"status": store.ARM_RUNNING, "started_at": _now(), "model": omni.OMNI_MODEL})
    out_dir = ctx.work / arm
    parts: dict[str, Path] = {}
    edited: list[Path] = []
    edited_ok = 0
    try:
        # 1) edit every talking-head clip
        # The pattern enters once, on the reel's first scene; later scenes
        # open with it already in place.
        prompt = _omni_scene_prompt(ctx)
        entrance = _entrance_prompt(ctx)
        for seg in ctx.scenes:
            label = f"Scene {seg.index + 1}"
            try:
                scene_out = await _omni_edit(
                    ctx, arm, seg, prompt, out_dir, label,
                    first_prompt=entrance if seg is ctx.scenes[0] else None,
                )
                edited.append(scene_out)
                parts[f"scene_{seg.index}"] = scene_out
                edited_ok += 1
            except omni.OmniUnavailable:
                raise
            except Exception as e:  # noqa: BLE001 — keep the original scene, carry on
                log.exception("[rebrand] omni edit failed on %s", label)
                edited.append(seg.base)  # type: ignore[arg-type]
                await _step(ctx, arm, f"{label}: kept original", status="failed", detail=str(e))

        # 2) transitions from the exact boundary frames, concurrently (the
        #    omni_client semaphore still caps API concurrency)
        jobs: dict[str, Any] = {}
        if ctx.intro and ctx.frames.get("intro_last"):
            first_of_reel = await _frame(edited[0], out_dir / "reel_first.png", "first")
            jobs["transition_in"] = _omni_transition(
                ctx, arm, ctx.frames["intro_last"], first_of_reel,  # type: ignore[arg-type]
                "omni_transition_in_prompt", "Transition in", out_dir)
        if ctx.outro and ctx.frames.get("outro_first"):
            last_of_reel = await _frame(edited[-1], out_dir / "reel_last.png", "last")
            jobs["transition_out"] = _omni_transition(
                ctx, arm, last_of_reel, ctx.frames["outro_first"],  # type: ignore[arg-type]
                "omni_transition_out_prompt", "Transition out", out_dir)
        if ctx.options["scene_transitions"] and len(edited) > 1:
            for i in range(len(edited) - 1):
                a = await _frame(edited[i], out_dir / f"s{i}_last.png", "last")
                b = await _frame(edited[i + 1], out_dir / f"s{i + 1}_first.png", "first")
                jobs[f"between_{i}"] = _omni_transition(
                    ctx, arm, a, b, "omni_scene_transition_prompt", f"Scene transition {i + 1}", out_dir)
        results = await _gather_jobs(jobs)
        parts.update({k: v for k, v in results.items() if v})
        t_in, t_out = results.get("transition_in"), results.get("transition_out")
        between = [results.get(f"between_{i}") for i in range(max(0, len(edited) - 1))]

        if edited_ok == 0 and not (t_in or t_out):
            raise RuntimeError("Omni produced nothing usable (every scene and transition failed)")

        # 3) stitch
        await _step(ctx, arm, "Stitching the rebranded reel")
        final = await _thread(
            media.concat,
            _assemble_list(ctx, t_in=t_in, scenes=edited, between=between, t_out=t_out),
            out_dir / "final.mp4",
        )
        await _finish_arm(ctx, arm, final, parts, {
            "provider": omni._preferred,
            "scenes_edited": edited_ok,
            "scenes_total": len(ctx.scenes),
            "cost_note": "Omni is not metered here — check the provider console.",
        })
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("[rebrand] omni arm failed for experiment #%d", ctx.exp_id)
        await _fail_arm(ctx, arm, e)


# ---------------------------------------------------------------------------
# Brand arm — whole reel, NOTHING regenerated (services/brand_overlay.py)
# ---------------------------------------------------------------------------

_BRAND_STEPS = {
    "measure": "Measuring each clip's frame",
    "graphics": "Drawing the 42 graphics",
    "composite": "Compositing over the untouched footage",
}


def _brand_dims(aspect: str, resolution: str) -> tuple[int, int]:
    """1080 → 1080x1920 (9:16) / 1920x1080 (16:9); 720 likewise."""
    w, h = BRAND_RESOLUTIONS.get(str(resolution), BRAND_RESOLUTIONS["1080"])
    return (h, w) if aspect == "16:9" else (w, h)


def _cut_at_word(text: str, limit: int) -> str:
    """`text` (whitespace collapsed) whole when it has at most `limit`
    characters, else cut at a word boundary and closed with "…" (counted in
    the limit); "" when not even the first word fits."""
    words = " ".join(str(text or "").split())
    if len(words) <= limit:
        return words
    out = ""
    for w in words.split(" "):
        cand = f"{out} {w}" if out else w
        if len(cand) > limit - 1:
            break
        out = cand
    out = out.rstrip(" ,;:-–—.")
    return out + "…" if out else ""


def _brand_text(options: dict, run_ctx: dict) -> Optional[dict]:
    """The heading + sub-line the frame types on. An option left as None means
    "auto from the script"; "" means no text. None when both end up empty.

    The auto heading is the intro headline WHOLE or nothing. It used to be cut
    at a word to 22 characters, which turned 22 of the 30 most recent real
    headlines into typed fragments ("Why I Chose 42 Abu", "The Norminette
    is"). The composition sizes a heading itself (two lines, down to 40%) and
    leaves out — never cuts — one that cannot fit, keeping the sub-line, and
    reports it (`text_fit` on the arm). The auto sub is the hook, cut at a word
    only past what three lines at the smallest size always hold."""
    heading = options.get("brand_heading")
    sub = options.get("brand_sub")
    if heading is None:
        heading = " ".join(str(run_ctx.get("intro_headline") or "").split())
        if len(heading) > BRAND_HEADING_AUTO_MAX:
            heading = ""
    if sub is None:
        sub = _cut_at_word(run_ctx.get("hook") or "", BRAND_SUB_AUTO_MAX)
    heading, sub = str(heading or "").strip(), str(sub or "").strip()
    return {"heading": heading, "sub": sub} if (heading or sub) else None


def _brand_text_note(options: dict, text: Optional[dict], fit: Optional[dict]) -> str:
    """One line on how the text came out, from the composition's own report
    ("" when it was drawn as asked, or there was none). A heading taken from
    the script is named, since the user never typed it."""
    fit = fit or {}
    if not text or fit.get("status") in (None, "none", "shown"):
        return ""
    msg = str(fit.get("message") or "").strip()
    heading = (text.get("heading") or "").strip()
    if heading and options.get("brand_heading") is None and fit.get("heading_shown") is False:
        what = "shown" if fit.get("sub_shown") else "left out too"
        sub = f"; the sub-line is {what}" if (text.get("sub") or "").strip() else ""
        return (f'Auto heading "{heading}" left out: too long for the letterbox bars{sub}. '
                "Type a short heading (e.g. Myth / Fact) to show one.")
    return msg


def _brand_scene_text(sources: list[brand_overlay.BrandSource], run_ctx: dict) -> dict[int, str]:
    """{source position: that scene's dialogue} for the topic icons. Keys of
    `scene_dialogue` come back from the DB as strings, so both are accepted."""
    by_index = {int(k): str(v or "") for k, v in (run_ctx.get("scene_dialogue") or {}).items()}
    return {
        i: by_index[src.index]
        for i, src in enumerate(sources)
        if src.kind == "scene" and by_index.get(src.index)
    }


def _brand_sources(ctx: Ctx) -> list[brand_overlay.BrandSource]:
    """The reel in stitch order, from each Segment's RAW source — the 720p
    `base` copies exist for Omni, and compositing over them would be a
    second encode of every footage pixel."""
    out: list[brand_overlay.BrandSource] = []
    if ctx.intro:
        out.append(brand_overlay.BrandSource(
            kind="intro", index=0, path=ctx.intro.source, is_still=ctx.intro.is_still,
            still_seconds=INTRO_STILL_SEC if ctx.intro.is_still else 0.0))
    for seg in ctx.scenes:
        out.append(brand_overlay.BrandSource(
            kind="scene", index=seg.index, path=seg.source, is_still=seg.is_still))
    if ctx.outro:
        out.append(brand_overlay.BrandSource(
            kind="outro", index=0, path=ctx.outro.source, is_still=ctx.outro.is_still,
            still_seconds=OUTRO_STILL_SEC if ctx.outro.is_still else 0.0))
    return out


async def _brand_arm(ctx: Ctx) -> None:
    arm = "brand"
    opts = ctx.options
    width, height = _brand_dims(ctx.aspect, opts.get("brand_resolution") or "1080")
    resolution = f"{width}x{height}"
    style = opts.get("brand_transitions") or "auto"
    await store.patch_arm(ctx.exp_id, arm, {
        "status": store.ARM_RUNNING, "started_at": _now(),
        "model": "42 brand frame (Remotion graphics + ffmpeg, no generation)",
        "resolution": resolution,
    })

    async def _on_stage(key: str, detail: str) -> None:
        await _step(ctx, arm, _BRAND_STEPS.get(key, key), detail=detail)

    try:
        text = _brand_text(opts, ctx.run_ctx)
        sources = _brand_sources(ctx)
        res = await brand_overlay.render_brand_reel(
            sources, ctx.work / arm,
            width=width, height=height, style=style, text=text,
            logo_on_cards=bool(opts.get("brand_logo_on_cards")),
            scene_text=_brand_scene_text(sources, ctx.run_ctx) if opts.get("brand_icons", True) else None,
            edge_patterns=bool(opts.get("brand_edge_patterns", True)),
            on_stage=_on_stage,
        )
        note = _brand_text_note(opts, text, res.text_fit)
        if note:
            # What the frame left out or shrank, so it does not look like a
            # lost option.
            await _step(ctx, arm, "Text", status="done", detail=note)
        await _finish_arm(ctx, arm, res.final, {}, {
            "segments": res.segments,
            "seams": res.seams,
            "resolution": resolution,
            "transitions": style,
            "text": text,
            "text_fit": res.text_fit,
            "icons": res.icons,
            "edge_patterns": bool(opts.get("brand_edge_patterns", True)),
            "graphics_seconds": res.graphics_seconds,
            "composite_seconds": res.composite_seconds,
            "cost_note": "Nothing is generated — no model cost.",
        })
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("[rebrand] brand arm failed for experiment #%d", ctx.exp_id)
        await _fail_arm(ctx, arm, e)


# ---------------------------------------------------------------------------
# Per-clip arms — the ✨ Rebrand button on ONE Videos-stage tile
#
#   scene_N  omni: edit that clip (background + icons, speaker + audio kept)
#   intro    omni: restyle the card (text/logo kept) + transition INTO the
#                  talking head's first frame
#   outro    mirrored: transition OUT of the talking head's last frame, then
#            the (restyled | original) card
#
# Omni switched off (start(): omni_off_message), only a studio scene made in
# house reaches _omni_clip for new work; the card and Omni-scene branches
# stay for the versions they made and for the golden lock.
# ---------------------------------------------------------------------------

def _made_in_house(ctx: Ctx) -> bool:
    """Whether the run's scene is made by the in-house engine (a studio
    recipe with engine "house"): no Omni, no brief, no critique, no memory."""
    return bool(ctx.harness and ctx.recipe is not None and ctx.recipe.engine == "house")


async def _omni_clip(ctx: Ctx) -> None:
    arm = "omni"
    target = ctx.options["target"]
    house = _made_in_house(ctx)
    # The arm keeps its key ("omni": every reader of a version reads it); a
    # scene made in house says so, and what drew it.
    await store.patch_arm(ctx.exp_id, arm, {"status": store.ARM_RUNNING, "started_at": _now(),
                                            **({"model": hhouse.HOUSE_MODEL, "engine": "house",
                                                "provider": hhouse.PROVIDER}
                                               if house else {"model": omni.OMNI_MODEL})})
    out_dir = ctx.work / arm
    parts: dict[str, Path] = {}
    try:
        seg = ctx.target_seg
        if seg is None:
            raise RuntimeError(f"no clip found for {target}")
        if house and seg.kind in ("scene", "intro", "outro"):
            # A scene, or an intro or outro (made by hand in the studio):
            # our own graphics over the untouched clip.
            manifest = hstages.Manifest()
            label = f"Scene {seg.index + 1}" if seg.kind == "scene" else seg.kind.capitalize()
            final = await hhouse.edit_scene(ctx, arm, seg, out_dir, label, manifest)
            parts[f"scene_{seg.index}" if seg.kind == "scene" else seg.kind] = final
            await _finish_arm(ctx, arm, final, parts, {
                "scenes_edited": 1, "scenes_total": 1, "stages": manifest.public(),
                "recipe_hash": hrecipe.recipe_hash(ctx.recipe), "provider": hhouse.PROVIDER, "engine": "house",
                "cost_usd": 0.0, "cost_note": "Made in house — no model cost.",
            })
            return
        if seg.kind == "scene" and ctx.harness:
            manifest = hstages.Manifest(ctx.prior_stages)
            final = await hrunner.edit_scene(ctx, arm, seg, out_dir, f"Scene {seg.index + 1}",
                                             manifest)
            parts[f"scene_{seg.index}"] = final
            extra = {"scenes_edited": 1, "scenes_total": 1, "stages": manifest.public(),
                     "recipe_hash": hrecipe.recipe_hash(ctx.recipe)}
        elif seg.kind == "scene":
            final = await _omni_edit(
                ctx, arm, seg, _omni_scene_prompt(ctx), out_dir, f"Scene {seg.index + 1}",
                first_prompt=_entrance_prompt(ctx),
            )
            parts[f"scene_{seg.index}"] = final
            extra = {"scenes_edited": 1, "scenes_total": 1}
        else:
            label = "Intro" if seg.kind == "intro" else "Outro"
            body: Path = seg.base  # type: ignore[assignment]
            restyled = False
            if not seg.is_still:
                try:
                    body = await _omni_edit(ctx, arm, seg, _omni_card_prompt(ctx), out_dir, label)
                    parts[seg.kind] = body
                    restyled = True
                except omni.OmniUnavailable:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("[rebrand] omni %s restyle failed", label)
                    await _step(ctx, arm, f"{label}: kept original", status="failed", detail=str(e))
            if seg.kind == "intro":
                start = await _frame(body, out_dir / "intro_last.png", "last")
                trans = await _omni_transition(
                    ctx, arm, start, ctx.frames["th_first"],  # type: ignore[arg-type]
                    "omni_transition_in_prompt", "Transition in", out_dir)
                if trans:
                    parts["transition_in"] = trans
                segs = [body] + ([trans] if trans else [])
            else:
                end = await _frame(body, out_dir / "outro_first.png", "first")
                trans = await _omni_transition(
                    ctx, arm, ctx.frames["th_last"], end,  # type: ignore[arg-type]
                    "omni_transition_out_prompt", "Transition out", out_dir)
                if trans:
                    parts["transition_out"] = trans
                segs = ([trans] if trans else []) + [body]
            if not restyled and not trans:
                raise RuntimeError(f"Omni produced nothing usable for the {label.lower()}")
            await _step(ctx, arm, f"Stitching the {label.lower()}")
            final = await _thread(media.concat, segs, out_dir / "final.mp4")
            extra = {"restyled": restyled}
        await _finish_arm(ctx, arm, final, parts, {
            **extra, "provider": omni._preferred,
            "cost_note": "Omni is not metered here — check the provider console.",
        })
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("[rebrand] omni clip arm failed for experiment #%d", ctx.exp_id)
        await _fail_arm(ctx, arm, e)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

async def _prepare(exp_id: int, run_id: int, options: dict) -> Ctx:
    run = await th._fetch_run_row(run_id)
    md = run["metadata"] or {}
    refs = dict(md.get("refs") or {})
    aspect = th._norm_aspect(md.get("aspect_ratio"))
    width, height = media.dims_for(aspect)
    work = REBRAND_ROOT / str(exp_id)
    cache = REBRAND_ROOT / "_cache"
    work.mkdir(parents=True, exist_ok=True)

    target = options.get("target") or "reel"
    scene_target = int(target.split("_", 1)[1]) if target.startswith("scene_") else None
    if target in ("intro", "outro") and options.get("recipe") and hrunner.enabled(options) \
            and _recipe_of(options).engine == "house":
        return await _prepare_house_bookend(exp_id, run_id, options, run, md, aspect, width, height, work)
    intro = await _bookend("intro", run_id=run_id, md=md, refs=refs, cache=cache,
                           force=target == "intro")
    scenes = await _scenes(md, cache, strict=target == "reel", force_index=scene_target)
    outro = await _bookend("outro", run_id=run_id, md=md, refs=refs, cache=cache,
                           force=target == "outro")
    if target == "intro" and not intro:
        raise HTTPException(409, "This reel has no intro clip to rebrand.")
    if target == "outro" and not outro:
        raise HTTPException(409, "This reel has no outro clip to rebrand.")
    ctx = Ctx(
        exp_id=exp_id, run_id=run_id, aspect=aspect, width=width, height=height,
        work=work, options=options, run_ctx=_run_ctx(run, md, scenes, aspect),
        intro=intro, scenes=scenes, outro=outro,
        # The logo is off for now (owner, 2026-09-25: "dont send 42 logo, we
        # dont need that for now"): not to Omni, not to the brief model. Set
        # it back to card_animation.LOGO_PATH to bring it back.
        logo=None,
    )
    ctx.run_ctx["target"] = target
    ctx.recipe = _recipe_of(options)
    ctx.harness = scene_target is not None and hrunner.enabled(options)
    await _link_base(ctx)
    if target == "intro":
        ctx.target_seg = intro
    elif target == "outro":
        ctx.target_seg = outro
    elif scene_target is not None:
        ctx.target_seg = next((sg for sg in scenes if sg.index == scene_target), None)

    # The normalised 720p copies and the boundary frames only feed Omni, Veo
    # and their brief. The brand arm reads the RAW sources, so a brand-only
    # run skips these encodes entirely.
    if not options.get("run_omni"):
        return ctx

    base = work / "base"
    if intro:
        ctx.frames["intro_last"] = await _frame(intro.source, base / "intro_last.png", "last")
        intro.base = await (
            _thread(media.still_to_clip, intro.source, base / "intro.mp4",
                    seconds=INTRO_STILL_SEC, width=width, height=height)
            if intro.is_still else
            _thread(media.normalize, intro.source, base / "intro.mp4",
                    width=width, height=height, audio="keep")
        )
    for seg in scenes:
        seg.base = await _scene_base(ctx, seg, base / f"scene{seg.index}.mp4")
        seg.duration = await _thread(media.probe_duration, seg.base) or 0.0
    ctx.frames["th_first"] = await _frame(scenes[0].source, base / "th_first.png", "first")
    # The reel's last picture is the last scene's own last frame: not the
    # flash of another shot a Veo source can end on, which the scene's
    # normalised copy has cut off (media.normalize_scene).
    ctx.frames["th_last"] = await _frame(scenes[-1].base, base / "th_last.png", "last")
    if outro:
        ctx.frames["outro_first"] = await _frame(outro.source, base / "outro_first.png", "first")
        outro.base = await (
            _thread(media.still_to_clip, outro.source, base / "outro.mp4",
                    seconds=OUTRO_STILL_SEC, width=width, height=height)
            if outro.is_still else
            _thread(media.normalize, outro.source, base / "outro.mp4",
                    width=width, height=height, audio="keep")
        )
    if hrunner.stacked(ctx) and ctx.target_seg is not None:
        # "Add on top": the version's parts, conformed to this clip's, and
        # their settled first frames — before the patterns, which are
        # planned on the first of them, off the version's own graphics.
        seg = ctx.target_seg
        chunks = await _thread(media.split_chunks, seg.base,
                               work / "omni" / f"chunks_scene_{seg.index + 1}")
        await hbase.stack_parts(ctx.base, chunks, base / f"on_v{ctx.base.version_id}",
                                width=width, height=height)
    # Omni's reference pattern, once per experiment — and only when Omni will
    # edit a talking-head scene: a card restyle sends no image, so an
    # intro/outro-only run would pick and colour one for nothing (and the
    # brief would tell Gemini Omni sees an image it never gets).
    if options.get("run_omni") and (target == "reel" or scene_target is not None):
        await _build_pattern_ref(ctx)
    return ctx


async def _prepare_house_bookend(exp_id: int, run_id: int, options: dict, run: dict, md: dict, aspect: str,
                                 width: int, height: int, work: Path) -> Ctx:
    """An intro or outro made in house (a studio recipe): the studio's own
    clip of it (studio.prepare — the reel's source for it, th_reel, on the
    canvas as the reel shows it, whole), copied as a scene's is
    (_scene_base), and its patterns (frozen at Generate). Nothing else of
    the reel is read or encoded: nothing here is for Omni or a brief."""
    import shutil

    from .rebrand_harness import studio

    target = options["target"]
    scene = await studio.prepare(run_id, target)
    made = scene.ctx.target_seg
    out = work / "base" / f"{target}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    for src, dst in ((media.scene_info_path(made.base), media.scene_info_path(out)), (made.base, out)):
        if Path(src).is_file():
            tmp = dst.with_name(f"{dst.stem}.part{dst.suffix}")
            await _thread(shutil.copyfile, src, tmp)
            await _thread(os.replace, tmp, dst)
    seg = Segment(kind=target, index=0, source=Path(made.source), is_still=bool(made.is_still), base=out)
    intro = scene.ctx.intro
    ctx = Ctx(
        exp_id=exp_id, run_id=run_id, aspect=aspect, width=width, height=height,
        work=work, options=options, run_ctx=_run_ctx(run, md, [], aspect),
        intro=seg if target == "intro" else intro, scenes=[], outro=seg if target == "outro" else None,
        logo=None,
    )
    ctx.run_ctx["target"] = target
    ctx.recipe = _recipe_of(options)
    ctx.harness = True
    ctx.target_seg = seg
    await _link_base(ctx)
    await _build_pattern_ref(ctx)
    return ctx


async def _scene_base(ctx: Ctx, seg: Segment, out: Path) -> Path:
    """A scene's normalised copy for this run: media.normalize_scene (the
    source on the canvas, minus a Veo tail flash). When the studio has
    prepared the same source at this canvas (studio.scene_base), its copy is
    copied rather than made again: the studio's previews, stills and parts
    were all made from those very bytes."""
    import shutil

    from .rebrand_harness import studio

    # The target scene kept whole past its automatic cut (options.variant
    # "full": a version made for an end past it): the studio's full-length
    # clip, or the same made here.
    full = (ctx.options or {}).get("variant") == "full" and (ctx.options or {}).get("target") == f"scene_{seg.index}"
    made = (await _thread(studio.scene_base, ctx.run_id, f"scene_{seg.index}", seg.source, full=full)
            / "base" / f"scene{seg.index}.mp4")
    if (made.is_file() and media.scene_info(made) is not None
            and await _thread(media.video_size, made) == (ctx.width, ctx.height)):
        out.parent.mkdir(parents=True, exist_ok=True)
        for src, dst in ((media.scene_info_path(made), media.scene_info_path(out)), (made, out)):
            tmp = dst.with_name(f"{dst.stem}.part{dst.suffix}")
            await _thread(shutil.copyfile, src, tmp)
            await _thread(os.replace, tmp, dst)
        return out
    if full:
        return await _thread(studio._full_clip, seg.source, out, width=ctx.width, height=ctx.height)
    return await _thread(media.normalize_scene, seg.source, out, width=ctx.width, height=ctx.height)


async def _link_base(ctx: Ctx) -> None:
    """The finished version the run builds on (options.base), checked again
    — it could have gone since start() — onto ctx.base. "Change it" (a
    rebuild, not a fresh take) is handed its manifest: a part whose chunk,
    prompt and target did not change reuses its Omni clip (options.take is
    the version's, so the keys can match). Nothing for a fresh run."""
    spec = hbase.BaseSpec.of(ctx.options.get("base"))
    if spec is None or not ctx.harness:
        return
    ctx.base = await hbase.load(spec, ctx.run_id, ctx.options.get("target"))
    if spec.mode == "rebuild" and not spec.fresh:
        ctx.prior_stages = hbase.omni_stages(ctx.base.exp)


def _recipe_of(options: dict) -> hrecipe.Recipe:
    """The run's recipe: the one the studio sent, else the one the legacy
    options imply (the 42 default for an untouched request)."""
    if options.get("recipe"):
        return hrecipe.normalize(options["recipe"])
    return hrecipe.recipe_from_options(options)


async def _build_pattern_ref(ctx: Ctx) -> None:
    """The run's pattern (rebrand_harness.elements.pattern: structure unless
    another was picked, or none when switched off), coloured in the intro's
    own colours. Failure degrades, never kills: Omni then runs with no
    reference image and no pattern clause, and the reason is kept for the
    evidence. An auto-placed pattern is placed here, once per experiment, on
    the clip Omni will edit first (_plan_frame)."""
    rcp = ctx.recipe or _recipe_of(ctx.options)
    intro = ctx.intro

    async def step(name: str, status: str = "running", detail: str = "") -> None:
        await _step(ctx, "omni", name, status, detail)

    async def plan_frame() -> Optional[Path]:
        return await _plan_frame(ctx)

    job = pattern_el.PatternJob(
        exp_id=ctx.exp_id, aspect=ctx.aspect, out_dir=ctx.work / "pattern",
        intro=intro.source if intro else None, intro_is_still=bool(intro and intro.is_still),
        seed=rcp.seed or f"rebrand:{ctx.exp_id}", plan_frame=plan_frame, step=step,
    )
    els = hrecipe.pattern_elements(rcp)
    if _made_in_house(ctx):
        await _build_house_patterns(ctx, rcp, els, job)
        return
    if hrunner.stacked(ctx):
        # Added on top of a finished version: every new pattern keeps off its
        # graphics (their ink and slots), and the speaker's mask is taken as
        # the studio's preview takes it, so both place alike.
        job = dataclasses.replace(job, person_mask=hbase.run_mask)
        results = await pattern_el.build_all(els, rcp.colours, job, occupied=ctx.base.occupied,
                                             taken=ctx.base.taken)
    elif len(els) <= 1:
        # One pattern (or none): the approved path, call for call.
        res = await pattern_el.build(hrecipe.pattern_element(rcp), rcp.colours, job)
        ctx.pattern_ref, ctx.placement_report, ctx.pattern_error = res.ref, res.report, res.error
        ctx.pattern_refs = [res.ref] if res.ref else []
        ctx.pattern_results = [res]
        return
    else:
        results = await pattern_el.build_all(els, rcp.colours, job)
    _keep_patterns(ctx, results)


def _keep_patterns(ctx: Ctx, results: list) -> None:
    """Several patterns' builds onto the run: every result, the built refs
    in draw order (the first is ctx.pattern_ref), and why any failed."""
    built = [r for r in results if r.ref is not None]
    ctx.pattern_results = results
    ctx.pattern_refs = [r.ref for r in built]
    ctx.pattern_ref = built[0].ref if built else None
    ctx.placement_report = built[0].report if built else None
    ctx.pattern_error = "; ".join(f"{r.element_id}: {r.error}" for r in results if r.error)


async def _local_mask(frame: Path) -> Any:
    """The speaker's mask on a frame as a run made in house takes it: the
    LOCAL matte only (person_matte.local_mask) — never the paid fallback."""
    return await person_matte.local_mask(Path(frame))


async def _local_run_mask(frame: Path) -> Any:
    """base.run_mask with the local matte only: quantised as the studio's
    (a pass added on top of a version places its patterns as the preview
    on it does)."""
    return hbase.quantised(await person_matte.local_mask(Path(frame)))


async def _build_house_patterns(ctx: Ctx, rcp: hrecipe.Recipe, els: list, job: pattern_el.PatternJob) -> None:
    """The patterns of a scene made in house: each exactly as Generate froze
    it (pattern_el.from_resolved — the preview's place and colour, no
    planner, no colour dig), when every one was; else planned as the
    studio's preview plans them, with the LOCAL matte (a recipe from an API
    client, or a pattern the preview could not draw). Added on top of a
    version, they keep off its graphics, as ever — added on top of a house
    pass, its passes' own patterns are laid out first (house.flatten, as the
    studio's preview on it lays them), to be drawn again under the new
    ones. Never a paid call."""
    if hrunner.stacked(ctx) and ctx.base.passes:
        await hhouse.flatten(ctx.base, job, mask=_local_run_mask)
    if els and all(pattern_el.frozen(e) for e in els):
        results = [await pattern_el.from_resolved(e, job) for e in els]
    elif hrunner.stacked(ctx):
        job = dataclasses.replace(job, person_mask=_local_run_mask)
        results = await pattern_el.build_all(els, rcp.colours, job, occupied=ctx.base.occupied,
                                             taken=ctx.base.taken)
    else:
        job = dataclasses.replace(job, person_mask=_local_mask)
        results = await pattern_el.build_all(els, rcp.colours, job)
    _keep_patterns(ctx, [r for r in results if r.element_id or r.ref is not None])


async def _plan_frame(ctx: Ctx) -> Optional[Path]:
    """The frame an auto-placed pattern is planned on: the first frame of the
    clip Omni edits first — the target scene of a per-clip run, else the
    reel's first scene (whose first frame _prepare already took as
    th_first). None when there is none (the pattern's default place).
    Added on top of a finished version: that version's settled first frame
    (base.stack_parts), as the studio's preview on it plans."""
    if hrunner.stacked(ctx) and ctx.base.frames:
        return ctx.base.frames[0]
    if ctx.target_seg is not None and ctx.target_seg.kind in ("intro", "outro"):
        # An intro or outro made in house: its own first frame (the studio
        # plans it there too).
        bk = ctx.target_seg
        try:
            return await _frame(bk.base or bk.source, ctx.work / "pattern" / f"plan_{bk.kind}_first.png", "first")
        except Exception as e:  # noqa: BLE001
            log.warning("[rebrand] experiment #%d: no frame to place the pattern on (%s)", ctx.exp_id,
                        str(e)[:200])
            return None
    target = ctx.target_seg if (ctx.target_seg and ctx.target_seg.kind == "scene") else None
    seg = target or (ctx.scenes[0] if ctx.scenes else None)
    try:
        if seg is not None and ctx.scenes and seg is ctx.scenes[0] and ctx.frames.get("th_first"):
            return ctx.frames["th_first"]
        if seg is not None:
            # The normalised copy when there is one: the frames Omni is shown
            # are cut from it, so the plan sees the same canvas.
            return await _frame(seg.base or seg.source,
                                ctx.work / "pattern" / f"plan_scene{seg.index}_first.png", "first")
    except Exception as e:  # noqa: BLE001
        log.warning("[rebrand] experiment #%d: no frame to place the pattern on (%s)",
                    ctx.exp_id, str(e)[:200])
    return None


async def _kept_brief(ctx: Ctx) -> Optional[dict]:
    """The brief a run built on a finished version opens with instead of a
    new one, or None (write one, as a fresh version does).

      stack    always the version's: the prompt opens with the harness's own
               words (a brief describes the first look and would ask for it
               again), so the brief is only what the self-review scores
               against. (stack_parts already refused another take of the
               clip.)
      rebuild  the version's when what its brief was told is unchanged
               (base.brief_reusable: director, the patterns in their places
               and colours, the icons rule — and the clip it saw, not a take
               made since); a brief written again comes back in other words,
               and every part's prompt — so every Omni call — would change
               with it. Never for a fresh take (Try again)."""
    bv = ctx.base
    if bv is None:
        return None
    brief = dict(bv.exp.get("brief") or {})
    if bv.mode == "stack":
        return brief
    if bv.spec.fresh:
        return None
    first = await _target_first(ctx)
    reusable = await _thread(hbase.brief_reusable, ctx.recipe, ctx.pattern_refs, ctx.options,
                             bv.exp, first=first)
    return brief if reusable else None


async def _target_first(ctx: Ctx) -> Optional[Path]:
    """The first frame of the clip the run edits (its normalised copy), or
    None when there is none or it cannot be read."""
    seg = ctx.target_seg
    src = (seg.base or seg.source) if seg is not None else None
    if src is None or not Path(src).exists():
        return None
    try:
        return await _frame(Path(src), ctx.work / "base" / f"scene{seg.index}_first.png", "first")
    except Exception:  # noqa: BLE001 — unknown: the brief is judged without it
        log.warning("[rebrand] #%s: the clip's first frame could not be read", ctx.exp_id)
        return None


async def _heartbeat(exp_id: int) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await store.touch(exp_id)
        except Exception:  # noqa: BLE001
            log.warning("[rebrand] heartbeat failed for experiment #%d", exp_id)


HOUSE_STAGE = "Making it in house (free)"


def _running_stage(options: dict) -> str:
    """Stage line naming only the arms that actually run."""
    names = []
    if options.get("run_brand"):
        names.append("the 42 brand frame")
    if options.get("run_omni"):
        names.append("Omni")
    if not names:
        return "Working"
    if len(names) == 1:
        return "Applying the 42 brand frame" if options.get("run_brand") else f"{names[0]} is working"
    joined = ", ".join(names[:-1]) + " and " + names[-1]
    return f"{joined[0].upper()}{joined[1:]} are working in parallel"


async def _run(exp_id: int, run_id: int, options: dict) -> None:
    ctx: Optional[Ctx] = None
    beat = asyncio.create_task(_heartbeat(exp_id), name=f"rebrand_beat_{exp_id}")
    try:
        await store.update_experiment(exp_id, status=store.ST_BRIEFING, stage="Reading the reel")
        ctx = await _prepare(exp_id, run_id, options)

        # The brief (Gemini) only steers the generating arms. The brand frame
        # is a fixed template and must work with Vertex unreachable, and a
        # scene made in house draws nothing a prompt could steer: neither
        # writes a brief, is critiqued or is remembered (no paid call).
        house = _made_in_house(ctx)
        generating = bool(options.get("run_omni")) and not house
        brief: dict = {}
        evidence: dict = {}
        kept = await _kept_brief(ctx) if generating else None
        if kept is not None:
            # Built on a finished version: its brief, not a new one (see
            # _kept_brief) — no Gemini call, and a "Change it" whose brief
            # opening is unchanged can reuse the parts Omni already made.
            brief = kept
            await store.update_experiment(
                exp_id, stage=f"Using the brief of version #{ctx.base.version_id}")
        elif generating:
            await store.update_experiment(
                exp_id, stage="Writing the brief (Instagram brand DNA + past rebrand lessons)",
            )
            # Several patterns: the brief sees every one (its opening must name
            # each in its own place). One pattern: exactly the approved call.
            extra = ({"extra_patterns": ctx.pattern_refs[1:]}
                     if len(ctx.pattern_refs) > 1 else {})
            brief, evidence = await rebrand_brief.build_brief(
                exp_id=exp_id, run_ctx=ctx.run_ctx, frames=ctx.frames,
                logo=ctx.logo, options=options, pattern_ref=ctx.pattern_ref, **extra,
            )
        # Which pattern Omni got (or why it got none). It was made in
        # _prepare, but the evidence dict is born in build_brief, so it is
        # recorded here.
        if ctx.pattern_ref:
            evidence["omni_pattern"] = ctx.pattern_ref.public()
            if ctx.placement_report:
                evidence["omni_pattern"]["placement_report"] = ctx.placement_report
        elif ctx.pattern_error:
            evidence["omni_pattern_error"] = ctx.pattern_error
        if len(ctx.pattern_refs) > 1:
            evidence["omni_patterns"] = [
                {**r.ref.public(), "element_id": r.element_id, "placement_report": r.report,
                 "moved": r.moved, "overlap": r.overlap, "warnings": r.warnings}
                for r in ctx.pattern_results if r.ref is not None
            ]
            if ctx.pattern_error:
                evidence["omni_pattern_error"] = ctx.pattern_error
        evidence["target"] = options["target"]
        if ctx.harness and ctx.recipe is not None:
            evidence["recipe"] = ctx.recipe.model_dump(mode="json")
            evidence["recipe_hash"] = hrecipe.recipe_hash(ctx.recipe)
        if house:
            evidence["engine"] = "house"
        if ctx.base is not None:
            evidence["base"] = {**ctx.base.spec.public(),
                                "depth": ctx.base.depth + 1 if ctx.base.mode == "stack" else 0}
            if kept is not None:
                evidence["brief_from"] = ctx.base.version_id
        evidence["segments"] = {
            "intro": (("still" if ctx.intro.is_still else "video") if ctx.intro else "none"),
            "scenes": len(ctx.scenes),
            "outro": (("still" if ctx.outro.is_still else "video") if ctx.outro else "none"),
        }
        ctx.brief = brief
        # A brief opening written for one pattern ("graphics ONLY in the left
        # half") would steer Omni away from the others: with several, such an
        # opening gives way to the harness's own for this run (the stored
        # brief is kept as written) — as does one that still asks Omni for
        # icons when ours are stamped. runner.run_brief — the studio predicts
        # a "Change it" run's prompts with it too.
        if ctx.harness and not house:
            ctx.brief, why = hrunner.run_brief(ctx.recipe, brief, ctx.pattern_refs)
            if why:
                evidence["brief_head_fallback"] = why
                await _step(ctx, "omni", "Brief opening not used", status="done",
                            detail=f"{why}; the harness's own opening is used instead")
        fields: dict[str, Any] = {"evidence": evidence}
        if generating:
            fields["brief"] = brief
        await store.update_experiment(
            exp_id, **fields, status=store.ST_RUNNING,
            stage=HOUSE_STAGE if house else _running_stage(options),
        )

        whole_reel = options["target"] == "reel"
        arms = []
        if options.get("run_brand") and whole_reel:
            arms.append(_brand_arm(ctx))
        if options["run_omni"]:
            arms.append(_omni_arm(ctx) if whole_reel else _omni_clip(ctx))
        await asyncio.gather(*arms)

        exp = await store.get_experiment(exp_id) or {}
        arm_states = exp.get("arms") or {}
        any_ready = any((arm_states.get(a) or {}).get("status") == store.ARM_READY for a in store.ARMS)
        # Made in house: no brief, so no lesson for the next one.
        embed_text, lesson = (("", "") if house else
                              rebrand_brief.summarize_for_memory(ctx.run_ctx, brief, arm_states, None))
        await store.update_experiment(
            exp_id,
            status=store.ST_READY if any_ready else store.ST_FAILED,
            stage="Done" if any_ready else ("Failed" if len(arms) == 1 else "Every arm failed"),
            lesson=lesson or None,
            error=None if any_ready else "No arm produced a video — see each arm's steps.",
        )
        # A brand-only run has no brief to remember and must not need Vertex
        # for the embed; the team's rating (rate()) still embeds it later.
        # Nor does a run that kept the brief of the version it was built on:
        # remembered, it would be a second copy of that version's lesson
        # (and a pass on top's brief does not describe what it added) — the
        # team's rating still embeds it.
        if generating and kept is None:
            await store.store_memory(exp_id, embed_text)
    except asyncio.CancelledError:
        await store.update_experiment(exp_id, status=store.ST_CANCELLED, stage="Cancelled")
        raise
    except HTTPException as e:
        await store.update_experiment(exp_id, status=store.ST_FAILED, stage="Failed", error=str(e.detail))
    except Exception as e:  # noqa: BLE001
        log.exception("[rebrand] experiment #%d crashed", exp_id)
        await store.update_experiment(exp_id, status=store.ST_FAILED, stage="Failed", error=str(e)[:500])
    finally:
        beat.cancel()


# ---------------------------------------------------------------------------
# Entry points (router)
# ---------------------------------------------------------------------------

# Omni switched off (hrecipe.omni_on): what to use instead, per target.
OMNI_OFF_REEL = ("Omni is switched off. Brand the whole reel with the 42 brand frame instead — "
                 "nothing is regenerated, and it's free.")
OMNI_OFF_CARD = ("Omni is switched off, so the intro and outro cards aren't redrawn any more. "
                 "The 42 brand frame on the whole reel wipes into and out of them, for free.")
OMNI_OFF_SCENE = ("Omni is switched off. Rebrand this scene in the Rebrand studio — it's made in "
                  "house, for free.")
BOOKEND_HOUSE_ONLY = "An intro or outro is made in house, from its own clip."


def omni_off_message(target: str) -> str:
    """Why an Omni run is refused while Omni is off, and what to use
    instead, for this target (reel | intro | outro | scene_N)."""
    if target == "reel":
        return OMNI_OFF_REEL
    if target in ("intro", "outro"):
        return OMNI_OFF_CARD
    return OMNI_OFF_SCENE


async def start(run_id: int, raw_options: dict | None,
                recipe: Optional[hrecipe.Recipe] = None, base: Any = None,
                end_frame: Optional[int] = None, variant: Optional[str] = None,
                seen_ideas: Optional[list] = None, icon_lanes: Optional[list] = None) -> dict:
    """Start an experiment. `recipe` (the Rebrand studio's, already
    normalised) replaces the options: the run follows it through the harness,
    and the legacy option keys it implies are stored beside it.

    `base` (a hbase.BaseSpec or {version_id, mode[, fresh]}; recipes only)
    builds on a finished version of the same clip: "stack" adds the recipe's
    layers on top of it (the recipe holds only those; its opening is the
    harness's; the pattern cap counts the version's own), "rebuild" redoes
    it from the original with the version's take, so what did not change is
    reused — unless `fresh` (a new take). Stored as options.base (and
    options.take), after normalize_options.

    Omni switched off (hrecipe.omni_on, the default): a recipe for Omni is
    refused by hrecipe.runnable, and legacy options asking for Omni (the
    whole reel, a card, a scene) by omni_off_message — a 400 before
    anything is written. The brand frame and a scene made in house start as
    ever.

    `end_frame` (recipes only): the scene end the version is made for, in
    the reel's 30 fps frames (None: automatic), stored as options.end_frame
    beside the clip it is made of (th_reel.version_options). `variant`
    "full": made on the scene kept whole past its automatic cut (the
    studio's full-length variant), stored as options.variant — the run and
    a remake use that clip. `seen_ideas`: the icons seen in Suggested while
    the draft was made, stored as options.seen_ideas (the feedback log).
    `icon_lanes`: each frozen icon's track on the timeline, in the order of
    the recipe's props.resolved (the planner's, icon.lanes_of), stored as
    options.icon_lanes: the version's read-only timeline shows them on the
    tracks its draft had (the render list has none).
    JSON only: recipe_hash does not move."""
    run = await th._fetch_run_row(run_id)
    md = run["metadata"] or {}
    spec = hbase.BaseSpec.of(base)
    bv: Optional[hbase.BaseVersion] = None
    if base is not None and (spec is None or recipe is None):
        raise HTTPException(400, "Only a studio recipe can be built on a version.")
    if recipe is not None:
        if not recipe.target:
            raise HTTPException(400, "A recipe needs its part of the reel (target scene_N, intro or outro).")
        if recipe.target in ("intro", "outro") and (recipe.engine != "house" or spec is not None):
            # Done by hand in the studio: made in house, from its own clip.
            raise HTTPException(400, BOOKEND_HOUSE_ONLY)
        try:
            hrecipe.runnable(recipe)
        except hrecipe.RecipeError as e:
            raise HTTPException(400, str(e)) from e
        chain: list = []
        if spec is not None:
            bv = await hbase.load(spec, run_id, recipe.target)
            if spec.mode == "stack" and hbase.made_in_house(bv.exp):
                if recipe.engine != "house":
                    # Omni would edit the video under its layers.
                    raise HTTPException(409, hbase.OMNI_ON_HOUSE)
                if not bv.passes:
                    # Made in house from the original, a version is its
                    # recipe: changed, it is drawn again from the original
                    # with everything it had — the very same picture as
                    # adding on top, one encode fewer. One made in house ON
                    # TOP of a version Omni made is added to over that
                    # version's video (flattened: base.py, house.flatten).
                    raise HTTPException(409, hbase.ADD_ON_HOUSE)
            if spec.mode == "stack":
                # A layer, or our icons (an icon-only pass on top: no Omni),
                # or — switched off — the version's own icons taken away.
                unstamp = (hrecipe.icon_element(recipe, enabled=False) is not None
                           and hbase.has_icons(bv.exp))
                if not (hrecipe.pattern_elements(recipe) or hrecipe.icons_by_pipeline(recipe) or unstamp):
                    raise HTTPException(400, "Add a layer to put on top of that version.")
                if recipe.director.head != "harness":
                    # The brief describes the first look and would ask for it
                    # again: a pass on top opens with the harness's words.
                    recipe = recipe.model_copy(update={
                        "director": recipe.director.model_copy(update={"head": "harness"})})
                chain = hbase.elements_of(bv.layers)
        # Omni's own icons are kept only on a version that has them, and ours
        # never go on top of those (two sets).
        refused = hbase.icon_refusal(recipe, bv)
        if refused is not None:
            raise HTTPException(refused[0], refused[1])
        # Every pattern, not just the first: an unknown one, a full-screen
        # one beside another, too many — counting the patterns a version
        # being added to already carries.
        problems = await pattern_el.check(recipe, chain)
        if problems:
            raise HTTPException(422, {"message": str(hrecipe.RecipeError(problems)),
                                      "errors": problems})
        implied = hrecipe.options_from_recipe(recipe)
        options = normalize_options(implied)
        # Kept past normalize_options, which rebuilds the dict from the keys
        # it knows (as the recipe is).
        if implied.get("patterns"):
            options["patterns"] = implied["patterns"]
        # Icons stamped by us: Omni draws none and the brief is told (only
        # with an icon element — the 42 default's options are unchanged).
        if implied.get("icons_by_pipeline"):
            options["icons_by_pipeline"] = True
        # The stray-drawing clean-up, likewise only when it is on — and only
        # on a run that makes it: not one that asks Omni for more than the
        # patterns (hguards.cleans_strays), nor a pass on top of a version,
        # where the hold keeps the version as it is.
        if (implied.get("stray_clean") and recipe.engine != "house" and hguards.cleans_strays(recipe)
                and not (spec is not None and spec.mode == "stack")):
            options["stray_clean"] = True
        # Made in house: said on the row (the lineage, as the recipe is).
        if implied.get("engine") == "house":
            options["engine"] = "house"
        options["recipe"] = recipe.model_dump(mode="json")
        # The clip it is made of, the kind of scene, the end it is made for
        # and this server (JSON only: recipe_hash does not move): the reel
        # ships a picked version only while it is of the scene's clip, and
        # a version left running by a restart is told apart at once.
        options.update(await th_reel.version_options(run, recipe.target, end_frame=end_frame))
        if variant == "full" and str(recipe.target).startswith("scene_"):
            options["variant"] = "full"
        if seen_ideas:
            options["seen_ideas"] = [str(g)[:80] for g in seen_ideas][:60]
        if icon_lanes:
            options["icon_lanes"] = [max(0, int(n)) for n in icon_lanes][:hrecipe.ICON_ITEMS_MAX]
        if bv is not None:
            # Lineage, likewise past normalize_options (no column: the table
            # stays as it is).
            options["base"] = spec.public()
            if spec.mode == "rebuild" and not spec.fresh:
                options["take"] = hbase.take_of(bv.exp)
    else:
        options = normalize_options(raw_options)
    # Omni is switched off for new work: no Omni run starts (a scene made in
    # house calls none, and the brand frame never did).
    if options["run_omni"] and options.get("engine") != "house" and not hrecipe.omni_on():
        raise HTTPException(400, omni_off_message(options["target"]))
    if options["target"] == "reel" and md.get("composition_status") != th.COMP_READY:
        raise HTTPException(409, "Finish the video first — the whole-reel Rebrand works on the final stitched reel.")
    if not (options["run_omni"] or options["run_brand"]):
        raise HTTPException(
            400,
            "Pick at least one: the 42 brand frame or Omni."
            if options["target"] == "reel" else "Turn Omni on to rebrand this clip.",
        )
    # A scene made in house needs no Omni provider (it calls none).
    if options["run_omni"] and options.get("engine") != "house" and not omni.providers():
        raise HTTPException(400, omni.NOT_CONFIGURED_MSG)
    if (options["run_omni"] and options["pattern_on"]
            and not await pattern_library.get(options["pattern"])):
        raise HTTPException(400, f"Unknown pattern {options['pattern']!r}: pick one from the list.")
    live = live_ids()
    for e in await store.list_for_run(run_id, limit=5, target=options["target"]):
        e = await store.mark_interrupted_if_orphaned(e, live_ids=live)
        if e.get("status") in store.ACTIVE_STATUSES:
            raise HTTPException(409, f"Rebrand #{e['id']} is still running for this clip.")
    exp = await store.create_experiment(run_id, options)
    exp_id = int(exp["id"])
    task = asyncio.create_task(_run(exp_id, run_id, options), name=f"rebrand_{exp_id}")
    _TASKS[exp_id] = task
    task.add_done_callback(lambda _t, _id=exp_id: _TASKS.pop(_id, None))
    log.info("[rebrand] experiment #%d started for TH v2 run #%d (%s)", exp_id, run_id, options)
    return exp


async def cancel(exp_id: int) -> dict:
    exp = await store.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, f"rebrand #{exp_id} not found")
    task = _TASKS.get(exp_id)
    if task and not task.done():
        task.cancel()
    elif exp.get("status") in store.ACTIVE_STATUSES:
        await store.update_experiment(exp_id, status=store.ST_CANCELLED, stage="Cancelled")
    return await store.get_experiment(exp_id) or exp


async def rate(exp_id: int, *, winner: Optional[str], omni_stars: Optional[int],
               veo_stars: Optional[int], notes: str,
               brand_stars: Optional[int] = None) -> dict:
    """The human half of the feedback loop: stars + notes are folded into the
    experiment's lesson and re-embedded, so the very next brief sees them.
    A version made in house is only rated: it had no brief to learn from,
    so nothing is embedded ($0). With Omni switched off (hrecipe.omni_on)
    no brief is written any more, so an older run's rating and lesson are
    saved and nothing is embedded either ($0)."""
    exp = await store.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, f"rebrand #{exp_id} not found")
    if winner not in (None, *store.ALL_ARMS, "neither"):
        raise HTTPException(400, f"winner must be {' | '.join(store.ALL_ARMS)} | neither")

    def _stars(v: Optional[int]) -> Optional[int]:
        return max(1, min(5, int(v))) if v else None

    rating = {
        "omni_stars": _stars(omni_stars),
        "veo_stars": _stars(veo_stars),
        "brand_stars": _stars(brand_stars),
        "notes": (notes or "").strip()[:1000],
        "rated_at": _now(),
    }
    if hbase.made_in_house(exp):
        await store.update_experiment(exp_id, rating=rating, winner=winner)
        return await store.get_experiment(exp_id) or exp
    run_ctx: dict = {}
    try:
        run = await th._fetch_run_row(int(exp["run_id"]))
        md = run["metadata"] or {}
        script = md.get("script") if isinstance(md.get("script"), dict) else {}
        run_ctx = {"topic": run.get("topic") or "", "hook": script.get("hook") or "",
                   "target": (exp.get("options") or {}).get("target") or "reel"}
    except Exception:  # noqa: BLE001 — the run may have been deleted; the rating still counts
        pass
    embed_text, lesson = rebrand_brief.summarize_for_memory(
        run_ctx, exp.get("brief") or {}, exp.get("arms") or {}, rating,
    )
    await store.update_experiment(exp_id, rating=rating, winner=winner, lesson=lesson or None)
    if hrecipe.omni_on():
        await store.store_memory(exp_id, embed_text)
    return await store.get_experiment(exp_id) or exp


def public(exp: dict) -> dict:
    """API shape: file paths → URLs, timestamps → ISO."""
    base = f"/api/rebrand/experiments/{exp['id']}"
    arms_out: dict[str, Any] = {}
    stored = exp.get("arms") or {}
    # Current arms always; a retired arm (Veo) only where an old run has it.
    for a in (*store.ARMS, *(x for x in store.LEGACY_ARMS if x in stored)):
        arm = dict(stored.get(a) or {"status": store.ARM_SKIPPED})
        ready = arm.get("status") == store.ARM_READY and (arm.get("final_path") or arm.get("storage_path"))
        arm["url"] = f"{base}/file/{a}/final" if ready else None
        arm["download_url"] = f"{base}/file/{a}/final?download=1" if ready else None
        arm["parts"] = {k: f"{base}/file/{a}/{k}" for k in (arm.get("parts") or {})}
        for k in ("final_path", "storage_bucket", "storage_path"):
            arm.pop(k, None)
        arms_out[a] = arm

    def _iso(v: Any) -> Any:
        return v.isoformat() if hasattr(v, "isoformat") else v

    return {
        "id": exp["id"],
        "run_id": exp["run_id"],
        "target": (exp.get("options") or {}).get("target") or "reel",
        "status": exp.get("status"),
        "stage": exp.get("stage"),
        # The version it was built on ({version_id, mode}), or None.
        "base": store.base_of(exp),
        "options": exp.get("options") or {},
        "brief": exp.get("brief"),
        "evidence": exp.get("evidence"),
        "arms": arms_out,
        "winner": exp.get("winner"),
        "rating": exp.get("rating"),
        "lesson": exp.get("lesson"),
        "error": exp.get("error"),
        "cost_usd": float(exp.get("cost_usd") or 0.0),
        "created_at": _iso(exp.get("created_at")),
        "updated_at": _iso(exp.get("updated_at")),
    }
