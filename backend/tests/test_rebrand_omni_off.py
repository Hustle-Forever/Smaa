"""Omni switched off for new work (I16 — the owner, 2026-09-30: "no need
Omni").

  the switch    REBRAND_OMNI is off unless set to on; off, a recipe for Omni
                is refused (rc.runnable) and one made in house never is
  the studio    every route that takes a recipe refuses one for Omni in plain
                words (400) before anything is drawn, planned or asked; the
                same draft made in house goes through
  the pipeline  no Omni run starts: the whole reel's experiment, the intro
                and outro cards, a scene from the classic panel (or the
                legacy path), a studio recipe for Omni — each a 400 saying
                what to use instead, before anything is written; the 42
                brand frame, a scene made in house and "Add on top" of a
                version Omni made start as ever
  the panel     the classic panel is told Omni is off (capabilities), so it
                hides it and says why
  old versions  a version Omni made (run 815's v25) still lists, plays,
                downloads, shows its still and is rated — and rating it now
                embeds nothing (the memory only ever fed Omni's brief)
  the gate      no paid call is reachable from the in-house engine, the
                studio or the icon modules — by reference, through every
                module they reach — but the two the owner asks for on
                purpose: the icon director (one call per scene, cached) and
                "Generate with AI" (on a press). The one fallback the
                pattern planner keeps for a run of its own (person_matte.
                person_mask when no mask is handed over, which can pay
                Gemini) is never taken: every pattern job they make carries
                its own local mask

Every test runs with REBRAND_OMNI unset (omni_default), as a server has it.
Every paid client raises; nothing touches the database, a bucket or a
server."""
from __future__ import annotations

import ast
import asyncio
import json
import subprocess
from collections import deque
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers import rebrand as rebrand_router
from app.routers import rebrand_harness as rt
from app.services import omni_client as omni
from app.services import pattern_library as lib
from app.services import person_matte
from app.services import rebrand_brief
from app.services import rebrand_media as media
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import speaker_front
from app.services import vertex_client
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio

pytestmark = pytest.mark.omni_default

BACKEND = Path(__file__).resolve().parents[1]
STORED = json.loads((Path(__file__).parent / "fixtures" / "rebrand_recipes" / "stored_815.json")
                    .read_text(encoding="utf-8"))["versions"]


@pytest.fixture(autouse=True)
def paid(monkeypatch, tmp_path):
    """Every paid client raises and is remembered, and the experiments table
    cannot be reached (a test fakes what it reads); the pipeline's folder is
    the test's own."""
    asked: list[str] = []

    def refusing(name):
        async def refuse(*_a, **_k):
            asked.append(name)
            raise AssertionError(f"a paid call in a test: {name}")
        return refuse

    for mod, name in ((omni, "edit_video"), (omni, "interpolate"), (vertex_client, "gemini_multimodal"),
                      (vertex_client, "gemini_image"), (vertex_client, "gemini_embed"),
                      (vertex_client, "gemini_text"), (person_matte, "person_mask"),
                      (rebrand_brief, "build_brief"), (rebrand_brief, "critique"), (store, "store_memory")):
        monkeypatch.setattr(mod, name, refusing(name))

    def no_database(name):
        async def refuse(*_a, **_k):
            raise AssertionError(f"the experiments table in a test: {name}")
        return refuse

    for name in ("create_experiment", "get_experiment", "list_for_run", "update_experiment", "patch_arm",
                 "touch"):
        monkeypatch.setattr(store, name, no_database(name))
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    return asked


def _app(*routers) -> TestClient:
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


async def _quiet(*_a, **_k):
    return None


def _library(monkeypatch) -> None:
    async def lookup(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "lookup", lookup)
    monkeypatch.setattr(lib, "get", lookup)


def _run_row(monkeypatch, composed: bool = True) -> None:
    async def run_row(run_id):
        return {"metadata": {"composition_status": pipeline.th.COMP_READY if composed else "draft"}}

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)


