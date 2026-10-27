"""Rebrand harness API — recipes of elements, made into versions: in house
(our own graphics over the untouched footage, $0 — every new version since
2026-09-29), or, for the versions made before, by Omni.

  GET  /api/rebrand/harness/schema      the recipe JSON Schema + version
  GET  /api/rebrand/harness/presets     built-in presets (42 default, …)
  GET  /api/rebrand/harness/catalogue   everything the studio offers: kinds,
                                        places, entrances, patterns, colours,
                                        swatches, limits
  POST /api/rebrand/harness/prepare     {run_id, target} — the scene ready to
                                        preview (frames per chunk, the video)
                                        + the default recipe (the 42 default,
                                        made in house; an intro or outro:
                                        empty) + its versions + how far the
                                        scene's warm-up got
  GET  /api/rebrand/harness/warmup?run_id&target   that warm-up, polled while
                                        the first preview is on its way
                                        (with the scene's analysis: its
                                        speaker frames, its camera)
  GET  /api/rebrand/harness/house/camera?run_id&target   the scene's camera:
                                        per frame [a, b, tx, ty] (a
                                        similarity from frame 0's pixels to
                                        that frame's), its cuts, where it is
                                        held, the speaker's masks — or how
                                        far its analysis is (free, local)
  POST /api/rebrand/harness/house/render   {run_id, target, recipe, base?,
                                        icons_plan_key?} — a draft made in
                                        house rendered exactly as Generate
                                        will make it, in the background
                                        (free; Generate copies it)
  GET  /api/rebrand/harness/house/render/{key}?run_id&target   how far it is
  POST /api/rebrand/harness/house/moment   {…, t} — its exact frame at t
  POST /api/rebrand/harness/preview     {run_id, target, recipe, base?} — the
                                        draft's picture (free); each part
                                        lists its layers as colour-free
                                        alphas, so the studio paints a new
                                        colour on its frame at once. Made in
                                        house, its first part is the design
                                        still (the run's plate over frame 0)
                                        and `house` says how each pattern
                                        moves
  POST /api/rebrand/harness/pattern/art   {run_id, target, recipe, base?,
                                        element_id} — one pattern unmoved and
                                        uncut, past the frame's edges (white
                                        RGBA + where it lies): the stage's
                                        stand-in while a hand moves, sizes or
                                        turns it (free, local)
  POST /api/rebrand/harness/versions    {run_id, target, recipe, base?} — generate
  GET  /api/rebrand/harness/versions?run_id&target   this scene's versions,
                                        each with what it was built on and
                                        the layers it shows
  GET  /api/rebrand/harness/versions/{id}/still/{j}  a version's part j as
                                        its settled first frame (PNG)
  POST /api/rebrand/harness/versions/{id}/remake   a version made in house
                                        whose video is lost (`lost` on the
                                        list), made again for $0 — the same
                                        frames (GET it to see how far)
  POST /api/rebrand/harness/base        {run_id, target, version_id} — a
                                        version made ready to add on top of:
                                        its settled frames and its layers
  GET  /api/rebrand/harness/file/{path} a studio image (target frames, the
                                        layers' alphas, an icon's settled
                                        sprite — immutable, named by its bytes)

Icons (one icon element: up to three icons, stamped by us):
  GET    /api/rebrand/harness/icons/catalogue   the 42 picks, styles,
                                        entrances, sizes, the timing rules
                                        and what an AI drawing costs
  GET    /api/rebrand/harness/icons/search?q&offset&limit   the one search:
                                        the library and the team's icons,
                                        ranked, + whether "Generate with AI"
                                        may be offered (free: nothing drawn,
                                        no AI asked)
  GET    /api/rebrand/harness/icons/team?include_hidden&limit   the team's
                                        own icons (drawn by the AI, or
                                        uploaded), newest first
  POST   /api/rebrand/harness/icons/upload/preview   multipart {file, mode?,
                                        label?} — an SVG / PNG / WebP / JPEG
                                        made into a one-colour glyph, shown
                                        as the circle and the chip will stamp
                                        it (free; nothing saved). 413 too
                                        large, 415 not a kind we read, 422
                                        not an icon (the reason, in words)
  POST   /api/rebrand/harness/icons/upload   multipart {file, name, tags?,
                                        mode?} — saved to the team's icons,
                                        on every machine (201; 200 with
                                        `duplicate` when that very icon is
                                        there already). The uploaded SVG is
                                        never served back — only our PNG is
  PATCH  /api/rebrand/harness/icons/custom/{id}   {name?, label?, tags?,
                                        hidden?} — a team icon renamed,
                                        re-tagged, hidden or shown again (its
                                        pixels never change)
  POST   /api/rebrand/harness/icons/generate   {name, again?} — the ONE paid
                                        icon call, on the user's press: the
                                        name drawn by the AI (~$0.04; one
                                        already made is free), as the circle
                                        and the chip will stamp it. 422 a
                                        brand / a bad drawing, 429 the
                                        team's drawings for the day are used,
                                        503 the model or the table is down
  GET    /api/rebrand/harness/icons/glyph/{id}?weight=fill|bold   SVG
                                        (library, in that weight) or PNG
                                        (a team icon: one shape, no weights)
  GET    /api/rebrand/harness/icons/font/{key}.ttf   a chip label font face
                                        (fonts.json key; immutable)
  DELETE /api/rebrand/harness/icons/custom/{id}  hide a team icon for
                                        everyone (versions that use it still
                                        draw it)
  POST   /api/rebrand/harness/icons/plan    {run_id, target, recipe, base?,
                                        ask_again?, reorder?} — which icons,
                                        on which words, where, in what look
                                        (each its own: style, colours, words,
                                        in front) with each one's settled
                                        sprite (free; "warming" at first).
                                        props.keep ("Keep v3's icons"): that
                                        version's icons exactly as it froze
                                        them, nothing planned
  POST   /api/rebrand/harness/icons/moment  {run_id, target, recipe, plan_key,
                                        t, version_id?} — that plan's frame
                                        at t, made exactly as the run makes it

POST /versions takes `icons_plan_key` (the icon plan the studio showed): the
plan is made again, a different one is a 409 with it ("check them"), and
the one that stands is frozen into the stored recipe (props.resolved) — the
run stamps exactly that. Each icon's track on the timeline is kept beside
it (options.icon_lanes, when one is off track 0: the version's read-only
timeline shows them where the draft did; the render list has no track).
Made in house, every pattern is frozen the same way
(its props.resolved: the place and colour the preview drew), so the run
draws it exactly there.

Omni is switched off for new work (REBRAND_OMNI, off unless set to on): every
route that takes a recipe refuses one for Omni (400, rc.OMNI_OFF), so nothing
here asks Omni for anything. Versions Omni made before still list, show their
stills, rate and download (the experiments API), are added on top of for $0
(over Omni's own video) and are made again in house ("Make it in house").

`base` = {version_id, mode: "stack" | "rebuild", fresh?}: build on a finished
version of the same clip — "stack" (Add on top) draws only the recipe's
layers on that version, which stays exactly as it is; "rebuild" (Change it)
redoes it from the original and reuses what did not change; `fresh` (Try
again) is a rebuild with a new take.

`end_frame` (every request that names a scene; optional): where the scene
ends, in the reel's 30 fps frames — the end grip. An end before the clip's
own last frame cuts the preview, the plan, the draft render and Generate
there (the version stores it as options.end_frame and is made again to
it); none, or one past the clip, changes nothing. A request field, never
the recipe's: no hash moves, and a server before it ignores it.

`variant` "full" (a scene's requests, optional): the scene kept whole past
its automatic cut (a Veo tail flash) — prepared once on POST /prepare with
it, in the background, while the end grip is dragged past the cut; every
request of that view then carries it, and a version generated on it is made
on that clip (options.variant). A server before it ignores it (and answers
with the trimmed scene: its scene has no `variant`).

`target` is a scene (scene_N) or the reel's intro or outro ("intro" |
"outro"): the very clip the reel uses there, done by hand only — its draft
starts empty, the icon director is never asked on it, and only versions
made in house are listed for it (an Omni-era card has its transition baked
in).

The logic lives in services/rebrand_harness/; a version is an experiment
(GET /api/rebrand/experiments/{id} polls it).
"""
from __future__ import annotations

