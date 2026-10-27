"""Trend Finder API — competitors, discovered posts, themes and scans.

    GET    /api/trends/health                  is Instagram usable, and as whom
    GET    /api/trends/stats                   header numbers

    GET    /api/trends/competitors             ?status=&region=
    POST   /api/trends/competitors             {username,…}  validates first
    PATCH  /api/trends/competitors/{id}        {status,…}    exclude / re-include
    DELETE /api/trends/competitors/{id}                      cascades its posts
    POST   /api/trends/competitors/research    {hint?,limit?,geo?,relevance?,engagement?,size?}
    POST   /api/trends/competitors/{id}/scan

    POST   /api/trends/scan                    {competitor_ids?,lookback_days?}
    GET    /api/trends/scan/latest             poll target

    GET    /api/trends/posts                   filters + sort + weight sliders
                                               (?source=competitor|news selects the
                                                category; omit it for all)
    GET    /api/trends/posts/{id}              one post, everything stored on it
    PATCH  /api/trends/posts/{id}              {status}  save / dismiss / used
    GET    /api/trends/posts/{id}/thumb        local thumbnail (CDN URLs expire)
    POST   /api/trends/posts/rescore           {post_ids?}
    POST   /api/trends/posts/{id}/brainstorm   {feedback?, exclude?}  3 ideas first
    POST   /api/trends/posts/{id}/ideas        {tool, idea?}  AI prefill for a Producer tool

    GET    /api/trends/themes                  topics grouped into buckets

    GET    /api/trends/news/health             did it run, what did it find
    GET    /api/trends/news/config             the brief, the count, the switch
    PUT    /api/trends/news/config             {brief?,stories_per_day?,enabled?}
    POST   /api/trends/news/brief/suggest      {notes}  three briefs to pick from
    POST   /api/trends/news/fetch              {limit?}  search now (the only way)
    POST   /api/trends/news/cancel             stop it, and clear a stranded run

Everything degrades cleanly when Instagram isn't configured: endpoints return a
plain-language 422 rather than a 500, and the UI shows a setup banner.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import config
from ..services import instagram_client as ig
from ..services import news_search, news_store
from ..services import trend_finder, trend_ideation, trend_research, trend_scoring
from ..services import trend_store as store
from ..services.storage import resolve_local

router = APIRouter(prefix="/api/trends", tags=["trends"])
log = logging.getLogger("archivist.router.trend_finder")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class AddCompetitorRequest(BaseModel):
    username: str
    category: Optional[str] = None
    region: Optional[str] = None
    why: Optional[str] = None
    # Set by the "Ignore" action on an AI suggestion: remember the handle so it
    # is never proposed again, without ever scanning it.
    exclude: bool = False


class PatchCompetitorRequest(BaseModel):
    status: Optional[str] = None
    category: Optional[str] = None
    region: Optional[str] = None
    why: Optional[str] = None


class ResearchRequest(BaseModel):
    hint: Optional[str] = None
    limit: int = Field(default=8, ge=1, le=12)
    # Focus dials. Each defaults to None = "leave it as it was" — an older
    # frontend that sends neither keeps the previous UAE-weighted behaviour.
    geo: Optional[str] = None
    relevance: Optional[str] = None
    engagement: Optional[str] = None
    size: Optional[str] = None


class ScanRequest(BaseModel):
    competitor_ids: Optional[list[int]] = None
    lookback_days: int = Field(default=trend_finder.DEFAULT_LOOKBACK_DAYS, ge=7, le=365)
    # The most posts to take from each account. A cap the person sets on the
    # button, not a target: an account with fewer recent posts returns fewer,
    # and that is the right answer rather than a shortfall.
    max_posts: int = Field(
        default=trend_finder.DEFAULT_MAX_POSTS_PER_ACCOUNT, ge=5, le=200,
    )


class PatchPostRequest(BaseModel):
    status: str


class RescoreRequest(BaseModel):
    post_ids: Optional[list[int]] = None


class PostIdeasRequest(BaseModel):
    tool: str
    # The idea the team picked in the brainstorm step, as returned by
    # /brainstorm. Optional and untyped on purpose: it is additive (callers that
    # predate the picker still work) and the service clamps every field anyway.
    idea: Optional[dict] = None


class PostBrainstormRequest(BaseModel):
    feedback: Optional[str] = None       # "none of these — more like X"
    exclude: Optional[list[str]] = None  # titles already shown, don't repeat them


class NewsFetchRequest(BaseModel):
    """What the Find-stories button sends: the most stories to keep, and nothing
    else. Bounded here as well as in the worker, because a route is reachable
    without the screen."""

    limit: Optional[int] = Field(
        default=None, ge=news_store.MIN_STORIES, le=news_store.MAX_STORIES,
    )


class NewsConfigRequest(BaseModel):
    """The whole configuration surface: what to look for, how much, on or off.

    Every field optional and the handler forwards only what was sent, so a
    screen that toggled the switch does not resend the brief and clobber an edit
    made in another tab.
    """

    brief: Optional[str] = None
    stories_per_day: Optional[int] = None
    enabled: Optional[bool] = None


class BriefSuggestRequest(BaseModel):
    """A rough note in the owner's own words, turned into briefs to pick from."""

    notes: Optional[str] = None