def _omni_row(vid: int, final: Path) -> dict:
    """A version Omni made, as its row reads: run 815's v25 recipe (two
    patterns and our frozen icons), one part, ready."""
    opts = {**pipeline.normalize_options({"target": "scene_0"}), "recipe": STORED[str(vid)]["recipe"],
            "icons_by_pipeline": True}
    arm = {"status": "ready", "final_path": str(final), "model": omni.OMNI_MODEL, "stages": {
        "omni": {"c0": {"key": "k", "path": str(final), "entering": True}},
        "guards": {"c0": {"key": None, "path": str(final)}}}}
    return {"id": vid, "run_id": 815, "status": "ready", "stage": "Done", "options": opts, "arms": {"omni": arm},
            "evidence": {"recipe_hash": STORED[str(vid)]["recipe_hash"]}, "created_at": None, "updated_at": None}


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

def test_omni_is_off_unless_it_is_switched_on(monkeypatch):
    assert not rc.omni_on(), "unset: off"
    with pytest.raises(rc.RecipeError) as e:
        rc.runnable(rc.preset_42_default("scene_0"))
    assert e.value.errors == [{"element_id": None, "field": "engine", "message": rc.OMNI_OFF}]
    rc.runnable(rc.preset_42_house("scene_0"))
    for on in ("on", "1", "true", "yes", " ON "):
        monkeypatch.setenv("REBRAND_OMNI", on)
        assert rc.omni_on(), on
    rc.runnable(rc.preset_42_default("scene_0"))
    for off in ("off", "0", "false", "no", "", "maybe"):
        monkeypatch.setenv("REBRAND_OMNI", off)
        assert not rc.omni_on(), off
    # What a new scene starts from, and every preset the studio lists, is
    # made in house.
    assert rt._default_recipe(815, "scene_0")["engine"] == "house"
    assert all(make().engine == "house" for make in rc.BUILTIN_PRESETS.values())


# ---------------------------------------------------------------------------
# The studio
# ---------------------------------------------------------------------------

STUDIO_ROUTES = {
    "/preview": {},
    "/versions": {},
    "/house/render": {},
    "/house/moment": {"t": 1.0},
    "/pattern/art": {"element_id": "pattern"},
    "/icons/plan": {},
    "/icons/moment": {"plan_key": "0" * 16, "t": 1.0},
}


def test_the_studio_refuses_a_recipe_for_omni_on_every_route(monkeypatch, paid):
    _library(monkeypatch)
    reached: list[str] = []

    def never(name):
        async def fail(*_a, **_k):
            reached.append(name)
            raise AssertionError(f"{name} was asked for a recipe for Omni")
        return fail

    for name in ("preview", "house_render", "moment", "pattern_art", "icon_plan", "icon_moment",
                 "final_patterns", "final_icon_plan", "load_base"):
        monkeypatch.setattr(studio, name, never(name))
    monkeypatch.setattr(pipeline, "start", never("start"))
    client = _app(rt.router)
    omni_recipe = rc.preset_42_default().model_dump(mode="json")
    for path, extra in STUDIO_ROUTES.items():
        r = client.post(f"/api/rebrand/harness{path}",
                        json={"run_id": 815, "target": "scene_0", "recipe": omni_recipe, **extra})
        assert r.status_code == 400, (path, r.text)
        assert r.json()["detail"]["message"] == rc.OMNI_OFF, path
        assert r.json()["detail"]["errors"] == [{"element_id": None, "field": "engine", "message": rc.OMNI_OFF}]
    # Built on a version too: refused before the version is even read.
    r = client.post("/api/rebrand/harness/versions", json={
        "run_id": 815, "target": "scene_0", "recipe": omni_recipe, "base": {"version_id": 25, "mode": "rebuild"}})
    assert r.status_code == 400 and r.json()["detail"]["message"] == rc.OMNI_OFF
    assert reached == [] and paid == []