import asyncio
import html
from typing import Any, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError

from ..services import pattern_library
from ..services import rebrand_pattern_ref as pattern_ref
from ..services import rebrand_pipeline as pipeline
from ..services import rebrand_store as store
from ..services import th_reel
from ..services import yas_talking_head_v2 as th
from ..services.rebrand_harness import base as hbase
from ..services.rebrand_harness import icon_catalog
from ..services.rebrand_harness import icon_feedback
from ..services.rebrand_harness import icon_import
from ..services.rebrand_harness import label_font
from ..services.rebrand_harness import recipe as rc
from ..services.rebrand_harness import studio
from ..services.rebrand_harness.elements import icon as icon_el
from ..services.rebrand_harness.elements import pattern as pattern_el

router = APIRouter(prefix="/api/rebrand/harness", tags=["rebrand-harness"])

KIND_LABELS = {"pattern": "Pattern", "logo": "Logo", "text": "Text", "icon": "Icons",
               "slab": "Shape", "beads": "Beads", "plate": "Plate"}
ANCHOR_LABELS = {"auto": "Its own place", "edge-left": "Left side", "edge-right": "Right side",
                 "edge-top": "Top", "edge-bottom": "Bottom", "full": "Full screen"}
COLOUR_SOURCES = [
    {"value": "intro", "label": "From the intro", "help": "The colour dug out of the intro card."},
    {"value": "brand", "label": "42 violet", "help": "The brand's own colour."},
    {"value": "custom", "label": "Pick a colour", "help": "Any colour you choose."},
]
# One click per colour on a pattern (props.colour): "auto" is the recipe's
# colours (the intro's by default).
SWATCHES = [
    {"value": "auto", "label": "Auto"},
    {"value": "#7f2bf2", "label": "42 violet"},
    {"value": "#8d01fa", "label": "42 purple"},
    {"value": "#303030", "label": "Charcoal"},
    {"value": "#f4f4f8", "label": "White"},
]
INTENSITIES = [{"value": v, "label": v.capitalize()} for v in ("subtle", "balanced", "bold")]
# New drafts start with the circle; the badge and the bare glyph are every
# older version's looks (`old`: the studio offers them only while in use).
ICON_STYLES = [{"value": "circle", "label": "Circle"}, {"value": "chip", "label": "Circle + words"},
               {"value": "badge", "label": "Badge", "old": True},
               {"value": "glyph", "label": "Just the icon", "old": True}]
# "auto" is the glitch (the 42 reels' own entrance).
ICON_ENTRANCES = [{"value": "glitch", "label": "Glitch"}, {"value": "pop", "label": "Pop"},
                  {"value": "fade", "label": "Fade"}, {"value": "slide", "label": "Slide"},
                  {"value": "none", "label": "None"}]
ICON_ANCHOR_LABELS = {"auto": "Auto", "left": "Left", "right": "Right", "top": "Top",
                      "bottom": "Bottom", "custom": "Your spot"}