class WatchAddRequest(BaseModel):
    """One thing to follow: a company, a person, a subject or a website.

    A single field on purpose. Which of those four it is gets worked out from
    the text (`news_store.clean_watch`), because the person typing "Anthropic"
    or pasting a link does not think of that as a question to answer.
    """

    value: Optional[str] = None
    # Which box it was typed into: "name" or "site". Optional, and the text
    # still decides what it is; the box only lets the server refuse a name
    # typed where a website was asked for (`news_store.resolve_watch`).
    kind: Optional[str] = None


# ---------------------------------------------------------------------------
# Health + stats
# ---------------------------------------------------------------------------

@router.get("/health")
async def trends_health() -> dict:
    """Whether competitor scanning can work at all, and which account's token
    is being used. The UI shows a setup banner when `ok` is false."""
    try:
        info = await ig.ping()
        return {"ok": True, "account": info.get("username"),
                "account_id": info.get("id"), "name": info.get("name")}
    except ig.InstagramConfigError as exc:
        return {"ok": False, "error": str(exc), "reason": "not_configured"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "reason": "unreachable"}


@router.get("/stats")
async def trends_stats() -> dict:
    return await store.stats()


# ---------------------------------------------------------------------------
# Competitors
# ---------------------------------------------------------------------------

@router.get("/competitors")
async def list_competitors(
    status: Optional[str] = None, region: Optional[str] = None,
) -> dict:
    return {"items": await store.list_competitors(status=status, region=region)}


@router.post("/competitors")
async def add_competitor(body: AddCompetitorRequest) -> dict:
    """Add a handle to watch.

    An "ignore" (exclude=true) is recorded WITHOUT calling Instagram — the point
    is to remember a rejection, and the handle may well be unreachable anyway."""
    try:
        handle = store.validate_username(body.username)
    except store.InvalidHandleError as exc:
        raise HTTPException(422, str(exc)) from exc

    if body.exclude:
        comp = await store.upsert_competitor(
            username=handle, category=body.category,
            region=body.region or "uae", why=body.why,
            status="excluded", source="ai_research",
        )
        # upsert never changes status on conflict, so set it explicitly.
        comp = await store.update_competitor(int(comp["id"]), status="excluded") or comp
        return {"competitor": comp}

    existing = await store.get_competitor_by_username(handle)
    if existing and existing["status"] == "active":
        raise HTTPException(409, f"@{handle} is already being tracked")

    # Prove it can actually be read before saving — Business Discovery only
    # reaches Business/Creator accounts, so this is the single most common
    # failure and it deserves a clear message rather than a silent dead row.
    try:
        profile = await trend_research.validate_handle(handle)
    except ig.InstagramConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    comp = await store.upsert_competitor(
        username=handle,
        display_name=profile.get("display_name"),
        biography=profile.get("biography"),
        profile_picture_url=profile.get("profile_picture_url"),
        followers_count=profile.get("followers_count"),
        media_count=profile.get("media_count"),
        category=body.category, region=body.region or "uae", why=body.why,
        status="active", source="manual",
    )
    if existing and existing["status"] == "excluded":
        # Re-adding something previously ignored is an explicit un-ignore.
        comp = await store.update_competitor(int(comp["id"]), status="active") or comp
    return {"competitor": comp}


