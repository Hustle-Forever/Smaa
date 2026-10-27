"""General Trends API — what the whole internet is doing right now.

    GET    /api/trends/general/stats              header numbers + next auto refresh
    GET    /api/trends/general/sources            configured sources + last-run health
    POST   /api/trends/general/refresh            queue a refresh (409 if one is running)
    GET    /api/trends/general/runs/latest        poll target

    GET    /api/trends/general/items              filters + sort + weight sliders
    GET    /api/trends/general/items/{id}         one trend + EVERY sighting
    POST   /api/trends/general/items/rescore      {ids?}
    PATCH  /api/trends/general/items/{id}         {status}  save / dismiss / used
    GET    /api/trends/general/items/{id}/thumb   local thumbnail
    POST   /api/trends/general/items/{id}/brainstorm  {feedback?, exclude?} 3 ideas
                                                      (+ what the trend really is)
    POST   /api/trends/general/items/{id}/ideas   {tool, idea?}  AI prefill for a tool

    GET    /api/trends/general/themes             scored trends grouped into buckets

Sits next to the competitor Trend Finder (``/api/trends``) and shares its
conventions: plain-language 4xx for setup problems (a scoring outage is a 422,
not a 500), one refresh at a time (409), and ``IdeationUnavailable`` mapped
BEFORE the generic ``RuntimeError`` → 502.
"""
from __future__ import annotations

import mimetypes

import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import config
from ..services import general_trend_ideation as ideation
from ..services import general_trends_pipeline as pipeline
from ..services import general_trends_ranking as ranking
from ..services import general_trends_sources as gts
from ..services import general_trends_store as store
from ..services.storage import resolve_local

router = APIRouter(prefix="/api/trends/general", tags=["trends"])
log = logging.getLogger("archivist.router.general_trends")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class PatchTrendRequest(BaseModel):
    status: str


class RescoreRequest(BaseModel):
    ids: Optional[list[int]] = None


class TrendIdeasRequest(BaseModel):
    tool: str
    # The idea the team picked in the brainstorm step, as returned by
    # /brainstorm. Optional and untyped on purpose: it is additive (callers that
    # predate the picker still work) and the service clamps every field anyway.
    idea: Optional[dict] = None


class TrendBrainstormRequest(BaseModel):
    feedback: Optional[str] = None      # "none of these — more like X"
    exclude: Optional[list[str]] = None  # titles already shown, don't repeat them


# ---------------------------------------------------------------------------
# Stats + sources + runs
# ---------------------------------------------------------------------------

@router.get("/stats")
async def general_stats() -> dict:
    """Header numbers plus when the poller will next refresh on its own."""
    out = await store.stats()
    hours = int(config.general_trends_refresh_hours or 0)
    out["refresh_every_hours"] = hours
    next_at = None
    if hours > 0:
        latest = await store.latest_run()
        finished = (latest or {}).get("finished_at")
        if finished is not None:
            next_at = finished + timedelta(hours=hours)
    out["next_auto_refresh_at"] = next_at
    return out


def _source_items(plan: list[dict], latest: Optional[dict]) -> list[dict]:
    """Merge the configured plan with the latest run's per-source stats."""
    meta = (latest or {}).get("metadata") or {}
    stats = meta.get("sources") if isinstance(meta, dict) else None
    stats = stats if isinstance(stats, dict) else {}
    items: list[dict] = []
    for sid in gts.SOURCE_IDS:
        units = [u for u in plan if u.get("source") == sid]
        regions: list[str] = []
        for u in units:
            r = str(u.get("region") or "")
            if r and r not in regions:
                regions.append(r)
        st = stats.get(sid) if isinstance(stats.get(sid), dict) else None
        # `dropped` / `dropped_by` come from the ingest screen (safety +
        # freshness) that runs inside fetch_all. Surfacing them here is what
        # lets the owner tell "this source is broken" from "this source is
        # noisy and we filtered it" without reading the backend log.
        by = st.get("dropped_by") if st is not None else None
        items.append({
            "id": sid,
            "label": gts.SOURCE_LABELS.get(sid, sid),
            "enabled": bool(units),
            "regions": regions,
            "ok": (bool(st.get("ok")) if st is not None else None),
            "count": (st.get("count") if st is not None else None),
            "error": (st.get("error") if st is not None else None),
            "dropped": (st.get("dropped") if st is not None else None),
            "dropped_by": (by if isinstance(by, dict) else None),
        })
    return items


@router.get("/sources")
async def general_sources() -> dict:
    """Every known source: enabled (in the config plan) + how it did last run.
    ``geos`` echoes GENERAL_TRENDS_GEOS so the Region filter can be built."""
    plan = pipeline._fetch_plan()
    latest = await store.latest_run()
    return {"items": _source_items(plan, latest),
            "geos": [str(g).upper() for g in config.general_trends_geos]}