_GLYPH_HEADERS = {"Cache-Control": "private, max-age=86400"}
# A font key names its file's bytes (sha8), so it never changes.
_FONT_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable"}


class SceneRequest(BaseModel):
    run_id: int
    target: str = Field(pattern=r"^(scene_\d{1,2}|intro|outro)$")
    # The scene's end, in the reel's 30 fps frames (module doc; None: the
    # whole clip).
    end_frame: Optional[int] = Field(default=None, ge=30, le=900)
    # "full": the scene kept whole past its automatic cut (module doc).
    variant: Optional[Literal["full"]] = None


def _variant(body: SceneRequest) -> dict:
    """The request's variant as the studio's keyword — only when one is set."""
    return {"variant": body.variant} if body.variant else {}


def _qvariant(variant: Optional[str]) -> dict:
    return {"variant": "full"} if variant == "full" else {}


def _end(body: SceneRequest) -> dict:
    """The request's end as the studio's keyword — only when one is set, so
    a call without one is the very call it always was."""
    return {"end_frame": body.end_frame} if body.end_frame is not None else {}


class BaseRequest(BaseModel):
    """The finished version a request builds on."""
    version_id: int
    mode: Literal["stack", "rebuild"]
    fresh: bool = False                 # rebuild only: a new take, nothing reused


class RecipeRequest(SceneRequest):
    recipe: dict[str, Any]
    base: Optional[BaseRequest] = None


class BuildOnRequest(SceneRequest):
    version_id: int


class GenerateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=icon_catalog.NAME_MAX)
    again: bool = False                # "Try again": a new drawing, the one shown steps aside


class IconUpdateRequest(BaseModel):
    """A team icon's new name, short label, tags, or hidden / shown."""
    name: Optional[str] = Field(default=None, max_length=icon_import.NAME_MAX * 3)
    label: Optional[str] = Field(default=None, max_length=icon_catalog.LABEL_MAX * 3)
    tags: Optional[list[str]] = Field(default=None, max_length=icon_import.TAGS_MAX * 3)
    hidden: Optional[bool] = None


class ReorderRequest(BaseModel):
    """Show the icon `glyph` at place `to` of the on-screen order."""
    glyph: str = Field(pattern=rc.GLYPH_ID)
    to: int = Field(ge=0, le=rc.ICON_ITEMS_MAX)


class IconPlanRequest(RecipeRequest):
    ask_again: bool = False            # "Ask the AI again": a new take of the director
    reorder: Optional[ReorderRequest] = None


class VersionRequest(RecipeRequest):
    # The icon plan the studio showed (a recipe with icons): Generate makes
    # it again, and a different one is a 409 carrying it.
    icons_plan_key: Optional[str] = Field(default=None, max_length=64)
    # The icons seen in Add icon → Suggested while this draft was made (glyph
    # ids): stored in options.seen_ideas, so the feedback log can say which
    # the user saw and left (icon_feedback's `ignored`). Never in a key.
    seen_ideas: Optional[list[str]] = Field(default=None, max_length=60)


class MomentRequest(RecipeRequest):
    plan_key: str = Field(pattern=r"^[0-9a-f]{16}$")
    t: float = Field(ge=0.0, le=600.0)
    version_id: Optional[int] = None   # on that version (added on top of it)


class HouseRequest(RecipeRequest):
    # The icon plan the studio shows (a draft with icons): its icons are
    # the ones drawn.
    icons_plan_key: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{16}$")


class HouseMomentRequest(HouseRequest):
    t: float = Field(ge=0.0, le=600.0)


class PatternArtRequest(RecipeRequest):
    element_id: str = Field(min_length=1, max_length=64)


ICONS_CHANGED = "The icons changed since you looked — check them"


def _spec(body: RecipeRequest) -> Optional[hbase.BaseSpec]:
    return hbase.BaseSpec.of(body.base) if body.base is not None else None


def _not_omni(body: RecipeRequest) -> None:
    """A recipe for Omni (its engine, "omni" when left out) while Omni is
    switched off (rc.omni_on) is a 400 in plain words, before anything is
    read: nothing here previews, plans or makes new work with one."""
    if body.recipe.get("engine", "omni") == "omni" and not rc.omni_on():
        raise HTTPException(400, {"message": rc.OMNI_OFF, "errors": [
            {"element_id": None, "field": "engine", "message": rc.OMNI_OFF}]})


def _recipe(body: RecipeRequest) -> rc.Recipe:
    """The request's recipe, normalised, on the request's scene, with its
    seed (so the preview and the run draw the same place). Added on top of a
    version, it opens with the harness's words (studio.on_top). A frozen
    icon plan or frozen pattern (props.resolved) a client sends is never
    read: only Generate writes one, from what the studio showed. A recipe
    for Omni while Omni is switched off is a 400 (_not_omni)."""
    _not_omni(body)
    try:
        r = pattern_el.strip_resolved(icon_el.strip_resolved(
            rc.normalize({**body.recipe, "target": body.target})))
    except rc.RecipeError as e:
        raise HTTPException(422, {"message": str(e), "errors": e.errors}) from e
    except ValidationError as e:
        raise HTTPException(422, {"message": "The recipe is not valid.",
                                  "errors": e.errors(include_url=False, include_context=False)}) from e
    if not r.seed:
        r = r.model_copy(update={"seed": studio.default_seed(body.run_id, body.target)})
    if body.base is not None and body.base.mode == "stack":
        r = studio.on_top(r)
    return r