def test_the_same_draft_made_in_house_is_previewed_and_generated(monkeypatch, paid):
    _library(monkeypatch)
    seen: dict = {}

    async def preview(run_id, target, recipe, base=None):
        seen["preview"] = recipe.engine
        return {"engine": recipe.engine}

    async def final_patterns(run_id, target, recipe, base=None):
        return {}

    async def start(run_id, raw, recipe=None, base=None):
        seen["start"] = recipe.engine
        return {"id": 5, "run_id": run_id, "status": "queued", "options": {"target": recipe.target}}

    monkeypatch.setattr(studio, "preview", preview)
    monkeypatch.setattr(studio, "final_patterns", final_patterns)
    monkeypatch.setattr(pipeline, "start", start)
    client = _app(rt.router)
    house = rc.preset_42_house().model_dump(mode="json")
    body = {"run_id": 815, "target": "scene_0", "recipe": house}
    assert client.post("/api/rebrand/harness/preview", json=body).json() == {"engine": "house"}
    r = client.post("/api/rebrand/harness/versions", json=body)
    assert r.status_code == 200 and r.json()["recipe"]["engine"] == "house"
    assert seen == {"preview": "house", "start": "house"} and paid == []


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, message", [
    ({"target": "reel", "run_omni": True, "run_brand": True}, pipeline.OMNI_OFF_REEL),
    ({"target": "reel", "run_omni": True}, pipeline.OMNI_OFF_REEL),
    ({"target": "intro"}, pipeline.OMNI_OFF_CARD),
    ({"target": "outro", "pattern_on": False}, pipeline.OMNI_OFF_CARD),
    ({"target": "scene_0"}, pipeline.OMNI_OFF_SCENE),
    ({"target": "scene_1", "path": "legacy"}, pipeline.OMNI_OFF_SCENE),
])
def test_no_omni_run_starts_from_the_classic_panel(monkeypatch, paid, raw, message):
    _run_row(monkeypatch, composed=False)
    # No provider either: the refusal says Omni is off, not that it is
    # missing — it comes first, before anything is read from the library or
    # written.
    monkeypatch.setattr(omni, "providers", lambda: [])
    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(815, raw))
    assert err.value.status_code == 400 and err.value.detail == message
    assert pipeline.omni_off_message(raw["target"]) == message
    assert paid == []


def test_the_classic_panel_is_told_omni_is_off_and_refused_with_what_to_use(monkeypatch, paid, tmp_path):
    _run_row(monkeypatch)
    final = tmp_path / "v.mp4"
    final.write_bytes(b"x")
    card = {**_omni_row(25, final), "id": 12}
    card["options"] = {**card["options"], "target": "intro"}

    async def listed(run_id, *, limit=20, target=None):
        return [card]

    monkeypatch.setattr(store, "list_for_run", listed)
    client = _app(rebrand_router.router)
    got = client.get("/api/rebrand/th-v2/815", params={"target": "intro"}).json()
    assert got["capabilities"]["omni"]["switched_off"] is True
    assert got["experiments"][0]["arms"]["omni"]["url"] == "/api/rebrand/experiments/12/file/omni/final"
    r = client.post("/api/rebrand/th-v2/815", json={"target": "intro"})
    assert r.status_code == 400 and r.json()["detail"] == pipeline.OMNI_OFF_CARD
    r = client.post("/api/rebrand/th-v2/815", json={"target": "reel", "run_omni": True, "run_brand": True})
    assert r.status_code == 400 and r.json()["detail"] == pipeline.OMNI_OFF_REEL
    monkeypatch.setenv("REBRAND_OMNI", "on")
    assert client.get("/api/rebrand/th-v2/815", params={"target": "intro"}).json()[
        "capabilities"]["omni"]["switched_off"] is False
    assert paid == []