@router.patch("/competitors/{competitor_id}")
async def patch_competitor(competitor_id: int, body: PatchCompetitorRequest) -> dict:
    if body.status is not None and body.status not in store.COMPETITOR_STATUSES:
        raise HTTPException(422, "status must be 'active' or 'excluded'")
    if body.region is not None and body.region not in store.REGIONS:
        raise HTTPException(422, "region must be 'uae', 'gcc' or 'international'")
    # `exclude_unset`, so a field the caller did not mention is left alone while
    # one they sent as null is CLEARED. Passing all four unconditionally made
    # those two cases identical, and the store had to guess: it dropped every
    # null, so the Edit form could never empty a box it had filled.
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        comp = await store.get_competitor(competitor_id)
    else:
        comp = await store.update_competitor(competitor_id, **fields)
    if not comp:
        raise HTTPException(404, "competitor not found")
    return {"competitor": comp}


@router.delete("/competitors/{competitor_id}")
async def delete_competitor(competitor_id: int) -> dict:
    """Remove entirely — its discovered posts cascade away. Use PATCH with
    status='excluded' instead to keep the row and just stop watching."""
    if not await store.delete_competitor(competitor_id):
        raise HTTPException(404, "competitor not found")
    return {"ok": True}


@router.post("/competitors/research")
async def research_competitors(body: ResearchRequest) -> dict:
    """AI suggestions, each verified against the real API before being returned.

    The four focus dials steer WHICH accounts the model proposes — they are not
    the feed's ranking weights. Omit them all and the behaviour is unchanged."""
    for name, value, allowed in (
        ("geo", body.geo, trend_research.GEO_OPTIONS),
        ("relevance", body.relevance, trend_research.RELEVANCE_OPTIONS),
        ("engagement", body.engagement, trend_research.ENGAGEMENT_OPTIONS),
        ("size", body.size, trend_research.SIZE_OPTIONS),
    ):
        if value is not None and value not in allowed:
            raise HTTPException(422, f"{name} must be one of {', '.join(allowed)}")
    try:
        return await trend_research.research_competitors(
            hint=body.hint, limit=body.limit, geo=body.geo,
            relevance=body.relevance, engagement=body.engagement, size=body.size,
        )
    except ig.InstagramConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/competitors/{competitor_id}/scan")
async def scan_one_competitor(
    competitor_id: int, body: Optional[ScanRequest] = None,
) -> dict:
    """One account, same contract as a full scan: `max_posts` is the ceiling the
    person set on the button, not a target."""
    req = body or ScanRequest()
    comp = await store.get_competitor(competitor_id)
    if not comp:
        raise HTTPException(404, "competitor not found")
    if comp["status"] != "active":
        raise HTTPException(409, f"@{comp['username']} is excluded — re-include it first")
    try:
        scan = await trend_finder.start_scan(
            [competitor_id], lookback_days=req.lookback_days,
            max_posts=req.max_posts,
        )
    except trend_finder.TrendScanError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"scan": scan}


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------

@router.post("/scan")
async def start_scan(body: Optional[ScanRequest] = None) -> dict:
    """Queue a scan. Returns immediately — a background worker runs it, so the
    scan survives a backend restart. Poll /scan/latest."""
    req = body or ScanRequest()
    try:
        scan = await trend_finder.start_scan(
            req.competitor_ids, lookback_days=req.lookback_days,
            max_posts=req.max_posts,
        )
    except trend_finder.TrendScanError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"scan": scan}


@router.get("/scan/latest")
async def latest_scan() -> dict:
    """The newest scan, and how often the poller starts one by itself (0 when
    it never will: the interval is 0, or this process runs no pollers)."""
    auto = int(config.trend_auto_scan_hours or 0) if config.pollers_enabled else 0
    return {"scan": await store.latest_scan(), "auto_scan_hours": max(auto, 0)}


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