async def _checked(body: RecipeRequest, chain: list = ()) -> rc.Recipe:
    """_recipe, plus what only the pattern library can tell: an unknown
    pattern, a full-screen one beside another, too many — `chain`: the
    patterns a version being added to already carries (422, one error per
    element). A library that cannot be read right now is a 503 (worth a Try
    again)."""
    r = _recipe(body)
    errors = await pattern_el.check(r, chain)
    if errors:
        raise HTTPException(422, {"message": str(rc.RecipeError(errors)), "errors": errors})
    return r


async def _on(body: RecipeRequest) -> tuple[Optional[hbase.BaseSpec], list, Optional[hbase.BaseVersion]]:
    """The request's base, checked (the owner's words when it cannot be
    built on), — stack — the patterns it already carries, and the version.
    A recipe for Omni while Omni is off is refused first (_not_omni)."""
    _not_omni(body)
    spec = _spec(body)
    if spec is None:
        return None, [], None
    bv = await studio.load_base(body.run_id, body.target, spec)
    return spec, (hbase.elements_of(bv.layers) if spec.mode == "stack" else []), bv


def _icon_rules(r: rc.Recipe, bv: Optional[hbase.BaseVersion], *, generate: bool) -> None:
    """Omni's own icons are kept only on a version that has them (422); ours
    never go on top of one that has them — a preview or a plan only reports
    it (parent_has_omni_icons), Generate refuses it (409)."""
    refused = hbase.icon_refusal(r, bv)
    if refused is None:
        return
    status, message = refused
    if status == 422:
        el = rc.icon_element(r)
        raise HTTPException(422, {"message": message, "errors": [
            {"element_id": el.id if el is not None else None, "field": "props.omni_own", "message": message}]})
    if generate:
        raise HTTPException(status, message)


def _default_recipe(run_id: int, target: str) -> dict:
    """What a new scene's draft starts from: the 42 default, made in house
    (the owner, 2026-09-29: no Omni for new work). An intro or outro is done
    by hand only: it starts empty (made in house, with its seed)."""
    if studio.is_bookend(target):
        r = rc.normalize(rc.Recipe(engine="house", target=target))
    else:
        r = rc.preset_42_house(target)
    return r.model_copy(update={"seed": studio.default_seed(run_id, target)}).model_dump(mode="json")


def _fetcher(known: dict[int, dict]) -> hbase.Fetch:
    """Versions by id: the ones already read first, else the database (a
    version it cannot read counts as gone)."""
    async def fetch(exp_id: int) -> Optional[dict]:
        if exp_id not in known:
            try:
                row = await store.get_experiment(exp_id)
            except Exception:  # noqa: BLE001 — the list still shows what it has
                row = None
            if row is None:
                return None
            known[exp_id] = row
        return known[exp_id]

    return fetch


async def _lineage(e: dict, fetch: hbase.Fetch) -> dict:
    """What a version was built on, the layers it shows (each with the
    version that drew it: a stacked one shows the ones under it too) and how
    many passes were added on top of the first."""
    chain = await hbase.chain_of(e, fetch)
    return {"base": store.base_of(e), "layers": hbase.layers_of(chain), "depth": len(chain) - 1}


async def _versions(run_id: int, target: str) -> list[dict]:
    live = pipeline.live_ids()
    rows = []
    for e in await store.list_for_run(run_id, limit=20, target=target):
        if studio.is_bookend(target) and not hbase.made_in_house(e):
            continue                  # an Omni-era card: its transition is baked in
        rows.append(await store.mark_interrupted_if_orphaned(e, live_ids=live))
    fetch = _fetcher({int(e["id"]): e for e in rows})
    out = []
    for e in rows:
        pub = pipeline.public(e)
        pub["recipe"] = (e.get("options") or {}).get("recipe")
        # Each part's settled first frame, once the version is ready: what
        # the studio shows for it until play is pressed.
        pub["stills"] = hbase.still_urls(e)
        # Made in house and its video gone: it can be made again, free.
        pub["lost"] = hbase.lost(e)
        pub.update(await _lineage(e, fetch))
        out.append(pub)
    return out


@router.get("/schema")
async def schema() -> dict:
    return {"recipe_version": rc.RECIPE_VERSION, "schema": rc.json_schema(),
            "kinds": rc.KIND_RULES, "max_elements": rc.MAX_ELEMENTS,
            "icon_props": rc.icon_props_schema()}


@router.get("/presets")
async def presets() -> dict:
    return {"presets": [
        {"id": pid, "name": "42 default" if pid == "42-default" else pid, "builtin": True,
         "recipe": make().model_dump(mode="json")}
        for pid, make in rc.BUILTIN_PRESETS.items()
    ]}


@router.get("/catalogue")
async def catalogue() -> dict:
    return {
        "recipe_version": rc.RECIPE_VERSION,
        "kinds": [{"kind": k, "label": KIND_LABELS[k], **v} for k, v in rc.KIND_RULES.items()],
        "anchors": [{"value": a, "label": ANCHOR_LABELS[a]} for a in rc.PATTERN_ANCHORS],
        # Every entrance our own renderer draws (Omni's, plus the slide):
        # new work is made in house.
        "entrances": [{"value": "auto", "label": "Its own",
                       "help": "The pattern's own entrance, from the edge it grows from."}]
                     + pattern_library.entrances_public(),
        # How a pattern ends its span ("auto": its entrance played
        # backwards, or none when it lasts to the scene's end).
        "exits": pattern_library.exits_public(),
        "patterns": [p.public() for p in await pattern_library.list_patterns()],
        "default_pattern": pattern_library.DEFAULT_SLUG,
        "colour_sources": COLOUR_SOURCES,
        "swatches": SWATCHES,
        "brand_colour": pattern_ref.BRAND_GROUND,
        "intensities": INTENSITIES,
        "presets": (await presets())["presets"],
        "limits": {"max_elements": rc.MAX_ELEMENTS, "notes": rc.NOTES_MAX,
                   "patterns": rc.KIND_RULES["pattern"]["max"], "icons": rc.ICON_ITEMS_MAX},
    }


