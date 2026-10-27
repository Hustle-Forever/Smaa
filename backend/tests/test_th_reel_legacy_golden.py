"""The reel's stitch as it is today: the oracle for the Rebrand-stage round.

yas_th_v2_composition_poller._kick_render (and, for a reel of more than one
scene, _kick_render_multi) turns a talking-head-v2 run into one POST /render
for Remotion. The round's S1-S3 (finalize again, one reel plan, a pick and an
end per scene) must leave that POST exactly as it is for every LEGACY row (no
render_owner, no composition_plan), and S2's plan builder must give the same
payload for a reel with no flash, no pick and no end whose files are found as
today. This drives today's code over synthetic rows and holds what it sends.

Each golden, tests/fixtures/th_reel_legacy_<case>.json, holds:
  run      the yas_generations row (id, metadata) handed to _kick_render;
  world    what the fakes answer from: each animation's file (null: not
           ready or not on disk) and duration_sec, each card's, upload's and
           asset's file, what an upload probes to, and Remotion's reply;
  golden   what today's code did: every POST (URL and JSON body, its keys in
           the order sent, ints and floats as sent), every metadata patch and
           every lookup, in order.
Made from today's code with TH_REEL_GOLDEN=write; asserted otherwise. A path
is shown relative: <DATA>/ for Remotion's /data mount, <ROOT>/ for
backend/data (a temp folder here), so a golden holds on any machine.

No database, and nothing written under backend/data: every get_conn the app
has loaded is a tripwire during a drive, except the poller's own, which
answers only the duration_sec SELECT (from the world); the metadata patches
are recorded, never written; the data root and COMP_DIR are a temp folder.

drive_legacy() is the one driver: S2's plan test compares its builder with
these goldens, and the round's read-only reel sweep (scratch) runs every real
row through it with a world read from the database."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from app import db as app_db
from app.workers import yas_th_v2_composition_poller as poller

FIX = Path(__file__).parent / "fixtures"
WRITE = os.environ.get("TH_REEL_GOLDEN") == "write"
DURATION_SQL = "SELECT duration_sec FROM yas_animations WHERE id = %s"
_REAL_CLIENT = httpx.AsyncClient
_REAL_GET_CONN = app_db.get_conn


class DbReached(AssertionError):
    pass


# ---------------------------------------------------------------------------
# The world the fakes answer from
# ---------------------------------------------------------------------------

class DictWorld:
    """A golden's world: files are paths relative to the data root (null:
    not found), looked up by id."""

    def __init__(self, world: dict, root: Path):
        self.w = world
        self.root = root

    def _file(self, table: str, key: Any) -> Optional[Path]:
        rel = (self.w.get(table) or {}).get(str(key))
        if isinstance(rel, dict):
            rel = rel.get("file")
        return self.root / rel if rel else None

    async def veo(self, anim_id: int) -> Optional[Path]:
        return self._file("anims", anim_id)

    async def card(self, card_id: int) -> Optional[Path]:
        return self._file("cards", card_id)

    async def upload(self, upload_id: int) -> Optional[Path]:
        return self._file("uploads", upload_id)

    async def asset(self, asset_id: int) -> Optional[Path]:
        return self._file("assets", asset_id)

    async def duration_row(self, anim_id: int) -> Optional[dict]:
        row = (self.w.get("anims") or {}).get(str(anim_id))
        return None if row is None else {"duration_sec": row.get("duration_sec")}

    def probe(self, path: Path) -> Optional[float]:
        return (self.w.get("probe") or {}).get(Path(path).relative_to(self.root).as_posix())

    def remotion(self, request: httpx.Request) -> httpx.Response:
        reply = self.w.get("remotion") or {"status": 200, "json": {"job_id": "job-golden"}}
        if "json" in reply:
            return httpx.Response(reply["status"], json=reply["json"])
        return httpx.Response(reply["status"], text=reply.get("text", ""))


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, drive: "_Drive"):
        self.drive = drive
        self.row: Optional[dict] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql: str, params: tuple = ()) -> None:
        if " ".join(sql.split()) != DURATION_SQL:
            self.drive.trip(f"the poller ran SQL other than the duration SELECT: {' '.join(sql.split())}")
        anim_id = int(params[0])
        self.drive.lookups.append(["duration", anim_id])
        self.row = await self.drive.world.duration_row(anim_id)

    async def fetchone(self) -> Optional[dict]:
        return self.row

    async def fetchall(self) -> list:
        self.drive.trip("the poller fetched rows (only the duration SELECT is expected)")
        return []


class _Conn:
    def __init__(self, drive: "_Drive"):
        self.drive = drive

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def cursor(self, *_a, **_k) -> _Cursor:
        return _Cursor(self.drive)


class _Drive:
    def __init__(self, world: Any):
        self.world = world
        self.lookups: list[list] = []
        self.posts: list[dict] = []
        self.patches: list[dict] = []
        self.tripped: list[str] = []

    def trip(self, why: str) -> None:
        self.tripped.append(why)
        raise DbReached(why)


def _tripwire(drive: _Drive):
    def get_conn(*_a, **_k):
        drive.trip("app.db.get_conn was called during a legacy drive")
    return get_conn


def _set(saved: list, obj: Any, name: str, value: Any) -> None:
    saved.append((obj, name, getattr(obj, name)))
    setattr(obj, name, value)


def drive_legacy(run: dict, world: Any, *, root: Path, comp_dir: Optional[Path] = None) -> dict:
    """Today's _kick_render on `run`, every lookup answered by `world` (see
    DictWorld for the methods it needs), the POST recorded, never sent. The
    data root is `root` and the reel goes under `comp_dir` (default: root's
    talking-head-v2 folder) — neither is written to beyond its run folder's
    mkdir. Returns {posts, patches, lookups} as sent (not normalised).
    Synchronous; restores everything it patched."""
    drive = _Drive(world)
    root = Path(root)
    comp_dir = Path(comp_dir) if comp_dir is not None else root / "yas_producer" / "talking-head-v2"

    async def veo(anim_id):
        drive.lookups.append(["veo", int(anim_id)])
        return await world.veo(int(anim_id))

    async def card(card_id):
        if not card_id:
            return None
        drive.lookups.append(["card", int(card_id)])
        return await world.card(int(card_id))

    async def upload(upload_id):
        if not upload_id:
            return None
        drive.lookups.append(["upload", int(upload_id)])
        return await world.upload(int(upload_id))

    async def asset(asset_id):
        if not asset_id:
            return None
        drive.lookups.append(["asset", int(asset_id)])
        return await world.asset(int(asset_id))

    def probe(path):
        drive.lookups.append(["probe", Path(path).relative_to(root).as_posix()
                              if Path(path).is_relative_to(root) else str(path)])
        return world.probe(Path(path))

    async def patch(run_id, body):
        drive.patches.append({"run_id": int(run_id), "patch": copy.deepcopy(body)})

    def handler(request: httpx.Request) -> httpx.Response:
        drive.posts.append({"method": request.method, "url": str(request.url),
                            "content_type": request.headers.get("content-type"),
                            "body": json.loads(request.content.decode("utf-8"))})
        return world.remotion(request)

    def client(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    trip = _tripwire(drive)
    saved: list = []
    try:
        # Every get_conn the app has bound (modules import it by name) is a
        # tripwire; the poller's own answers only the duration SELECT.
        for mod in list(sys.modules.values()):
            try:
                bound = mod is not None and mod is not poller and getattr(mod, "get_conn", None) is _REAL_GET_CONN
            except Exception:  # noqa: BLE001 — a lazy module that can't say is not the app's
                bound = False
            if bound:
                _set(saved, mod, "get_conn", trip)
        _set(saved, app_db, "get_pool", trip)
        _set(saved, app_db, "open_pool", trip)
        _set(saved, poller, "get_conn", lambda *a, **k: _Conn(drive))
        _set(saved, poller, "config", types.SimpleNamespace(asset_root=root / "assets", producer_assets_root=root / "assets" / "assets_library"))
        _set(saved, poller, "COMP_DIR", comp_dir)
        _set(saved, poller, "_fetch_veo_path", veo)
        _set(saved, poller, "_fetch_card_path", card)
        _set(saved, poller, "_fetch_upload_path", upload)
        _set(saved, poller, "_fetch_asset_path", asset)
        _set(saved, poller, "_probe_video_duration", probe)
        _set(saved, poller, "_patch_metadata", patch)
        _set(saved, poller, "httpx", types.SimpleNamespace(AsyncClient=client))
        asyncio.run(poller._kick_render(copy.deepcopy(run)))
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        # A module first imported during the drive bound the tripwire: it
        # gets the real one back too.
        for mod in list(sys.modules.values()):
            try:
                if mod is not None and getattr(mod, "get_conn", None) is trip:
                    setattr(mod, "get_conn", _REAL_GET_CONN)
            except Exception:  # noqa: BLE001
                pass
    if drive.tripped:
        raise DbReached("; ".join(drive.tripped))
    return {"posts": drive.posts, "patches": drive.patches, "lookups": drive.lookups}


def normalise(got: Any, *, root: Path, comp_dir: Optional[Path] = None) -> Any:
    """A drive's record with its machine's paths shown relative: Remotion's
    /data URLs as <DATA>/…, files under the data root as <ROOT>/… (under
    comp_dir, when it is elsewhere, <COMP_DIR>/…), the render URL as
    <REMOTION>/…, and composition_output_path (which the poller writes with
    this OS's separators) with "/"."""
    root = Path(root)

    def one(v: Any, key: str = "") -> Any:
        if isinstance(v, dict):
            return {k: one(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [one(x) for x in v]
        if not isinstance(v, str):
            return v
        if v.startswith(poller.DATA_URL_PREFIX + "/"):
            return "<DATA>/" + v[len(poller.DATA_URL_PREFIX) + 1:]
        if v.startswith(poller.REMOTION_BASE_URL + "/"):
            return "<REMOTION>/" + v[len(poller.REMOTION_BASE_URL) + 1:]
        for base, tag in ((comp_dir, "<COMP_DIR>"), (root, "<ROOT>")):
            if base is not None:
                try:
                    return f"{tag}/" + Path(v).relative_to(Path(base)).as_posix()
                except ValueError:
                    pass
        if key == "composition_output_path":
            return v.replace("\\", "/")
        return v

    return one(got)


# ---------------------------------------------------------------------------
# The cases (inputs only: what today's code does with them is the golden)
# ---------------------------------------------------------------------------

def _anim(i: int, dur: Optional[int] = 8, *, on_disk: bool = True) -> dict:
    return {"file": f"yas_producer/animations/anim-{i}.mp4" if on_disk else None, "duration_sec": dur}


def _card(run_id: int, i: int, side: str) -> str:
    return f"yas_producer/talking-head-v2/{run_id}/{side}-{i}.png"


def _refs(**kw) -> dict:
    base = {"bg_asset_id": None, "intro_asset_id": None, "outro_asset_id": None,
            "intro_video_upload_id": None, "outro_video_upload_id": None}
    return {**base, **kw}


def _single(run_id: int, **md) -> dict:
    base = {"aspect_ratio": "9:16", "composition_status": "rendering", "intro_excluded": False,
            "outro_excluded": False, "refs": _refs()}
    return {"id": run_id, "metadata": {**base, **md}}


def _multi(run_id: int, scenes: list[dict], **md) -> dict:
    base = {"aspect_ratio": "9:16", "composition_status": "rendering", "scene_count": len(scenes),
            "scenes": scenes, "intro_excluded": False, "outro_excluded": False, "refs": _refs()}
    return {"id": run_id, "metadata": {**base, **md}}


def _scene(i: int, anim_id: Optional[int], **kw) -> dict:
    return {"index": i, "animation_id": anim_id, "dialogue": f"Line {i + 1}.", **kw}


CASES: dict[str, dict] = {
    "single": {
        "about": "One scene, Veo intro and outro found (run 815's shape with every clip on disk).",
        "run": _single(9101, main_animation_id=351, veo_animation_id=351, intro_animation_id=352,
                       outro_animation_id=353, intro_image_id=816, outro_image_id=817),
        "world": {"anims": {"351": _anim(351), "352": _anim(352, 4), "353": _anim(353, 4)}},
    },
    "single_card_bookend": {
        "about": "Card-only intro (no Veo made); the outro's Veo row is ready but its file is not on this "
                 "disk (run 815's real case: a Mac path), so its card is used with the Veo's duration.",
        "run": _single(9102, main_animation_id=361, intro_image_id=903, outro_animation_id=363,
                       outro_image_id=904),
        "world": {"anims": {"361": _anim(361), "363": _anim(363, 4, on_disk=False)},
                  "cards": {"903": _card(9102, 903, "intro"), "904": _card(9102, 904, "outro")}},
    },
    "single_asset_fallback_16x9": {
        "about": "16:9; main only by the legacy veo_animation_id with no duration_sec (8 s default); the "
                 "intro's Veo and card are missing so its reference asset is used; the outro has nothing "
                 "(omitted, its duration still sent).",
        "run": _single(9103, aspect_ratio="16:9", veo_animation_id=371, intro_animation_id=372,
                       intro_image_id=905, refs=_refs(intro_asset_id=41)),
        "world": {"anims": {"371": _anim(371, None), "372": _anim(372, 4, on_disk=False)},
                  "cards": {"905": None}, "assets": {"41": "assets/refs/intro-41.png"}},
    },
    "single_excluded_bookends": {
        "about": "Both bookends excluded: main only; the bookends' durations are still looked up and sent.",
        "run": _single(9104, main_animation_id=381, intro_animation_id=382, outro_animation_id=383,
                       intro_image_id=906, outro_image_id=907, intro_excluded=True, outro_excluded=True),
        "world": {"anims": {"381": _anim(381), "382": _anim(382, 4), "383": _anim(383, 4)},
                  "cards": {"906": _card(9104, 906, "intro"), "907": _card(9104, 907, "outro")}},
    },
    "single_upload_bookend": {
        "about": "An uploaded intro video used as it is, at its probed length; the outro's upload is not "
                 "found, so its Veo is used.",
        "run": _single(9105, main_animation_id=391, intro_animation_id=392, outro_animation_id=393,
                       refs=_refs(intro_video_upload_id=51, outro_video_upload_id=52)),
        "world": {"anims": {"391": _anim(391), "392": _anim(392, 4), "393": _anim(393, 4)},
                  "uploads": {"51": "yas_producer/uploads/intro-51.mp4", "52": None},
                  "probe": {"yas_producer/uploads/intro-51.mp4": 5.312}},
    },
    "single_upload_probe_fails": {
        "about": "An uploaded outro whose probe fails: its Veo's duration_sec is sent instead.",
        "run": _single(9106, main_animation_id=401, outro_animation_id=403,
                       refs=_refs(outro_video_upload_id=53)),
        "world": {"anims": {"401": _anim(401, 8), "403": _anim(403, 4)},
                  "uploads": {"53": "yas_producer/uploads/outro-53.mov"},
                  "probe": {"yas_producer/uploads/outro-53.mov": None}},
    },
    "multi": {
        "about": "Three scenes (one with no duration_sec: 8 s), Veo intro and outro.",
        "run": _multi(9201, [_scene(0, 411), _scene(1, 412), _scene(2, 413)],
                      intro_animation_id=414, outro_animation_id=415),
        "world": {"anims": {"411": _anim(411), "412": _anim(412, None), "413": _anim(413, 4),
                            "414": _anim(414, 4), "415": _anim(415, 4)}},
    },
    "multi_excluded_scene": {
        "about": "16:9, three scenes with the middle one excluded (not looked up, not sent).",
        "run": _multi(9202, [_scene(0, 421), _scene(1, 422, excluded=True), _scene(2, 423)],
                      aspect_ratio="16:9", intro_animation_id=424, outro_animation_id=425),
        "world": {"anims": {"421": _anim(421), "422": _anim(422), "423": _anim(423),
                            "424": _anim(424, 4), "425": _anim(425, 4)}},
    },
    "multi_excluded_bookends": {
        "about": "Two scenes, both bookends excluded; their durations are still looked up and sent.",
        "run": _multi(9203, [_scene(0, 431), _scene(1, 432)], intro_animation_id=434, outro_animation_id=435,
                      intro_image_id=908, outro_image_id=909, intro_excluded=True, outro_excluded=True),
        "world": {"anims": {"431": _anim(431), "432": _anim(432), "434": _anim(434, 4), "435": _anim(435, 4)}},
    },
    "multi_card_bookend": {
        "about": "The intro's Veo file is missing, so its card; the outro has no Veo and no card, so its "
                 "reference asset.",
        "run": _multi(9204, [_scene(0, 441), _scene(1, 442)], intro_animation_id=444, intro_image_id=910,
                      outro_image_id=911, refs=_refs(outro_asset_id=42)),
        "world": {"anims": {"441": _anim(441), "442": _anim(442), "444": _anim(444, 4, on_disk=False)},
                  "cards": {"910": _card(9204, 910, "intro"), "911": None},
                  "assets": {"42": "assets/refs/outro-42.mp4"}},
    },
    "multi_upload_bookend": {
        "about": "An uploaded intro video on a multi-scene reel is used as it is, at its probed length "
                 "(as on a single-scene reel), not the Veo intro.",
        "run": _multi(9205, [_scene(0, 451), _scene(1, 452)], intro_animation_id=454, outro_animation_id=455,
                      refs=_refs(intro_video_upload_id=54)),
        "world": {"anims": {"451": _anim(451), "452": _anim(452), "454": _anim(454, 4), "455": _anim(455, 4)},
                  "uploads": {"54": "yas_producer/uploads/intro-54.mp4"},
                  "probe": {"yas_producer/uploads/intro-54.mp4": 6.0}},
    },
    "single_main_missing": {
        "about": "The main clip is not on disk: nothing is sent and nothing patched (retried next tick).",
        "run": _single(9107, main_animation_id=461, intro_animation_id=462),
        "world": {"anims": {"461": _anim(461, on_disk=False), "462": _anim(462, 4)}},
    },
    "multi_scene_missing": {
        "about": "A scene's clip is not on disk: nothing is sent and nothing patched (retried next tick).",
        "run": _multi(9206, [_scene(0, 471), _scene(1, 472)], intro_animation_id=474),
        "world": {"anims": {"471": _anim(471), "472": _anim(472, on_disk=False), "474": _anim(474, 4)}},
    },
    "multi_no_included": {
        "about": "Every scene excluded: failed, nothing sent.",
        "run": _multi(9207, [_scene(0, 481, excluded=True), _scene(1, 482, excluded=True)],
                      intro_animation_id=484),
        "world": {"anims": {"481": _anim(481), "482": _anim(482), "484": _anim(484, 4)}},
    },
    "single_remotion_500": {
        "about": "Remotion answers 500: failed with its words.",
        "run": _single(9108, main_animation_id=491),
        "world": {"anims": {"491": _anim(491)}, "remotion": {"status": 500, "text": "renderer busy"}},
    },
}


def _path(case: str) -> Path:
    return FIX / f"th_reel_legacy_{case}.json"


def _dump(value: Any) -> str:
    """Order-keeping, type-keeping JSON: 8 and 8.0 differ, as they do on the
    wire."""
    return json.dumps(value, indent=2, ensure_ascii=False)


def _drive_case(case: str, tmp_path: Path) -> dict:
    spec = CASES[case]
    root = tmp_path / "data"
    got = drive_legacy(spec["run"], DictWorld(spec["world"], root), root=root)
    return normalise(got, root=root)


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    from app.services import vertex_client

    async def refuse(*_a, **_k):
        raise AssertionError("a paid model call in a test")

    for name in ("gemini_text", "gemini_multimodal", "gemini_image", "gemini_embed", "veo_generate"):
        if hasattr(vertex_client, name):
            monkeypatch.setattr(vertex_client, name, refuse)


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_legacy_stitch_sends_exactly_what_it_sent(case, tmp_path):
    got = _drive_case(case, tmp_path)
    spec = CASES[case]
    body = {"case": case, "about": spec["about"], "run": spec["run"], "world": spec["world"], "golden": got}
    if WRITE:
        _path(case).write_text(_dump(body) + "\n", encoding="utf-8")
    assert _path(case).is_file(), f"no golden for {case}: run with TH_REEL_GOLDEN=write on untouched code"
    saved = json.loads(_path(case).read_text(encoding="utf-8"))
    # The inputs are the case's (a golden is never edited by hand) …
    assert _dump(saved["run"]) == _dump(spec["run"]) and _dump(saved["world"]) == _dump(spec["world"])
    # … and today's code does exactly what it did: the POST byte for byte as
    # JSON (key order, int vs float), the patches, every lookup in order.
    assert _dump(got["posts"]) == _dump(saved["golden"]["posts"])
    assert _dump(got["patches"]) == _dump(saved["golden"]["patches"])
    assert got["lookups"] == saved["golden"]["lookups"]


def test_every_golden_is_a_case():
    on_disk = {p.stem[len("th_reel_legacy_"):] for p in FIX.glob("th_reel_legacy_*.json")}
    assert on_disk == set(CASES)


def test_the_goldens_cover_the_rounds_list():
    """Single, multi, an excluded scene, excluded bookends, a card-only
    bookend and an uploaded bookend (single and multi) — each sends a
    render, as its golden shows."""
    def post(case):
        return json.loads(_path(case).read_text(encoding="utf-8"))["golden"]["posts"][0]

    assert post("single")["body"]["template"] == "talking-head-v2-with-cards"
    assert post("multi")["body"]["template"] == "talking-head-v2-multi-scene"
    assert len(post("multi_excluded_scene")["body"]["props"]["scenes"]) == 2
    for case in ("single_excluded_bookends", "multi_excluded_bookends"):
        props = post(case)["body"]["props"]
        assert props["intro_video_path"] is None and props["outro_video_path"] is None
    assert post("single_card_bookend")["body"]["props"]["intro_video_path"].endswith("/intro-903.png")
    assert post("single_upload_bookend")["body"]["props"]["intro_duration_sec"] == 5.312
    assert post("multi_upload_bookend")["body"]["props"]["intro_video_path"].endswith("/intro-54.mp4")
    assert post("multi_upload_bookend")["body"]["props"]["intro_duration_sec"] == 6.0


def test_a_drive_never_reaches_the_database(tmp_path):
    """The tripwire works: any SQL but the duration SELECT, or any other
    get_conn, stops the drive — and the shared pool is never opened."""
    root = tmp_path / "data"
    spec = CASES["single"]

    class Nosy(DictWorld):
        async def veo(self, anim_id):
            from app.services import yas_talking_head_v2 as th

            async with th.get_conn() as conn:           # any other module's get_conn
                await conn.execute("SELECT 1")
            return None

    with pytest.raises(DbReached):
        drive_legacy(spec["run"], Nosy(spec["world"], root), root=root)

    class Chatty(DictWorld):
        async def veo(self, anim_id):
            async with poller.get_conn() as conn:       # the poller's own, other SQL
                async with conn.cursor() as cur:
                    await cur.execute("SELECT local_path, status FROM yas_animations WHERE id = %s", (anim_id,))
            return None

    with pytest.raises(DbReached):
        drive_legacy(spec["run"], Chatty(spec["world"], root), root=root)
    # Everything patched is put back, and the pool was never opened.
    assert poller.get_conn is _REAL_GET_CONN and app_db.get_conn is _REAL_GET_CONN
    assert th_get_conn() is _REAL_GET_CONN and poller._fetch_veo_path.__module__ == poller.__name__
    assert app_db._pool is None
    # A full drive writes nothing but its run folder, under the temp root.
    got = drive_legacy(spec["run"], DictWorld(spec["world"], root), root=root)
    assert len(got["posts"]) == 1 and got["posts"][0]["body"]["output_path"].startswith(str(root))
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == [
        "yas_producer", "yas_producer/talking-head-v2", "yas_producer/talking-head-v2/9101"]


def th_get_conn():
    from app.services import yas_talking_head_v2 as th

    return th.get_conn