def test_the_brand_frame_and_a_scene_made_in_house_still_start(monkeypatch, paid):
    _run_row(monkeypatch)
    _library(monkeypatch)
    created: list[dict] = []

    async def none(*_a, **_k):
        return []

    async def create(run_id, options):
        created.append(options)
        return {"id": 70 + len(created)}

    def no_provider():
        raise AssertionError("a run with no Omni needs no Omni provider")

    monkeypatch.setattr(omni, "providers", no_provider)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", _quiet)

    async def go():
        brand = await pipeline.start(815, {"target": "reel", "run_omni": False, "run_brand": True})
        house = await pipeline.start(815, None, recipe=rc.normalize(rc.preset_42_house("scene_0")))
        await asyncio.sleep(0)
        return brand, house

    brand, house = asyncio.run(go())
    assert (brand["id"], house["id"]) == (71, 72)
    assert created[0]["run_brand"] is True and created[0]["run_omni"] is False
    assert created[1]["engine"] == "house" and rc.normalize(created[1]["recipe"]).engine == "house"
    assert paid == []


def test_adding_on_top_of_a_version_omni_made_still_starts_for_nothing(monkeypatch, paid, tmp_path):
    """Generate of a pass on top of v25 (Omni's video kept under it), through
    the studio's own route, with Omni switched off."""
    _run_row(monkeypatch)
    _library(monkeypatch)
    final = tmp_path / "v25.mp4"
    final.write_bytes(b"x")
    v25 = _omni_row(25, final)
    created: dict = {}

    async def load(spec, run_id, target, **_k):
        return hbase.BaseVersion(spec=spec, exp=v25, chain=[v25], layers=[])

    async def load_base(run_id, target, spec):
        return await load(spec, run_id, target)

    async def final_patterns(run_id, target, recipe, base=None):
        assert base is not None and base.mode == "stack" and base.version_id == 25
        return {}

    async def none(*_a, **_k):
        return []

    async def create(run_id, options):
        created.update(options)
        return {"id": 90, "run_id": run_id, "status": "queued", "options": options}

    async def get(exp_id):
        return v25 if exp_id == 25 else None

    def no_provider():
        raise AssertionError("adding on top in house needs no Omni provider")

    monkeypatch.setattr(hbase, "load", load)
    monkeypatch.setattr(studio, "load_base", load_base)
    monkeypatch.setattr(studio, "final_patterns", final_patterns)
    monkeypatch.setattr(omni, "providers", no_provider)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(pipeline, "_run", _quiet)
    recipe = rc.Recipe(engine="house", target="scene_0", elements=[
        rc.Element(id="pattern-3", kind="pattern", props={"slug": "grid"})]).model_dump(mode="json")
    r = _app(rt.router).post("/api/rebrand/harness/versions", json={
        "run_id": 815, "target": "scene_0", "recipe": recipe, "base": {"version_id": 25, "mode": "stack"}})
    assert r.status_code == 200, r.text
    assert created["engine"] == "house" and created["base"] == {"version_id": 25, "mode": "stack"}
    assert rc.normalize(created["recipe"]).engine == "house"
    assert r.json()["base"] == {"version_id": 25, "mode": "stack"}
    assert paid == []


# ---------------------------------------------------------------------------
# Old versions
# ---------------------------------------------------------------------------