@router.post("/prepare")
async def prepare(body: SceneRequest) -> dict:
    scene = await studio.prepare(body.run_id, body.target, **_variant(body))
    return {"scene": studio.public_scene(scene, **_end(body)),
            "default_recipe": _default_recipe(body.run_id, body.target),
            "versions": await _versions(body.run_id, body.target),
            "warming": studio.warmup_state(scene)}


@router.get("/warmup")
async def warmup(run_id: int, target: str, variant: Optional[str] = None) -> dict:
    scene = studio.prepared(run_id, target, variant)
    if scene is None:
        return {"prepared": False, "colours": "working", "masks": {"done": 0, "total": 0}}
    return studio.warmup_state(scene)


@router.get("/house/camera")
async def house_camera(run_id: int, target: str, variant: Optional[str] = None) -> dict:
    """How the camera moves over the scene (studio.house_camera): {state:
    "ready", fps, n, m: [[a, b, tx, ty] per frame], cuts_sec, held_from_sec,
    method, warnings, zoom_max, masks (the speaker as the matte bank cuts
    her, for the moving preview), …}, or {state: waiting | working | failed,
    speaker_frames} while the scene is still being analysed (ask again)."""
    return await studio.house_camera(run_id, target, **_qvariant(variant))


async def _house_recipe(body: HouseRequest) -> tuple[rc.Recipe, Optional[hbase.BaseSpec]]:
    """A draft made in house, checked as a preview checks it (400 for one
    Omni makes)."""
    spec, chain, bv = await _on(body)
    r = await _checked(body, chain)
    _icon_rules(r, bv, generate=False)
    if r.engine != "house":
        raise HTTPException(400, studio.HOUSE_ONLY)
    return r, spec


@router.post("/house/render")
async def house_render(body: HouseRequest) -> dict:
    """Render the draft exactly as Generate will make it, in the background
    (studio.house_render): {key, state: queued | running | ready | failed |
    cancelled, progress, url once ready}. The same draft asked again is the
    same render; a new one stops the old. A Generate of the same draft copies
    it. Free. 409: the icon plan shown is gone (look again), or a pattern the
    preview couldn't draw."""
    r, spec = await _house_recipe(body)
    return await studio.house_render(body.run_id, body.target, r, spec, plan_key=body.icons_plan_key, **_end(body),
                                     **_variant(body))


@router.get("/house/render/{key}")
async def house_render_state(key: str, run_id: int, target: str, variant: Optional[str] = None) -> dict:
    """How far a draft render is (studio.house_render_state); 404 for one
    never asked for (ask with POST again)."""
    return studio.house_render_state(run_id, target, key, **_qvariant(variant))


@router.post("/house/moment")
async def house_moment(body: HouseMomentRequest) -> dict:
    """The draft's frame at `t` exactly as the run will make it (studio.
    moment: the plate over the footage's frame): {url, frame, t, key,
    ingredients: {camera, layers}}. Free."""
    r, spec = await _house_recipe(body)
    return await studio.moment(body.run_id, body.target, r, body.t, spec, plan_key=body.icons_plan_key,
                               **_end(body), **_variant(body))


@router.post("/pattern/art")
async def pattern_art(body: PatternArtRequest) -> dict:
    """One pattern of the draft unmoved and uncut, past the frame's edges
    (studio.pattern_art): {art_url, art_key, art_window, art_rect} — what
    the stage draws under the hand while it moves, sizes or turns the
    pattern, before the exact frame lands. Free, local. 404: the draft's
    preview has no such pattern."""
    spec, chain, bv = await _on(body)
    r = await _checked(body, chain)
    _icon_rules(r, bv, generate=False)
    return await studio.pattern_art(body.run_id, body.target, r, body.element_id, spec, **_end(body),
                                    **_variant(body))


@router.post("/preview")
async def preview(body: RecipeRequest) -> dict:
    spec, chain, bv = await _on(body)
    r = await _checked(body, chain)
    _icon_rules(r, bv, generate=False)
    if spec is None:
        return await studio.preview(body.run_id, body.target, r, **_end(body), **_variant(body))
    return await studio.preview(body.run_id, body.target, r, base=spec, **_end(body), **_variant(body))