@router.post("/refresh")
async def general_refresh() -> dict:
    """Queue a refresh. Returns immediately — the poller runs it, so it survives
    a backend restart. Poll /runs/latest.

    423 while collection is paused: a configuration state an operator can act on
    (flip GENERAL_TRENDS_ENABLED), not a transient one. Distinct from the 409 a
    busy refresh returns, so a client can tell "wait" from "this is switched
    off" without reading the sentence."""
    try:
        run = await pipeline.start_refresh(trigger="manual")
    except pipeline.RefreshPaused as exc:
        # Neither class inherits the other (see the pipeline), so unlike the
        # IdeationUnavailable/RuntimeError pairs elsewhere in this codebase the
        # clause order here is free rather than load-bearing.
        raise HTTPException(423, str(exc)) from exc
    except pipeline.RefreshBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"run": run}


@router.get("/runs/latest")
async def general_latest_run() -> dict:
    return {"run": await store.latest_run()}


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

_EVIDENCE_KEYS = ("source", "region", "title", "url", "metric_value", "metric_label", "seen_at")


def _window(requested: int, configured: int) -> Optional[int]:
    """Resolve a day window: an explicit request wins, 0 means "the configured
    default", and a configured 0 means "no window at all" (``None``) rather
    than the zero-day window that would empty the feed."""
    days = int(requested or configured or 0)
    return days if days > 0 else None


@router.get("/items")
async def general_items(
    sort: str = Query(default="opportunity"),
    status: Optional[str] = None,
    region: Optional[str] = None,
    source: Optional[str] = None,
    theme: Optional[str] = None,
    kind: Optional[str] = None,
    min_fit: Optional[int] = Query(default=None, ge=0, le=100),
    q: Optional[str] = None,
    include_sensitive: bool = False,
    include_filtered: bool = False,
    all_kinds: bool = False,
    days: int = Query(default=0, ge=0, le=60),
    content_days: int = Query(default=0, ge=0, le=90),
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    w_vel: Optional[float] = Query(default=None, ge=0, le=1),
    w_fit: Optional[float] = Query(default=None, ge=0, le=1),
    w_uae: Optional[float] = Query(default=None, ge=0, le=1),
) -> dict:
    """The ranked feed. The three weights re-blend the opportunity score on the
    fly; evidence (up to 6 sightings) is attached for the returned page only.

    Two windows, two different questions — see ``store.query_trends``:
    ``days`` bounds ``last_seen_at`` ("is a source still listing it") and
    ``content_days`` bounds ``latest_content_at`` ("is anything new actually
    happening"). A trend first seen three weeks ago that is still collecting
    fresh sightings must survive, so ``first_seen_at`` gates nothing. Both take
    0 to mean "use the configured default" — the widener in the UI passes a
    real number, everything else passes nothing.
    """
    if sort not in ranking.SORTS:
        raise HTTPException(422, f"sort must be one of {', '.join(ranking.SORTS)}")
    if status is not None and status not in store.TREND_STATUSES:
        raise HTTPException(
            422, f"status must be one of {', '.join(sorted(store.TREND_STATUSES))}"
        )
    if kind is not None and kind not in store.TREND_KINDS:
        raise HTTPException(
            422, f"kind must be one of {', '.join(sorted(store.TREND_KINDS))}"
        )
    rows = await store.query_trends(
        status=status, region=region, source=source, theme=theme, kind=kind,
        min_fit=min_fit, q=q, include_sensitive=include_sensitive,
        include_filtered=include_filtered,
        # The participation gate. An explicit `kind` filter wins over it (the
        # user asked for that one kind); `all_kinds` turns it off entirely so
        # the news and event rows can still be inspected on demand.
        kinds=(None if (kind or all_kinds) else config.general_trends_kinds),
        include_unclassified=all_kinds,
        since_days=_window(days, config.general_trends_feed_days),
        content_days=_window(content_days, config.general_trends_max_content_age_days),
    )
    weights = {"velocity": w_vel, "fit": w_fit, "uae": w_uae}
    ranked = ranking.rank_trends(rows, sort=sort, weights=weights)
    page = ranked[offset:offset + limit]
    evidence = await store.sightings_for([int(t["id"]) for t in page], per_trend=6)
    for t in page:
        t["evidence"] = [
            {k: s.get(k) for k in _EVIDENCE_KEYS}
            for s in evidence.get(int(t["id"]), [])
        ]
        t["heat"] = ranking.heat_band(t.get("velocity"))
    return {
        "items": page,
        "total": len(ranked),
        "weights": ranking.normalise_weights(weights),
    }


# The list endpoint attaches 6 sightings per card — enough for a preview strip,
# and deliberately cheap. The detail view is the opposite trade: it is one row
# and the whole point of opening it is to see where the trend was actually
# spotted, so it gets every distinct sighting the store holds (bounded, because
# a long-lived trend on a chatty source is unbounded otherwise).
DETAIL_EVIDENCE_MAX = 200