def _clip(path: Path, seconds: float = 2.0) -> Path:
    """A small real H.264 + AAC clip, as a version's video is."""
    subprocess.run([media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=size=180x320:rate=30:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate=44100:duration={seconds}", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   check=True, timeout=120)
    return path


def test_an_omni_version_still_lists_plays_downloads_shows_and_rates(monkeypatch, paid, tmp_path):
    _run_row(monkeypatch)
    final = _clip(tmp_path / "v25.mp4")
    v25 = _omni_row(25, final)
    saved: dict = {}

    async def listed(run_id, *, limit=20, target=None):
        return [{**v25, **saved}]

    async def get(exp_id):
        return {**v25, **saved} if exp_id == 25 else None

    async def update(exp_id, **fields):
        assert exp_id == 25
        saved.update(fields)

    # The local matte, faked (it is free, but slow to load).
    monkeypatch.setattr(speaker_front, "person_matte", lambda bgr: np.zeros(bgr.shape[:2], np.float32))
    monkeypatch.setattr(store, "list_for_run", listed)
    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(store, "update_experiment", update)
    client = _app(rt.router, rebrand_router.router)

    # Listed in the studio as it was: its own recipe (for Omni), its video, its still.
    (v,) = client.get("/api/rebrand/harness/versions", params={"run_id": 815, "target": "scene_0"}).json()[
        "versions"]
    assert v["id"] == 25 and v["recipe"] == STORED["25"]["recipe"] and v["recipe"]["engine"] == "omni"
    assert rc.recipe_hash(rc.normalize(v["recipe"])) == STORED["25"]["recipe_hash"]
    assert v["arms"]["omni"]["url"] == "/api/rebrand/experiments/25/file/omni/final" and v["lost"] is False
    assert v["stills"] == ["/api/rebrand/harness/versions/25/still/0"]
    # It plays and downloads, byte for byte.
    played = client.get(v["arms"]["omni"]["url"])
    assert played.status_code == 200 and played.content == final.read_bytes()
    down = client.get(v["arms"]["omni"]["download_url"])
    assert down.status_code == 200 and "attachment" in down.headers["content-disposition"]
    # Its still is its own settled frame.
    still = client.get(v["stills"][0])
    assert still.status_code == 200 and still.headers["content-type"] == "image/png"
    # Rated: saved with its lesson, nothing embedded (no brief reads it any more).
    r = client.post("/api/rebrand/experiments/25/rate", json={"omni_stars": 4, "notes": "the strip sits well"})
    assert r.status_code == 200 and r.json()["rating"]["omni_stars"] == 4
    assert "the strip sits well" in saved["lesson"]
    # Made again it is made in house (the studio's "Make it in house"): a
    # remake of Omni's own video is not offered.
    with pytest.raises(HTTPException) as err:
        asyncio.run(studio.remake(25))
    assert err.value.status_code == 409 and err.value.detail == hbase.REMAKE_OMNI
    assert paid == []


def test_rating_an_omni_version_embeds_again_only_when_omni_is_on(monkeypatch, paid, tmp_path):
    _run_row(monkeypatch)
    v25 = _omni_row(25, tmp_path / "v.mp4")
    embedded: list[int] = []

    async def get(exp_id):
        return v25

    async def update(exp_id, **fields):
        pass

    async def store_memory(exp_id, text):
        embedded.append(exp_id)

    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(store, "update_experiment", update)
    asyncio.run(pipeline.rate(25, winner=None, omni_stars=5, veo_stars=None, notes=""))
    monkeypatch.setattr(store, "store_memory", store_memory)
    monkeypatch.setenv("REBRAND_OMNI", "on")
    asyncio.run(pipeline.rate(25, winner=None, omni_stars=5, veo_stars=None, notes=""))
    assert embedded == [25] and paid == []


# ---------------------------------------------------------------------------
# The gate: no paid call reachable from the engine, the studio or the icons
# ---------------------------------------------------------------------------

APP = BACKEND / "app"
HARNESS = "app.services.rebrand_harness"
# The in-house engine, the studio and the icon modules.
GATED = [f"{HARNESS}.{m}" for m in (
    "house", "plate", "mattes", "camera", "studio",
    "elements.icon", "icon_render", "icon_catalog", "icon_import", "label_font", "speaker_track", "words",
    "director", "director_dryrun", "icon_feedback", "icon_parity", "icon_director_report", "guards",
)]
# Every paid client there is (a model or a paid embed), and the Omni
# pipeline's own brief, critique and memory.
PAID = {
    "app.services.omni_client": {"edit_video", "interpolate"},
    "app.services.vertex_client": {"gemini_text", "gemini_search", "gemini_multimodal", "gemini_embed",
                                   "gemini_image", "gemini_image_multipart", "veo_generate"},
    "app.services.person_matte": {"person_mask"},
    "app.services.rebrand_brief": {"build_brief", "critique"},
    "app.services.rebrand_store": {"store_memory"},
}
# Followed through: the whole harness, and every service module it reaches
# that could lead to a paid call.
FOLLOWED = ("rebrand_pattern_ref", "rebrand_media", "speaker_front", "person_matte", "pattern_library",
            "pattern_planner", "brand_icons", "rebrand_store", "rebrand_pipeline", "rebrand_brief",
            "asset_storage", "yas_talking_head_v2", "brand_overlay")
# The paid calls the owner asks for on purpose, each where it is made.
OWNERS = {
    (f"{HARNESS}.director", "_ask", "app.services.vertex_client.gemini_multimodal"),     # one per scene, cached
    (f"{HARNESS}.icon_catalog", "_draw_new", "app.services.vertex_client.gemini_image"),  # "Generate with AI"
}
# The pattern planner's fallback for a run that hands it no speaker mask:
# `job.person_mask or person_matte.person_mask`.
FALLBACKS = {(f"{HARNESS}.elements.pattern", "_plan", "app.services.person_matte.person_mask")}


def _modname(p: Path) -> str:
    parts = list(p.relative_to(BACKEND).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


class _Module:
    """One module's names: what each alias is, its functions, classes and
    methods, and what each of them refers to."""

    def __init__(self, name: str, path: Path, known: set[str]):
        self.name = name
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.pkg = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        self.mods: dict[str, str] = {}
        self.syms: dict[str, tuple[str, str]] = {}
        self.defs: dict[str, ast.AST] = {}
        self.classes: dict[str, list[str]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.mods[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                base = self._base(node)
                for a in node.names:
                    full = f"{base}.{a.name}" if base else a.name
                    if full in known:
                        self.mods[a.asname or a.name] = full
                    else:
                        self.syms[a.asname or a.name] = (base, a.name)
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.defs[node.name] = node
                self.classes[node.name] = []
                for b in node.body:
                    if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.defs[f"{node.name}.{b.name}"] = b
                        self.classes[node.name].append(f"{node.name}.{b.name}")

    def _base(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        parts = self.pkg.split(".")
        return ".".join(parts[: len(parts) - (node.level - 1)] + ([node.module] if node.module else []))

    def refs(self, key: str) -> set[tuple[str, str, bool]]:
        """(module, name, fallback) for every function, method or class
        `key` refers to — called or handed on; `fallback`: only as the
        right-hand side of `<something>.person_mask or ...`."""
        node = self.defs[key]
        cls = key.split(".")[0] if "." in key else None
        out: set[tuple[str, str, bool]] = set()
        if isinstance(node, ast.ClassDef):
            out |= {(self.name, m, False) for m in self.classes[node.name]}
            body = [b for b in node.body if not isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
        else:
            body = [*node.body, *node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
        fallback: set[int] = set()
        for stmt in body:
            for n in ast.walk(stmt):
                if (isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or)
                        and isinstance(n.values[0], ast.Attribute) and n.values[0].attr == "person_mask"):
                    fallback |= {id(x) for v in n.values[1:] for x in ast.walk(v)}
        for stmt in body:
            for n in ast.walk(stmt):
                got = None
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    got = (self.name, n.id) if n.id in self.defs else self.syms.get(n.id)
                elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                    v = n.value.id
                    if v in self.mods:
                        got = (self.mods[v], n.attr)
                    elif v in ("self", "cls") and cls:
                        got = (self.name, f"{cls}.{n.attr}")
                    elif v in self.classes:
                        got = (self.name, f"{v}.{n.attr}")
                if got is not None:
                    out.add((got[0], got[1], id(n) in fallback))
        return out


def _modules() -> dict[str, _Module]:
    known = {_modname(p) for p in APP.rglob("*.py")}
    files = {_modname(p): p for p in (APP / "services" / "rebrand_harness").rglob("*.py")}
    files.update({f"app.services.{m}": APP / "services" / f"{m}.py" for m in FOLLOWED
                  if (APP / "services" / f"{m}.py").exists()})
    return {n: _Module(n, p, known) for n, p in files.items()}


def _paid_reached(mods: dict[str, _Module]) -> dict[tuple[str, str, str, bool], str]:
    """{(module, function, paid call, fallback): the path from a gated
    module} for every paid call any gated function reaches."""
    seen: dict[tuple[str, str], tuple[str, str] | None] = {}
    todo: deque = deque()
    for g in GATED:
        for k in mods[g].defs:
            seen[(g, k)] = None
            todo.append((g, k))
    hits: dict[tuple[str, str, str, bool], str] = {}
    while todo:
        mod, name = todo.popleft()
        m = mods.get(mod)
        if m is None or name not in m.defs:
            continue
        for tmod, tname, fb in m.refs(name):
            if tname in PAID.get(tmod, ()):
                path, c = [f"{tmod}.{tname}"], (mod, name)
                while c is not None:
                    path.append(f"{c[0]}.{c[1]}")
                    c = seen[c]
                hits.setdefault((mod, name, f"{tmod}.{tname}", fb), " <- ".join(path))
                continue
            if (tmod, tname) not in seen:
                seen[(tmod, tname)] = (mod, name)
                todo.append((tmod, tname))
    return hits


def test_no_paid_call_is_reachable_from_the_engine_the_studio_or_the_icons():
    mods = _modules()
    assert set(GATED) <= set(mods), sorted(set(GATED) - set(mods))
    hits = _paid_reached(mods)
    owned = {(m, f, c) for (m, f, c, fb) in hits if not fb}
    unexpected = {k: p for k, p in hits.items() if (k[0], k[1], k[2]) not in (OWNERS if not k[3] else FALLBACKS)}
    assert not unexpected, "a paid call is reachable:\n" + "\n".join(unexpected.values())
    assert owned == OWNERS, "the icon director and Generate with AI are the only paid calls, each in its place"
    # No Omni call at all — not even behind the owner's two.
    assert not any(c.startswith("app.services.omni_client.") for (_m, _f, c, _fb) in hits)


def test_the_gate_sees_a_paid_call_where_there_is_one():
    """The gate is not blind: the Omni pipeline, followed from its own
    functions, reaches Omni, the brief, the critique and the memory."""
    mods = _modules()
    pl = mods["app.services.rebrand_pipeline"]
    reached = {(t, n) for k in pl.defs for (t, n, _fb) in pl.refs(k)}
    assert ("app.services.omni_client", "edit_video") in reached
    assert ("app.services.omni_client", "interpolate") in reached
    assert ("app.services.rebrand_brief", "build_brief") in reached
    runner = mods[f"{HARNESS}.runner"]
    assert any(t == "app.services.omni_client" and n == "edit_video"
               for k in runner.defs for (t, n, _fb) in runner.refs(k))


def test_every_pattern_job_the_engine_and_the_studio_make_carries_its_own_mask():
    """The planner's fallback (person_matte.person_mask, which can pay
    Gemini) is taken only by a job with no mask of its own: every job the
    in-house engine, the studio and the icons make hands the LOCAL one."""
    mods = _modules()
    made = []
    for g in GATED:
        for n in ast.walk(mods[g].tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "PatternJob"
                    or isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "PatternJob"):
                made.append((g, n.lineno, {k.arg for k in n.keywords}))
    assert made, "the studio makes pattern jobs"
    assert all("person_mask" in kw for (_g, _line, kw) in made), [(g, line) for g, line, kw in made
                                                                   if "person_mask" not in kw]