@router.post("/versions")
async def create_version(body: VersionRequest) -> dict:
    spec, chain, bv = await _on(body)
    r = await _checked(body, chain)
    _icon_rules(r, bv, generate=True)
    plan = None
    if rc.icons_by_pipeline(r):
        # The icons the studio showed, made again: a plan that moved on since
        # is shown instead of generated; the one that stands is frozen into
        # the recipe (props.resolved), and the run stamps exactly that.
        shown, plan = await studio.final_icon_plan(body.run_id, body.target, r, spec, **_end(body),
                                                   **_variant(body))
        if plan is not None:
            if body.icons_plan_key != plan.key():
                raise HTTPException(409, {"message": ICONS_CHANGED, "plan": shown})
            r = icon_el.materialise(r, plan)
    if r.engine == "house" and rc.pattern_elements(r):
        # Made in house: each pattern is frozen exactly as the preview drew
        # it (props.resolved — its place and colour), and the run draws it
        # there with no planner: it cannot land anywhere else.
        r = pattern_el.materialise(r, await studio.final_patterns(body.run_id, body.target, r, spec,
                                                                  **_end(body), **_variant(body)))
    # The version is made for the scene's end (options.end_frame), on the
    # clip the studio showed (options.variant), and says
    # which ideas were seen (options.seen_ideas).
    extra = {**_end(body), **_variant(body)}
    if body.seen_ideas is not None:
        extra["seen_ideas"] = [g for g in body.seen_ideas if isinstance(g, str) and 0 < len(g) <= 80][:60]
    # Each frozen icon's track (options.icon_lanes): the version's read-only
    # timeline shows them where its draft did. Only when one is off track 0:
    # on track 0 alone they never overlap, one row either way.
    lanes = icon_el.lanes_of(plan) if plan is not None else None
    if lanes and any(lanes):
        extra["icon_lanes"] = lanes
    if spec is None:
        exp = await pipeline.start(body.run_id, None, recipe=r, **extra)
    else:
        exp = await pipeline.start(body.run_id, None, recipe=r, base=spec, **extra)
    # What the user did with the AI's icon picks (kept, removed, added), in
    # the background: never in Generate's way, never its failure.
    icon_feedback.after_generate(exp, r, plan)
    # The version is on its way into the reel (the scene's `pending`: it
    # takes the ✓ once ready). Awaited, so the page's next read has it, but
    # bounded and never raising: never Generate's failure.
    await th_reel.after_generate(exp)
    pub = pipeline.public(exp)
    pub["recipe"] = r.model_dump(mode="json")
    pub["stills"] = []
    known = {int(exp["id"]): exp} if exp.get("id") is not None else {}
    pub.update(await _lineage(exp, _fetcher(known)))
    return pub


@router.post("/base")
async def build_on(body: BuildOnRequest) -> dict:
    """A version made ready to add on top of ("Add on top"): each part's
    settled frame, its layers and depth. Made once per scene and kept; the
    first call takes a few seconds. 409/410 with the owner's words when it
    cannot be built on."""
    return await studio.base_scene(body.run_id, body.target, body.version_id, **_variant(body))


@router.get("/versions")
async def list_versions(run_id: int, target: str) -> dict:
    await th._fetch_run_row(run_id)  # 404 for an unknown run
    return {"versions": await _versions(run_id, target)}


_STILL_HEADERS = {"Cache-Control": "private, max-age=86400"}


@router.get("/versions/{exp_id}/still/{part}")
async def version_still(exp_id: int, part: int):
    # Made already: only a ready version's still is ever written, so it is
    # served without asking the database (every filmstrip card asks).
    made = hbase.still_path(exp_id, part) if part >= 0 else None
    if made is not None and made.is_file():
        return FileResponse(str(made), media_type="image/png", headers=_STILL_HEADERS)
    exp = await store.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, f"No version #{exp_id}.")
    if not hbase.is_ready(exp):
        raise HTTPException(404, f"Version #{exp_id} is not ready yet.")
    if part < 0 or part >= hbase.part_count(exp):
        raise HTTPException(404, f"Version #{exp_id} has no part {part + 1}.")
    try:
        p = await hbase.still(exp, part)
    except FileNotFoundError as e:
        raise HTTPException(410, str(e)) from e
    return FileResponse(str(p), media_type="image/png", headers=_STILL_HEADERS)


@router.post("/versions/{exp_id}/remake")
async def remake_version(exp_id: int) -> dict:
    """Make a version made in house again, for $0, when its video is lost
    (studio.remake): the very frames its run made, from its recipe and the
    original. In the background: {version_id, state, progress}. 409 for one
    Omni made, or one that didn't finish."""
    return await studio.remake(exp_id)


@router.get("/versions/{exp_id}/remake")
async def remake_state(exp_id: int) -> dict:
    """How far making it again is (404: not asked for here — ask again)."""
    return studio.remake_state(exp_id)


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

def _glyph_url(icon_id: str) -> str:
    return f"/api/rebrand/harness/icons/glyph/{icon_id}"


def _font_url(face_key: str) -> str:
    return f"/api/rebrand/harness/icons/font/{face_key}.ttf"


def _fonts_public() -> dict:
    """The chip label fonts: the key new chips use and each label key's two
    faces (Latin, Arabic) as URLs — {} when the fonts are not installed."""
    try:
        m = label_font.manifest()
    except label_font.FontError:
        return {}
    return {"default": m.get("default"),
            "labels": {k: {"latin": _font_url(v["latin"]), "arabic": _font_url(v["arabic"])}
                       for k, v in (m.get("labels") or {}).items()}}


@router.get("/icons/catalogue")
async def icons_catalogue() -> dict:
    """The icon library's front page: the 42 picks (grouped), what an icon
    element takes (styles, entrances, where, sizes) plus the timing rules
    the timeline clamps with, the chip label fonts, and what one AI drawing
    costs and how many the team may make a day."""
    return {
        **icon_catalog.catalogue_public(),
        "styles": ICON_STYLES,
        "entrances": ICON_ENTRANCES,
        "anchors": [{"value": a, "label": ICON_ANCHOR_LABELS[a]} for a in rc.ICON_ANCHORS],
        "sizes": dict(icon_el.SIZES),
        "rules": icon_el.rules(),
        "limits": {"items": rc.ICON_ITEMS_MAX, "added": rc.ICON_ADDED_MAX,
                   "dismissed": rc.ICON_DISMISSED_MAX, "pinned": rc.ICON_TWEAKS_MAX,
                   "label": rc.ICON_LABEL_MAX},
        "fonts": _fonts_public(),
        "generate": {"cost_usd": icon_catalog.draw_cost_usd(), "per_day": icon_catalog.draws_per_day()},
        "upload": icon_catalog.upload_limits(),
    }