# The full sighting, minus the three columns nobody outside the pipeline can
# use: `trend_id` (already known), `run_id` (an internal id) and `extra` (raw
# per-source jsonb).
_DETAIL_EVIDENCE_KEYS = (
    "id", "source", "source_key", "region", "category", "title", "url",
    "image_url", "summary", "metric_value", "metric_label", "published_at",
    "seen_at",
)


@router.get("/items/{trend_id}")
async def general_item_detail(trend_id: int) -> dict:
    """One trend, everything known about it.

    Three things the card cannot show: the whole trend row, EVERY sighting
    (the list endpoint stops at 6), and the cached research — what a live
    Google-grounded lookup found the trend actually is, if anyone has already
    paid for one on this trend.

    `research` is READ-ONLY here. Opening a detail view must never start a
    model call: the lookup happens when the owner presses Brainstorm / Make
    this, and this endpoint only reports whether that answer already exists.
    """
    trend = await store.get_trend(trend_id)
    if not trend:
        raise HTTPException(404, "trend not found")
    tid = int(trend["id"])
    trend["heat"] = ranking.heat_band(trend.get("velocity"))
    sightings = await store.sightings_for([tid], per_trend=DETAIL_EVIDENCE_MAX)
    evidence = [
        {k: s.get(k) for k in _DETAIL_EVIDENCE_KEYS}
        for s in sightings.get(tid, [])
    ]
    try:
        research = ideation.cached_research(tid, trend)
    except Exception:   # noqa: BLE001 — a cache read never fails a page view
        log.warning("[general-trends] cached research unreadable for #%s", tid,
                    exc_info=True)
        research = None
    return {"trend": trend, "evidence": evidence, "research": research}


@router.post("/items/rescore")
async def general_rescore(body: Optional[RescoreRequest] = None) -> dict:
    """Re-run the AI pass — e.g. after the brand profile changed."""
    ids = body.ids if body else None
    try:
        scored = await pipeline.rescore(ids)
    except pipeline.ScoringUnavailable as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"scored": scored}


@router.patch("/items/{trend_id}")
async def general_patch_item(trend_id: int, body: PatchTrendRequest) -> dict:
    if body.status not in store.TREND_STATUSES:
        raise HTTPException(
            422, f"status must be one of {', '.join(sorted(store.TREND_STATUSES))}"
        )
    trend = await store.update_status(trend_id, body.status)
    if not trend:
        raise HTTPException(404, "trend not found")
    return {"trend": trend}


@router.get("/items/{trend_id}/thumb")
async def general_item_thumb(trend_id: int):
    """Serve the locally-cached thumbnail; source image URLs go stale."""
    trend = await store.get_trend(trend_id)
    if not trend:
        raise HTTPException(404, "trend not found")
    rel = trend.get("local_thumb_path")
    if not rel:
        raise HTTPException(404, "no cached thumbnail for this trend")
    path = resolve_local(str(rel))
    if not path.is_file():
        raise HTTPException(410, "the cached thumbnail is gone — refresh to fetch it again")
    # The FILE's type, never a constant: see `_thumb_media_type` in
    # routers/trend_finder.py for the bug that taught us the difference.
    kind, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path, media_type=kind if (kind or "").startswith("image/") else "image/jpeg",
    )


@router.post("/items/{trend_id}/brainstorm")
async def general_item_brainstorm(
    trend_id: int, body: Optional[TrendBrainstormRequest] = None,
) -> dict:
    """Three different ideas for how 42 could do this trend, before any tool is
    chosen. Re-post with `feedback` (and the titles already seen in `exclude`)
    to get three more. The body is optional so the first round is a bare POST.

    The reply also carries `research`: what a live Google-grounded lookup found
    the trend ACTUALLY is, which is what the ideas were built from. It is
    additive and can be empty — a failed lookup degrades to ideas-without-it —
    so a client that ignores the field behaves exactly as it did before."""
    try:
        out = await ideation.brainstorm_ideas(
            trend_id=trend_id,
            feedback=(body.feedback or "") if body else "",
            exclude=(body.exclude or []) if body else [],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, "trend not found") from exc
    except ideation.IdeationUnavailable as exc:   # MUST precede RuntimeError
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return out


@router.post("/items/{trend_id}/ideas")
async def general_item_ideas(trend_id: int, body: TrendIdeasRequest) -> dict:
    """AI prefill for one Producer tool, generated from this trend — the same
    TrendPrefill envelope as the competitor feed (post_id = trend id). With an
    `idea` from /brainstorm the brief executes THAT idea."""
    try:
        prefill = await ideation.generate_prefill(trend_id=trend_id, tool=body.tool,
                                                  idea=body.idea)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, "trend not found") from exc
    except ideation.IdeationUnavailable as exc:   # MUST precede RuntimeError
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"prefill": prefill}


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

@router.get("/themes")
async def general_themes() -> dict:
    return {"items": await store.theme_rollup()}
