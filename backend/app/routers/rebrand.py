"""Rebrand experiment API — the 42 brand frame, Omni and Veo on a finished
Talking Head v2 reel.

  GET  /api/rebrand/th-v2/{run_id}?target=…         capabilities + experiments for one target
  POST /api/rebrand/th-v2/{run_id}                  start one (body.target: reel | intro |
                                                    outro | scene_<N>; default reel)
  GET  /api/rebrand/experiments/{exp_id}            one experiment (the UI polls this)
  POST /api/rebrand/experiments/{exp_id}/cancel     stop a running experiment
  POST /api/rebrand/experiments/{exp_id}/rate       stars + notes + winner (feeds the RAG loop)
  GET  /api/rebrand/experiments/{exp_id}/file/{arm}/{part}   stream an output MP4

  POST /api/rebrand/th-v2/{run_id}/seam               design + render the intro->main seam
  GET  /api/rebrand/th-v2/{run_id}/seam/file/{part}   stream it (part: clip | reel | still_<N>)
  POST /api/rebrand/seams/{recipe_id}/rate            stars + notes on one transition

  GET    /api/rebrand/patterns                       every pattern Omni can use + the entrances
  POST   /api/rebrand/patterns                       upload one (analysed by AI and embedded)
  DELETE /api/rebrand/patterns/{slug}                remove an uploaded one
  GET    /api/rebrand/patterns/{slug}/preview        its picker thumbnail (PNG)

The work itself lives in services/rebrand_pipeline.py, and the seam in
services/rebrand_transition.py.

Omni is switched off for new work (REBRAND_OMNI, off unless set to on): a
start that asks for Omni — the whole reel's experiment, an intro or outro
card, a scene from the classic panel — is a 400 saying what to use instead,
and `capabilities.omni.switched_off` tells the panel to hide it. The 42 brand
frame runs as ever, and every experiment Omni made still lists, plays, rates
and downloads.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..services import asset_storage
from ..services import omni_client
from ..services import pattern_library
from ..services import rebrand_pipeline as pipeline
from ..services import rebrand_store as store
from ..services import rebrand_transition as seam
from ..services import yas_talking_head_v2 as th
from ..services.rebrand_harness import recipe as hrecipe

router = APIRouter(prefix="/api/rebrand", tags=["rebrand"])

# Outputs are unique per (experiment, arm, part) and never rewritten.
_FILE_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable"}


class RebrandStartRequest(BaseModel):
    target: str = "reel"                    # reel | intro | outro | scene_<N>
    run_omni: bool = True
    # Veo is retired from Rebrand: run_veo / veo_* from older callers are
    # ignored (not fields any more; pydantic drops unknown keys).
    run_brand: bool = False                 # the 42 brand frame (whole reel only)
    intensity: str = "balanced"             # subtle | balanced | bold
    notes: str = Field(default="", max_length=600)
    scene_transitions: bool = False
    transition_sec: float = 1.6
    brand_transitions: str = "auto"         # auto | cover | handoff | short | glitch
    brand_heading: Optional[str] = Field(default=None, max_length=40)   # None = auto, "" = none
    brand_sub: Optional[str] = Field(default=None, max_length=140)      # None = auto, "" = none
    brand_logo_on_cards: bool = False
    brand_resolution: str = "1080"          # 1080 | 720
    brand_icons: bool = True                # topic icons from each scene's script
    brand_edge_patterns: bool = True        # pattern over the picture's left edge
    # The pattern Omni lays behind the speaker (services/pattern_library).
    pattern_on: bool = True
    pattern: str = Field(default=pattern_library.DEFAULT_SLUG, max_length=64)
    pattern_animation: Optional[str] = None  # None = the pattern's own entrance


class SeamRequest(BaseModel):
    """Everything the seam designer will accept. The defaults are the design —
    a caller that sends `{}` gets the intended transition."""
    notes: str = Field(default="", max_length=600)
    pre_sec: float = Field(default=0.8, ge=0.2, le=2.0)
    post_sec: float = Field(default=2.2, ge=0.4, le=3.5)
    # "shot" runs the overlay across the whole talking head so the branded
    # panel can arrive, hold and then leave; "seam" covers only the join and
    # post_sec applies.
    cover: str = Field(default="shot", pattern="^(shot|seam)$")
    side: str = Field(default="auto", pattern="^(auto|left|right)$")
    hold_sec: float = Field(default=3.5, ge=0.5, le=20.0)
    build_reel: bool = True
    critique: bool = True
    debug_safe_zones: bool = False
    # A recipe edited by hand in the UI, re-rendered as-is (still subject to
    # the safe-zone invariants — those are not negotiable from the client).
    recipe: Optional[dict] = None


class SeamRateRequest(BaseModel):
    stars: Optional[int] = Field(default=None, ge=1, le=5)
    notes: str = Field(default="", max_length=1000)


class RebrandRateRequest(BaseModel):
    winner: Optional[str] = None            # omni | veo | brand | neither
    omni_stars: Optional[int] = Field(default=None, ge=1, le=5)
    veo_stars: Optional[int] = Field(default=None, ge=1, le=5)
    brand_stars: Optional[int] = Field(default=None, ge=1, le=5)
    notes: str = Field(default="", max_length=1000)


def _capabilities() -> dict:
    """What the panel may offer. `omni.switched_off`: Omni makes nothing new
    (rebrand_harness.recipe.omni_on) — the panel hides it and says why."""
    return {
        "omni": {**omni_client.status(), "switched_off": not hrecipe.omni_on()},
    }


@router.get("/th-v2/{run_id}")
async def list_th_v2(run_id: int, target: str = "reel") -> dict:
    await th._fetch_run_row(run_id)  # 404 for an unknown run
    live = pipeline.live_ids()
    items = []
    for e in await store.list_for_run(run_id, target=target):
        e = await store.mark_interrupted_if_orphaned(e, live_ids=live)
        items.append(pipeline.public(e))
    return {"capabilities": _capabilities(), "experiments": items}


@router.post("/th-v2/{run_id}")
async def start_th_v2(run_id: int, body: RebrandStartRequest) -> dict:
    exp = await pipeline.start(run_id, body.model_dump())
    return pipeline.public(exp)


async def _get_or_404(exp_id: int) -> dict:
    exp = await store.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, f"rebrand #{exp_id} not found")
    return exp


@router.get("/experiments/{exp_id}")
async def get_experiment(exp_id: int) -> dict:
    exp = await _get_or_404(exp_id)
    exp = await store.mark_interrupted_if_orphaned(exp, live_ids=pipeline.live_ids())
    return pipeline.public(exp)


@router.post("/experiments/{exp_id}/cancel")
async def cancel_experiment(exp_id: int) -> dict:
    return pipeline.public(await pipeline.cancel(exp_id))


@router.post("/experiments/{exp_id}/rate")
async def rate_experiment(exp_id: int, body: RebrandRateRequest) -> dict:
    exp = await pipeline.rate(
        exp_id, winner=body.winner, omni_stars=body.omni_stars,
        veo_stars=body.veo_stars, brand_stars=body.brand_stars, notes=body.notes,
    )
    return pipeline.public(exp)


@router.post("/th-v2/{run_id}/seam")
async def design_seam(run_id: int, body: SeamRequest) -> dict:
    """Design and render the intro->main transition for a finished reel.

    Synchronous on purpose: it is a Remotion render of about three seconds
    plus two small Gemini calls, not a video generation, so there is nothing
    worth polling."""
    result = await seam.design_seam(
        run_id,
        notes=body.notes,
        pre_sec=body.pre_sec,
        post_sec=body.post_sec,
        cover=body.cover,
        side=body.side,
        hold_sec=body.hold_sec,
        recipe_override=body.recipe,
        build_reel=body.build_reel,
        do_critique=body.critique,
        debug_safe_zones=body.debug_safe_zones,
    )
    return result.public()


@router.get("/th-v2/{run_id}/seam/file/{part}")
async def seam_file(run_id: int, part: str, download: bool = False):
    """Stream the transition on its own, or the reel rebuilt around it."""
    work = seam.WORK_ROOT / str(run_id) / "latest"
    if part == "clip":
        path = work / "transition.mp4"
    elif part == "reel":
        path = work / "seam_preview.mp4"
    elif part.startswith("still_") and part[6:].isdigit():
        path = work / "stills" / f"{part}.jpg"
    else:
        raise HTTPException(400, "part must be clip | reel | still_<N>")
    if not path.is_file():
        raise HTTPException(404, f"no '{part}' for run {run_id} — design the seam first")
    media_type = "video/mp4" if path.suffix == ".mp4" else "image/jpeg"
    headers = {"Cache-Control": "private, max-age=60"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="seam-{run_id}-{part}{path.suffix}"'
    return FileResponse(str(path), media_type=media_type, headers=headers)


@router.post("/seams/{recipe_id}/rate")
async def rate_seam(recipe_id: int, body: SeamRateRequest) -> dict:
    """Rate one transition. The stars go straight into retrieval, so the next
    seam at a similar cut is designed knowing this one landed or did not."""
    return await seam.rate(recipe_id, stars=body.stars, notes=body.notes)


@router.get("/experiments/{exp_id}/file/{arm}/{part}")
async def experiment_file(exp_id: int, arm: str, part: str, download: bool = False):
    if arm not in store.ALL_ARMS:
        raise HTTPException(400, f"arm must be {' | '.join(store.ALL_ARMS)}")
    exp = await _get_or_404(exp_id)
    state = (exp.get("arms") or {}).get(arm) or {}
    if part == "final":
        rel = state.get("final_path")
    else:
        rel = (state.get("parts") or {}).get(part)
    if not rel and not (part == "final" and state.get("storage_path")):
        raise HTTPException(404, f"{arm} has no '{part}' output")
    name = f"rebrand-{exp_id}-{arm}-{part}.mp4"
    headers = dict(_FILE_HEADERS)
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    if rel:
        p = th._abs_from_rel(rel)
        if p.exists():
            return FileResponse(str(p), media_type="video/mp4", headers=headers)
    # Local copy gone (redeploy) — the final cut was backed up to the bucket.
    if part == "final" and state.get("storage_bucket") and state.get("storage_path"):
        # The download name is signed in by url_for: on S3 every query
        # parameter is part of the signature, so an appended one is a 403.
        signed = await asset_storage.url_for(
            storage_bucket=state["storage_bucket"], storage_path=state["storage_path"],
            local_path=None, expires=3600, download_name=name if download else None,
        )
        if signed:
            return RedirectResponse(signed, status_code=307)
    raise HTTPException(410, f"{arm} '{part}' file is gone (only the final cut is backed up)")


# ---------------------------------------------------------------------------
# Pattern library
# ---------------------------------------------------------------------------

@router.get("/patterns")
async def list_patterns() -> dict:
    return {
        "patterns": [p.public() for p in await pattern_library.list_patterns()],
        "animations": pattern_library.animations_public(),
        "default_pattern": pattern_library.DEFAULT_SLUG,
    }


@router.post("/patterns", status_code=201)
async def upload_pattern(file: UploadFile = File(...), name: Optional[str] = Form(default=None)) -> dict:
    data = await file.read(pattern_library.MAX_UPLOAD_BYTES + 1)
    if len(data) > pattern_library.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"The file is larger than {pattern_library.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    clean = " ".join(str(name or "").split())[:60] or None
    try:
        entry = await pattern_library.add_upload(data, file.filename or "pattern.png", clean)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return entry.public()


@router.delete("/patterns/{slug}")
async def delete_pattern(slug: str) -> dict:
    if slug in pattern_library.BUILTINS:
        raise HTTPException(403, "The built-in patterns can't be removed.")
    if not await pattern_library.delete_upload(slug):
        raise HTTPException(404, "No such pattern.")
    return {"ok": True}


@router.get("/patterns/{slug}/preview")
async def pattern_preview(slug: str):
    entry = await pattern_library.get(slug)
    if entry is None:
        raise HTTPException(404, "No such pattern.")
    import asyncio

    path = await asyncio.to_thread(pattern_library.preview, entry)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})