def _with_png(item: dict) -> dict:
    """A team icon's picture is its PNG (a library icon carries its paths)."""
    return {**item, "png_url": _glyph_url(item["id"]) if item.get("source") == "custom" else None}


@router.get("/icons/search")
async def icons_search(q: str = "", offset: int = 0, limit: int = 60) -> dict:
    """The one icon search (icon_catalog.find): the library and the team's
    icons, best first, each with its score and how it was found; `strong`
    when the best is a sure match; `generate` says whether "Generate with
    AI" may be offered for the query (and why not). Free: nothing is drawn
    and no AI is asked. An empty query lists the 42 picks, then A-Z."""
    got = await icon_catalog.search(q, offset=max(0, offset), limit=max(1, limit))
    return {**got, "items": [_with_png(i) for i in got["items"]]}


@router.get("/icons/team")
async def icons_team(include_hidden: bool = False, limit: int = 60) -> dict:
    """The team's own icons (drawn by the AI on a press, or uploaded),
    newest first — shared by every machine."""
    try:
        rows = await icon_catalog.team_icons(limit, include_hidden=include_hidden)
    except Exception as e:  # noqa: BLE001 — the table can't be reached
        raise HTTPException(503, icon_catalog.TABLE_DOWN) from e
    items = [_team_item(r) for r in rows]
    return {"items": items, "total": len(items)}


def _team_item(row: dict) -> dict:
    """A team icon as the studio reads it: its picture is our PNG (never a
    file someone uploaded)."""
    return {**row, "source": "custom", "png_url": _glyph_url(row["id"])}


async def _upload_bytes(file: UploadFile) -> bytes:
    data = await file.read(icon_import.MAX_BYTES + 1)
    if len(data) > icon_import.MAX_BYTES:
        raise HTTPException(413, f"The file is larger than {icon_import.MAX_BYTES // (1024 * 1024)} MB")
    return data


@router.post("/icons/upload/preview")
async def icons_upload_preview(
    file: UploadFile = File(...),
    mode: Literal["auto", "shape", "marks"] = Form(default="auto"),
    label: Optional[str] = Form(default=None, max_length=icon_import.NAME_MAX * 3),
) -> dict:
    """An uploaded SVG or picture made into your team's kind of icon — one
    colour, cleaned to its main shapes, its strokes at the library's bold
    weight — and shown exactly as the videos will stamp it (the circle, and
    the chip with `label`'s words). Free: nothing is saved; saving the same
    file in the same mode stores these very bytes. `duplicate_of`: your team
    has this icon already. `mode`: "shape" keeps a plate the icon sits on,
    "marks" takes the marks off it (offered when report.plate is set)."""
    data = await _upload_bytes(file)
    try:
        return await icon_catalog.upload_preview(data, file.filename or "", mode=mode, label=label)
    except icon_import.UploadError as e:
        raise HTTPException(e.status, str(e)) from e


@router.post("/icons/upload", status_code=201)
async def icons_upload(
    response: Response,
    file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=icon_import.NAME_MAX * 3),
    tags: str = Form(default="", max_length=1000),
    mode: Literal["auto", "shape", "marks"] = Form(default="auto"),
) -> dict:
    """Save an uploaded icon to your team's icons (the database row holds
    its PNG: every machine and the VPS draw it; no bucket). 201 with the
    icon; 200 with `duplicate` when that very icon is there already (shown
    again if it had been hidden; its own name kept). The uploaded file is
    never served back."""
    data = await _upload_bytes(file)
    try:
        row, created = await icon_catalog.upload(data, file.filename or "", name=name, tags=tags, mode=mode)
    except icon_import.UploadError as e:
        raise HTTPException(e.status, str(e)) from e
    except icon_catalog.DrawError as e:
        headers = {"Retry-After": str(icon_catalog.DRAW_DOWN_SEC)} if e.status == 503 else None
        raise HTTPException(e.status, str(e), headers=headers) from e
    if not created:
        # The same answer, encoded like every other (its created_at is a
        # datetime from the database), with 200 for "already there".
        response.status_code = 200
    return {**_team_item(row), "duplicate": not created}


@router.patch("/icons/custom/{icon_id}")
async def icons_update(icon_id: str, body: IconUpdateRequest) -> dict:
    """Rename, re-tag, hide or show again one of your team's icons, for
    everyone. Its picture never changes, so versions that use it look the
    same (a chip's words are kept in each version)."""
    try:
        row = await icon_catalog.update(icon_id, name=body.name, label=body.label, tags=body.tags,
                                        hidden=body.hidden)
    except KeyError as e:
        raise HTTPException(404, "There's no such icon.") from e
    except icon_import.UploadError as e:
        raise HTTPException(e.status, str(e)) from e
    except Exception as e:  # noqa: BLE001 — the table can't be reached
        raise HTTPException(503, icon_catalog.TABLE_DOWN) from e
    return _team_item(row)