@router.get("/posts")
async def list_posts(
    sort: str = Query(default="opportunity"),
    status: Optional[str] = None,
    region: Optional[str] = None,
    media_type: Optional[str] = None,
    theme: Optional[str] = None,
    competitor_id: Optional[int] = None,
    min_fit: Optional[int] = Query(default=None, ge=0, le=100),
    # The category tabs. Omitted = every source, which is what an older frontend
    # sends and what "All" means.
    source: Optional[str] = None,
    platform: Optional[str] = None,
    language: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    w_out: Optional[float] = Query(default=None, ge=0, le=1),
    w_fit: Optional[float] = Query(default=None, ge=0, le=1),
    w_uae: Optional[float] = Query(default=None, ge=0, le=1),
) -> dict:
    """The ranked feed. The three weights re-blend the opportunity score on the
    fly, so moving a slider re-ranks without re-scanning or re-scoring."""
    if sort not in trend_scoring.SORTS:
        raise HTTPException(422, f"sort must be one of {', '.join(trend_scoring.SORTS)}")
    if source is not None and source not in store.POST_SOURCES:
        raise HTTPException(
            422, f"source must be one of {', '.join(sorted(store.POST_SOURCES))}"
        )
    # `min_fit` is applied AFTER ranking rather than in SQL so the response can
    # say how many rows the bar is hiding. The AI News category hides technical
    # stories by default, and a filter that silently removes items is a filter
    # the user cannot trust — the feed prints "N below the fit bar" and offers
    # the select as the way back. One query answers both questions; asking SQL
    # would need a second round trip for the count.
    rows = await store.query_posts(
        status=status, region=region, media_type=media_type, theme=theme,
        competitor_id=competitor_id, search=q,
        source=source, platform=platform, language=language,
    )
    weights = {"outperformance": w_out, "fit": w_fit, "uae": w_uae}
    ranked = trend_scoring.rank_posts(rows, sort=sort, weights=weights)

    hidden = 0
    if min_fit:
        bar = int(min_fit)
        kept = [r for r in ranked if int(r.get("fit_score") or 0) >= bar]
        hidden = len(ranked) - len(kept)
        ranked = kept

    return {
        "items": ranked[offset:offset + limit],
        "total": len(ranked),
        "hidden_by_min_fit": hidden,
        "weights": trend_scoring.normalise_weights(weights),
    }


@router.get("/posts/{post_id}")
async def post_detail(post_id: int) -> dict:
    """One post — the whole stored row, for the detail view behind a card.

    The feed ships a ranked page and the card shows a slice of it; this is the
    same row unabridged, so the detail view can show the caption in full, the
    measured counts (like_count / comments_count / view_count / engagement /
    engagement_rate / outperformance) and the AI verdict (topic, theme,
    fit_score, suggested_angle) side by side. Nothing is computed here: what
    the scan measured is what is returned.

    Note for anything rendering this: there is no `shares` column and there
    never will be — the Instagram Graph API does not expose shares.
    """
    post = await store.get_post(post_id)
    if not post:
        raise HTTPException(404, "post not found")
    return {"post": post}


@router.patch("/posts/{post_id}")
async def patch_post(post_id: int, body: PatchPostRequest) -> dict:
    if body.status not in store.POST_STATUSES:
        raise HTTPException(
            422, f"status must be one of {', '.join(sorted(store.POST_STATUSES))}"
        )
    post = await store.update_post_status(post_id, body.status)
    if not post:
        raise HTTPException(404, "post not found")
    return {"post": post}


def _thumb_media_type(path: Path) -> str:
    """The image type this FILE actually is, from its extension.

    Hardcoding "image/jpeg" here was a real bug, not a cosmetic one. Every
    cached thumbnail used to be a JPEG off Instagram's CDN, so the constant was
    true by accident. It stopped being true the moment the news covers arrived
    as PNG: the route answered 200 with PNG bytes labelled image/jpeg, and a
    card that had a picture rendered a blank grey tile.
    """
    kind, _ = mimetypes.guess_type(path.name)
    return kind if (kind or "").startswith("image/") else "image/jpeg"


@router.get("/posts/{post_id}/thumb")
async def post_thumb(post_id: int):
    """Serve the locally-cached thumbnail. Instagram CDN URLs expire, so the
    stored copy is the only one guaranteed to still load."""
    post = await store.get_post(post_id)
    if not post:
        raise HTTPException(404, "post not found")
    rel = post.get("local_thumb_path")
    if not rel:
        raise HTTPException(404, "no cached thumbnail for this post")
    path = resolve_local(str(rel))
    if not path.is_file():
        raise HTTPException(410, "the cached thumbnail is gone — re-scan to fetch it again")
    return FileResponse(path, media_type=_thumb_media_type(path))


@router.post("/posts/rescore")
async def rescore_posts(body: Optional[RescoreRequest] = None) -> dict:
    """Re-run the AI pass — e.g. after the brand profile changed."""
    ids = body.post_ids if body else None
    try:
        scored = await trend_scoring.rescore(ids)
    except trend_scoring.ScoringUnavailable as exc:
        # Billing off / no credentials is a setup problem the user can act on,
        # not a crash — the module docstring promises a plain-language 422.
        raise HTTPException(422, str(exc)) from exc
    return {"scored": scored}


# Registered below the literal /posts/rescore on purpose: FastAPI matches in
# registration order, so every /posts/{id}/… route stays after the static ones.
@router.post("/posts/{post_id}/brainstorm")
async def post_brainstorm(
    post_id: int, body: Optional[PostBrainstormRequest] = None,
) -> dict:
    """Three different ideas for how 42 could answer this post or story, before
    any tool is chosen. Returns `{ideas, post: {id, title}}` — `title` being the
    row's readable name (the scorer's topic, else the caption, which for a story
    IS the headline), echoed so a caller holds a label for the round without
    re-fetching the post. It is NOT what the picker renders in its header: that
    is computed from the row already on screen the instant the dialog opens,
    which is a model call ahead of this reply. The body is optional so the first
    round is a bare POST.

    Re-post with `feedback` (what the owner typed when none of the three landed)
    and `exclude`, the titles already on screen. `exclude` is what makes a
    re-roll come back DIFFERENT rather than the same three ideas reworded: the
    model is told which titles it has already spent, and any that survive
    anyway are dropped by the parser.

    Unlike the General-trends twin the reply carries no `research` — a
    competitor post arrives with its caption and the scorer's verdict attached
    and a news item IS the story, so there is nothing to go and look up."""
    try:
        out = await trend_ideation.brainstorm_ideas(
            post_id=post_id,
            feedback=(body.feedback or "") if body else "",
            exclude=(body.exclude or []) if body else [],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, "post not found") from exc
    except trend_ideation.IdeationUnavailable as exc:   # MUST precede RuntimeError
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return out


@router.post("/posts/{post_id}/ideas")
async def post_ideas(post_id: int, body: PostIdeasRequest) -> dict:
    """AI prefill for one Producer tool, generated from this post.

    The "Make this" button: instead of handing the user a topic on the
    clipboard, this writes the destination creator's actual brief — a poster
    brief, a Veo prompt, per-scene dialogue — so the tool opens already filled.
    With an `idea` from /brainstorm the brief EXECUTES that idea instead of
    inventing a fourth angle; without one the behaviour is what it always was."""
    try:
        prefill = await trend_ideation.generate_prefill(post_id=post_id, tool=body.tool,
                                                        idea=body.idea)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, "post not found") from exc
    except trend_ideation.IdeationUnavailable as exc:   # MUST precede RuntimeError
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"prefill": prefill}


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

@router.get("/themes")
async def list_themes() -> dict:
    """Discovered topics grouped into reusable buckets — the topic-finder view."""
    return {"items": await store.theme_rollup()}


# ---------------------------------------------------------------------------
# Industry Signal
# ---------------------------------------------------------------------------
# Six endpoints, down from eleven. The nine that managed a feed catalogue went
# with the catalogue: there are no feeds to add, test, reset or delete, and the
# whole configuration surface is now one sentence saying what to look for.
#
# A "stories per day" default lives here rather than at 0: the degraded health
# branch below feeds a `<select>`, and a value outside its options renders the
# control blank.
DEFAULT_STORY_CAP = news_store.DEFAULT_STORIES


@router.get("/news/health")
async def news_health() -> dict:
    """Did it run, and what did it find.

    Powers the search bar above the feed. It NEVER raises: a status line that
    500s is worse than one saying it does not know, so a failure comes back as
    `ok: false` carrying every field the feed reads.

    THE FALLBACK MUST MATCH THE SUCCESS SHAPE, field for field. It is the half
    nobody looks at, so it is the half that rots: it was still returning
    `next_run_at` months after the schedule was deleted, and never gained
    `running`, which `TrendNewsHealth` declares as required. A frontend reading
    `health.running` off this branch would have got `undefined` on the one day
    the database was down.
    """
    try:
        payload = await news_store.health()
    except Exception as exc:  # noqa: BLE001 - the feed must render regardless
        log.exception("[news] health failed")
        return {"ok": False, "error": str(exc)[:300], "enabled": False,
                "ever_ran": False, "last_run": None, "running": False,
                "runs": [], "brief": "", "stories_per_day": DEFAULT_STORY_CAP,
                "min_fit_default": 0}
    return {
        "ok": True,
        **payload,
        # The feed's fit floor. Zero, deliberately: the judge already dropped
        # everything it thought was wrong for this audience, so a second bar
        # here would hide stories that already passed the only test that
        # matters. Kept in the payload because the feed reads it by name.
        "min_fit_default": 0,
    }