@router.post("/icons/generate")
async def icons_generate(body: GenerateRequest) -> dict:
    """"Generate with AI": the name drawn by the AI image model — the only
    paid icon call, made only on this press (a drawing of the name already
    made is returned at no cost; `again` makes a new one and hides the one
    shown). The answer carries the drawing as the circle and the chip will
    stamp it (`preview`) and how many drawings the team has left today."""
    try:
        got = await icon_catalog.draw(body.name, again=body.again)
    except icon_catalog.DrawError as e:
        headers = {"Retry-After": str(icon_catalog.DRAW_DOWN_SEC)} if e.status == 503 else None
        raise HTTPException(e.status, str(e), headers=headers) from e
    try:
        preview = icon_catalog.look_previews(await icon_catalog.glyph(got["id"]), got["name"])
    except Exception:  # noqa: BLE001 — the card falls back to its own drawing
        preview = {"circle": None, "chip": None}
    try:
        left: Optional[int] = await icon_catalog.draws_left()
    except Exception:  # noqa: BLE001
        left = None
    return {**got, "png_url": _glyph_url(got["id"]), "preview": preview, "left_today": left}


@router.get("/icons/glyph/{icon_id}")
async def icons_glyph(icon_id: str, weight: Literal["fill", "bold"] = "fill"):
    """A library icon as SVG (the path the run stamps, viewBox 256) in its
    fill or bold weight (the circle and chip icons draw bold), or a drawn
    one as its PNG (black on clear; one shape, whatever the weight)."""
    try:
        kind, data = await icon_catalog.glyph(icon_id, weight)
    except KeyError as e:
        raise HTTPException(404, "There's no such icon.") from e
    if kind == "svg":
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">'
               f'<path d="{html.escape(data, quote=True)}" fill="#000"/></svg>')
        return Response(svg, media_type="image/svg+xml", headers=_GLYPH_HEADERS)
    return Response(bytes(data), media_type="image/png", headers=_GLYPH_HEADERS)


@router.get("/icons/font/{font_key}.ttf")
async def icons_font(font_key: str):
    """A chip label font face by its fonts.json key ("jost-600@5376acf7"):
    the very TTF the server shapes the chip words with, for the studio's own
    drawing of a chip while its exact sprite is on its way."""
    try:
        path = label_font.face_file(font_key)
    except (KeyError, label_font.FontError) as e:
        raise HTTPException(404, "There's no such font.") from e
    if not path.is_file():
        raise HTTPException(404, "There's no such font.")
    return FileResponse(str(path), media_type="font/ttf", headers=_FONT_HEADERS)


@router.delete("/icons/custom/{icon_id}")
async def icons_hide(icon_id: str) -> dict:
    """Hide a team icon from the library, for everyone ("Discard" of a
    drawing). Its picture is kept: versions that use it still draw it, and
    generating the name again brings it back for free."""
    if not icon_catalog.is_custom_id(icon_id):
        raise HTTPException(404, "Only your team's own icons can be hidden.")
    if not await icon_catalog.hide(icon_id):
        try:
            await icon_catalog.glyph(icon_id)          # there, and hidden already
        except KeyError as e:
            raise HTTPException(404, "There's no such icon.") from e
    return {"id": icon_id, "hidden": True}


TEAM_READ_SEC = 3.0


async def _team_read() -> None:
    """The team's icons read before a plan is made (icon_catalog.team_index:
    at most every minute): the ones that are said are offered, and an added
    one lands where its words are said. Never in the plan's way — a table
    that can't be read in time keeps what was known."""
    try:
        await asyncio.wait_for(icon_catalog.team_index(), TEAM_READ_SEC)
    except Exception:  # noqa: BLE001 — the plan goes on with what is known
        pass


@router.post("/icons/plan")
async def icons_plan(body: IconPlanRequest) -> dict:
    """The recipe's icon plan on this scene (studio.icon_plan): which icons,
    on which words, where, in what colours — or "warming" while the scene's
    words, spots and the AI's choices are worked out (ask again each
    second). Free."""
    spec, _chain, bv = await _on(body)
    r = _recipe(body)
    _icon_rules(r, bv, generate=False)
    await _team_read()
    reorder = body.reorder.model_dump() if body.reorder is not None else None
    if spec is None:
        return await studio.icon_plan(body.run_id, body.target, r, ask_again=body.ask_again, reorder=reorder,
                                      **_end(body), **_variant(body))
    return await studio.icon_plan(body.run_id, body.target, r, spec, ask_again=body.ask_again,
                                  reorder=reorder, **_end(body), **_variant(body))


@router.post("/icons/moment")
async def icons_moment(body: MomentRequest) -> dict:
    """The plan's frame at `t` (scene seconds), made by the run's own code:
    the speaker pass that stamps the icons, the same matte, every frame
    decoded in order — on the scene, or (`version_id`) on that version's
    own parts. {url, frame, t}. 404 when the plan is not one this scene
    showed. Free: the local matte only. A draft made in house is made by
    the in-house engine's own frame (studio.moment: plate.plate_at)."""
    r = _recipe(body)
    if r.engine == "house":
        spec = _spec(body)
        if spec is None and body.version_id is not None:
            spec = hbase.BaseSpec(version_id=int(body.version_id), mode="stack")
        return await studio.moment(body.run_id, body.target, r, body.t, spec, plan_key=body.plan_key,
                                   **_end(body), **_variant(body))
    return await studio.icon_moment(body.run_id, body.target, r, body.plan_key, body.t,
                                    version_id=body.version_id, **_variant(body))


@router.get("/file/{rel:path}")
async def studio_file(rel: str):
    p = studio.resolve_file(rel)
    media = "video/mp4" if p.suffix.lower() == ".mp4" else f"image/{p.suffix.lower().lstrip('.')}"
    # An icon's sprite is named by its own bytes: it never changes.
    sprite = p.parent.name == studio.SPRITES and p.parent.parent.name == "icons"
    return FileResponse(str(p), media_type=media, headers=_FONT_HEADERS if sprite else
                        {"Cache-Control": "private, max-age=3600"})