@router.get("/news/config")
async def news_config() -> dict:
    """What we look for, how many stories, and whether it is switched on."""
    try:
        return {"config": await news_store.get_config()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"news config unavailable: {str(exc)[:200]}") from exc


@router.put("/news/config")
async def news_config_save(body: NewsConfigRequest) -> dict:
    """Save the brief, the count, or the switch.

    `exclude_unset` so a screen that only touched the switch does not resend the
    brief and quietly overwrite an edit somebody made in another tab. Values are
    validated and never clamped: a number the user typed either takes effect or
    comes back with a sentence saying why it did not.
    """
    patch = body.model_dump(exclude_unset=True)
    try:
        return {"config": await news_store.update_config(patch)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, f"news config unavailable: {str(exc)[:200]}") from exc


@router.post("/news/brief/suggest")
async def news_brief_suggest(body: BriefSuggestRequest) -> dict:
    """Turn a rough note into two or three briefs to pick from.

    Propose-and-pick, never write-in-place: the suggestions come back and the
    person chooses. A button that silently rewrites the one field controlling
    what the whole feature searches for would be the worst kind of helpful.
    """
    try:
        return await news_search.suggest_briefs(body.notes or "")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except news_search.SearchUnavailable as exc:
        raise HTTPException(503, f"the AI is not available right now: {str(exc)[:200]}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not write any suggestions: {str(exc)[:200]}") from exc


@router.get("/news/watch")
async def news_watch() -> dict:
    """What we follow, and where the stories have actually been coming from.

    Both halves in one response because the screen shows them together and they
    answer one question between them: this is what we ask for, and this is what
    came back. `watch` holds both the names we follow and the websites we
    always check (`kind`); `outlets` is the record of where every kept story
    actually came from, whichever search found it.
    """
    try:
        return {
            "watch": await news_store.list_watch(),
            "outlets": await news_store.outlets(),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"could not read the list: {str(exc)[:200]}") from exc


@router.post("/news/watch")
async def news_watch_add(body: WatchAddRequest) -> dict:
    """Follow one more thing. 422 carries the sentence the screen prints."""
    try:
        return {"item": await news_store.add_watch(body.value, body.kind)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"could not save that: {str(exc)[:200]}") from exc


@router.delete("/news/watch/{watch_id}")
async def news_watch_remove(watch_id: int) -> dict:
    """Stop following one thing. Removing what is already gone is not an error:
    two tabs open on this screen is the normal way that happens."""
    try:
        return {"removed": await news_store.remove_watch(watch_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"could not remove that: {str(exc)[:200]}") from exc


@router.post("/news/cancel")
async def news_cancel() -> dict:
    """Stop the search that is running, and clear anything left open.

    Also the cure for a run stranded by a restart: the row is closed whether or
    not there is still a task behind it, which is what unsticks a button that
    has been spinning since the backend last died mid-search.
    """
    from ..workers import news_poller

    try:
        stopped = await news_poller.cancel_now()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"could not stop the search: {str(exc)[:200]}") from exc
    return {
        "stopped": stopped,
        "note": "Stopped." if stopped else "Nothing was running.",
    }


@router.post("/news/fetch")
async def news_fetch(body: Optional[NewsFetchRequest] = None) -> dict:
    """Search now. The ONLY way a search ever starts: there is no timer.

    `limit` is the most stories this search may keep, chosen on the button. It
    is a cap, never a target: finding fewer is a normal outcome and is reported
    as one rather than padded.

    Fire-and-forget, because the run makes two model calls and an HTTP request
    that waits for them is a request that times out. The screen watches
    `/news/health` for the outcome.
    """
    limit = body.limit if body and body.limit is not None else None
    if limit is not None:
        # Remembered so the control opens where it was left, not because it
        # schedules anything.
        try:
            await news_store.update_config({"stories_per_day": limit})
        except Exception:  # noqa: BLE001 - a preference is not worth a 500
            log.warning("[news] could not remember the story cap", exc_info=True)

    # Imported here rather than at module scope: the worker imports the
    # services this router imports, so a top-level import closes the circle.
    from ..workers import news_poller

    started = news_poller.trigger_now(limit)
    return {
        "started": started,
        "note": ("Searching. This takes a minute or two."
                 if started else
                 "A search is already running. Give it a minute."),
    }
