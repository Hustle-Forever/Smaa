"""Rebrand stage: the reel plan Finalize freezes (services/th_reel.resolve)
against today's stitch (the S0 goldens, tests/fixtures/th_reel_legacy_*.json).

For a reel with no flash, no pick and no end whose files are found as today,
the plan's /render payload is today's POST body byte for byte (key order,
int vs float) apart from output_path, which the poller adds (final-<seq>.mp4);
where today's stitch sent nothing (a clip not on disk: retried every tick;
every scene excluded: failed) the plan is a 409 naming the scene. A main Veo
scene ending on a tail flash differs in its length only: its frames up to
the cut, as seconds_for(F), whose rounding the templates undo exactly (a
Python mirror over k = 1..9 segments). One file lookup (locate_anim) finds a
clip a row names on another machine's path.

No database, no Remotion, no paid call: the goldens' worlds, in a temp root.
"""
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Optional

import pytest
from fastapi import HTTPException

from app.services import th_reel
from app.workers import yas_th_v2_composition_poller as poller
from test_th_reel_legacy_golden import CASES, DictWorld, _dump, _path, normalise

MAC = "/Users/apple/Desktop/42-studio/backend/data/yas_producer/animations"


class GoldenIo:
    """The golden's world as the reel's lookups see it: each Veo row ready,
    at its file under the root (made, so it exists), or — not on disk — at
    a teammate's Mac path (run 815's real case)."""

    def __init__(self, world: dict, root: Path):
        self.w = world
        self.dict_world = DictWorld(world, root)
        self.root = root
        for table in ("anims", "cards", "uploads", "assets"):
            for rel in (world.get(table) or {}).values():
                rel = rel.get("file") if isinstance(rel, dict) else rel
                if rel:
                    p = root / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"x")

    async def anim(self, anim_id: int) -> Optional[dict]:
        row = (self.w.get("anims") or {}).get(str(anim_id))
        if row is None:
            return None
        lp = str(self.root / row["file"]) if row.get("file") else f"{MAC}/anim-{anim_id}.mp4"
        return {"id": anim_id, "status": "ready", "local_path": lp, "duration_sec": row.get("duration_sec")}

    async def card(self, card_id: int) -> Optional[Path]:
        return await self.dict_world.card(card_id)

    async def upload(self, upload_id: int) -> Optional[Path]:
        return await self.dict_world.upload(upload_id)

    async def asset(self, asset_id: int) -> Optional[Path]:
        return await self.dict_world.asset(asset_id)

    def probe(self, path: Path) -> Optional[float]:
        return self.dict_world.probe(path)

    async def bucket_anim(self, anim_id: int, row: Optional[dict]) -> Optional[Path]:
        return None

    # Each clip decodes as a clean Veo clip at 24 fps unless `flash` names
    # its file: {file name: the first frame of its tail flash}.
    flash: dict[str, int] = {}

    def facts(self, path: Path) -> th_reel.Facts:
        return th_reel.Facts(frames=192, fps=24.0, cut=self.flash.get(Path(path).name))


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "data"
    monkeypatch.setattr(poller, "config", types.SimpleNamespace(asset_root=r / "assets", producer_assets_root=r / "assets" / "assets_library"))
    monkeypatch.setattr(poller, "COMP_DIR", r / "yas_producer" / "talking-head-v2")
    return r


def _golden(case: str) -> dict:
    return json.loads(_path(case).read_text(encoding="utf-8"))["golden"]


def _plan(case: str, root: Path, flash: Optional[dict] = None) -> th_reel.Plan:
    spec = CASES[case]
    io = GoldenIo(spec["world"], root)
    io.flash = dict(flash or {})
    return asyncio.run(th_reel.resolve(spec["run"], io))


SENT = sorted(c for c in CASES if _golden(c)["posts"])
NOT_SENT = sorted(c for c in CASES if not _golden(c)["posts"])


@pytest.mark.parametrize("case", SENT)
def test_the_plan_sends_what_todays_stitch_sent(case, root):
    plan = _plan(case, root)
    want = dict(_golden(case)["posts"][0]["body"])
    want.pop("output_path")
    got = normalise(plan.payload, root=root)
    assert _dump(got) == _dump(want)
    # The frames the plan counts are the template's own (calculateMetadata).
    assert plan.total_frames == th_reel.expected_frames(plan.payload)


@pytest.mark.parametrize("case, words", [
    ("single_main_missing", "Main's clip isn't on this computer."),
    ("multi_scene_missing", "Main 2's clip isn't on this computer."),
    ("multi_no_included", "Every scene is excluded"),
])
def test_where_today_sent_nothing_the_plan_names_the_scene(case, words, root):
    assert case in NOT_SENT
    with pytest.raises(HTTPException) as err:
        _plan(case, root)
    assert err.value.status_code == 409 and err.value.detail.startswith(words)


def test_every_golden_is_covered():
    assert set(SENT) | {"single_main_missing", "multi_scene_missing", "multi_no_included"} == set(CASES)


@pytest.mark.parametrize("case, frames", [
    ("single", [120, 240, 120]),
    ("multi", [120, 240, 240, 120, 120]),
    ("multi_excluded_scene", [120, 240, 240, 120]),
    ("single_excluded_bookends", [240]),
    ("single_upload_bookend", [159, 240, 120]),
])
def test_the_plan_counts_each_segment_and_the_reel(case, frames, root):
    plan = _plan(case, root)
    assert [s.frames for s in plan.segments] == frames
    seams = len(frames) - 1
    if case != "single_upload_bookend":
        assert plan.total_frames == sum(frames) - 12 * seams
    frozen = plan.frozen(3)
    assert frozen["seq"] == 3 and frozen["total_frames"] == plan.total_frames
    assert [s["frames"] for s in frozen["segments"]] == frames
    assert all(not Path(s["file_rel"]).is_absolute() for s in frozen["segments"])


def test_the_inputs_hash_is_stable_and_names_what_goes_in(root):
    spec = CASES["single"]
    io = GoldenIo(spec["world"], root)
    targets = asyncio.run(th_reel.resolve_sources(spec["run"], io))
    body = th_reel.inputs(spec["run"]["metadata"], targets)
    assert body["segments"] == [["intro", "anim:352", False, None, None], ["scene_0", "anim:351", False, None, None],
                                ["outro", "anim:353", False, None, None]]
    assert body["template"] == "talking-head-v2-with-cards" and body["aspect"] == "9:16"
    shuffled = json.loads(json.dumps({k: body[k] for k in reversed(list(body))}))
    assert th_reel.inputs_sha(shuffled) == th_reel.inputs_sha(body)
    md = dict(spec["run"]["metadata"], outro_excluded=True)
    other = th_reel.inputs(md, asyncio.run(th_reel.resolve_sources({"metadata": md}, io)))
    assert other["segments"][2] == ["outro", None, True, None, None]
    assert th_reel.inputs_sha(other) != th_reel.inputs_sha(body)


# ---------------------------------------------------------------------------
# S2: the Veo tail flash cut, and the duration rule
# ---------------------------------------------------------------------------

def _mains(body: dict) -> list:
    p = body["props"]
    return [s["duration_sec"] for s in p["scenes"]] if "scenes" in p else [p["main_duration_sec"]]


@pytest.mark.parametrize("case", SENT)
def test_a_flash_changes_only_that_scenes_length(case, root):
    spec = CASES[case]
    anims = spec["world"]["anims"]
    md = spec["run"]["metadata"]
    if th_reel.is_multi(md):
        mains = [str(sm["animation_id"]) for sm in md["scenes"] if not sm.get("excluded")]
    else:
        mains = [str(md.get("main_animation_id") or md.get("veo_animation_id"))]
    # Every main clip ends on a 4-frame flash from source frame 188 (24 fps):
    # the reel keeps its first 235 frames. A bookend "flashing" is never cut.
    flash = {Path(anims[a]["file"]).name: 188 for a in mains}
    flash.update({Path(anims[a]["file"]).name: 90 for a in anims if a not in mains and anims[a].get("file")})
    plan = _plan(case, root, flash)
    got = normalise(plan.payload, root=root)
    want = dict(_golden(case)["posts"][0]["body"])
    want.pop("output_path")
    assert _mains(got) == [(235 - 0.1) / 30] * len(mains)
    for body in (got, want):
        if "scenes" in body["props"]:
            body["props"]["scenes"] = [{**s, "duration_sec": None} for s in body["props"]["scenes"]]
        else:
            body["props"]["main_duration_sec"] = None
    assert _dump(got) == _dump(want)                         # nothing else moved, bookends included
    mains_seg = [s for s in plan.segments if s.role == "main"]
    assert [s.frames for s in mains_seg] == [235] * len(mains)
    assert all(s.frames == th_reel.js_round(float(s.seconds) * 30) for s in plan.segments)
    seams = len(plan.segments) - 1
    if case != "single_upload_bookend":                      # an upload's probed 5.312 s isn't whole frames
        assert plan.total_frames == sum(s.frames for s in plan.segments) - 12 * seams
    first_main = [s.role for s in plan.segments].index("main")
    assert plan.frozen(1)["segments"][first_main]["end"] == {
        "src_fps": 24.0, "src_frames": 192, "full": 240, "auto": 235, "user": None}


def test_case_a_815s_reel_is_451_frames(root):
    """815: Veo intro and outro, main flashing from source frame 188 of 192
    at 24 fps: 120 + 235 + 120 - 24 = 451 (today's reel, untrimmed: 456)."""
    plan = _plan("single", root, {"anim-351.mp4": 188})
    assert [s.frames for s in plan.segments] == [120, 235, 120]
    assert plan.total_frames == 451
    assert plan.payload["props"]["main_duration_sec"] == (235 - 0.1) / 30
    assert _plan("single", root).total_frames == 456


def test_a_cut_is_never_shorter_than_a_second(root):
    plan = _plan("single", root, {"anim-351.mp4": 10})      # 10 frames at 24 fps = 12 at 30
    main = next(s for s in plan.segments if s.role == "main")
    assert main.frames == th_reel.MIN_FRAMES == 30 and main.seconds == (30 - 0.1) / 30


def test_the_frames_survive_the_templates_rounding_for_1_to_9_segments():
    """A Python mirror of the templates: each segment Math.round(sec * 30),
    calculateMetadata Math.ceil(sum(sec) - seams * 0.4) — the -0.1 bias
    gives back exactly the frames, for any mix of lengths."""
    import random

    rng = random.Random(4242)
    for k in range(1, 10):
        for _ in range(3000):
            frames = [rng.randint(30, 900) for _ in range(k)]
            secs = [th_reel.seconds_for(f) for f in frames]
            assert [th_reel.js_round(s * 30) for s in secs] == frames
            body = {"template": th_reel.TEMPLATE_MULTI, "props": {
                "intro_video_path": None, "outro_video_path": None, "fade_sec": 0.4,
                "scenes": [{"path": f"s{i}", "duration_sec": s} for i, s in enumerate(secs)]}}
            assert th_reel.expected_frames(body) == sum(frames) - 12 * (k - 1), frames
            if k <= 3:                                        # the single-scene template: [intro] main [outro]
                single = {"template": th_reel.TEMPLATE_SINGLE, "props": {
                    "intro_video_path": "i" if k >= 2 else None, "intro_image_path": None,
                    "outro_video_path": "o" if k == 3 else None, "outro_image_path": None,
                    "intro_duration_sec": secs[0] if k >= 2 else 4.0,
                    "main_duration_sec": secs[1] if k >= 2 else secs[0],
                    "outro_duration_sec": secs[2] if k == 3 else 4.0, "fade_sec": 0.4}}
                assert th_reel.expected_frames(single) == sum(frames) - 12 * (k - 1), frames
    # Plain F / 30 does drift (why the bias exists).
    drift = sum(1 for f in range(30, 900) for g in range(30, 60)
                if th_reel.expected_frames({"template": th_reel.TEMPLATE_MULTI, "props": {
                    "fade_sec": 0.4, "scenes": [{"path": "a", "duration_sec": f / 30},
                                                {"path": "b", "duration_sec": g / 30}]}}) != f + g - 12)
    assert drift > 0


def test_facts_of_a_clip():
    f = th_reel.Facts(frames=192, fps=24.0, cut=188)
    assert (f.full, f.auto_end) == (240, 235)
    assert th_reel.Facts(frames=192, fps=24.0, cut=186).auto_end == 232       # run 800's anim-336
    assert th_reel.Facts(frames=239, fps=30.0, cut=None).auto_end is None
    assert th_reel.Facts(frames=239, fps=30.0, cut=None).full == 239


# ---------------------------------------------------------------------------
# One file lookup
# ---------------------------------------------------------------------------

def _row(anim_id: int, lp: Optional[str], status: str = "ready", **kw) -> dict:
    return {"id": anim_id, "status": status, "local_path": lp, "duration_sec": 8, **kw}


def test_locate_anim_tries_the_rows_path_then_animations_then_the_cache(root):
    anims = root / "yas_producer" / "animations"
    cache = root / "yas_producer" / "talking-head-v2" / "rebrand" / "_cache"
    anims.mkdir(parents=True)
    cache.mkdir(parents=True)
    for name in ("anim-342.mp4", "anim-351.mp4", "anim-250.mp4", "custom.mp4"):
        (anims / name).write_bytes(b"x")
    (cache / "anim-353.mp4").write_bytes(b"x")
    (cache / "anim-351.mp4").write_bytes(b"x")
    here = anims / "anim-351.mp4"
    assert th_reel.anim_dir() == anims and th_reel.cache_dir() == cache
    # Its own path first (the cache's copy of the same clip is not used).
    assert th_reel.locate_anim(351, _row(351, str(here))) == here
    assert th_reel.locate_anim(351, _row(351, "yas_producer/animations/anim-351.mp4")) == here
    # A teammate's Mac path: the same file name under animations/.
    assert th_reel.locate_anim(342, _row(342, f"{MAC}/anim-342.mp4")) == anims / "anim-342.mp4"
    assert th_reel.locate_anim(9, _row(9, "/Users/apple/x/custom.mp4")) == anims / "custom.mp4"
    # Docker's /app path, only the studio's cached copy here.
    assert th_reel.locate_anim(353, _row(353, "/app/backend/data/yas_producer/animations/anim-353.mp4")) == \
        cache / "anim-353.mp4"
    # Another name for the file (the library's copy): anim-<id>.mp4.
    assert th_reel.locate_anim(250, _row(250, "data/assets_library/yas/animations/250.mp4")) == anims / "anim-250.mp4"
    assert th_reel.locate_anim(250, _row(250, None)) == anims / "anim-250.mp4"
    # Not ready, not a row, nowhere: None.
    assert th_reel.locate_anim(342, _row(342, f"{MAC}/anim-342.mp4", status="generating")) is None
    assert th_reel.locate_anim(342, None) is None
    assert th_reel.locate_anim(777, _row(777, f"{MAC}/anim-777.mp4")) is None
    # A cache of the caller's own (the pipeline passes REBRAND_ROOT/_cache).
    other = root / "elsewhere"
    other.mkdir()
    (other / "anim-777.mp4").write_bytes(b"x")
    assert th_reel.locate_anim(777, _row(777, None), cache=other) == other / "anim-777.mp4"


def test_the_bucket_is_the_last_tier_and_leaves_a_copy_in_the_cache(root, monkeypatch):
    import httpx

    from app.services import asset_storage

    signed: list = []

    async def url_for(**kw):
        signed.append(kw["storage_path"])
        return "https://bucket.example/anim-777.mp4?sig=1"

    real = httpx.AsyncClient

    def client(*a, **kw):
        return real(*a, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"video")), **kw)

    monkeypatch.setattr(asset_storage, "url_for", url_for)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    row = _row(777, f"{MAC}/anim-777.mp4", storage_bucket="producer-assets", storage_path="yas/animations/777.mp4")
    got = asyncio.run(th_reel.fetch_anim(777, row))
    assert got == th_reel.cache_dir() / "anim-777.mp4" and got.read_bytes() == b"video"
    assert signed == ["yas/animations/777.mp4"]
    # Now it is found on disk: no second trip to the bucket.
    assert asyncio.run(th_reel.fetch_anim(777, row)) == got and len(signed) == 1
    assert asyncio.run(th_reel.fetch_anim(778, _row(778, None))) is None      # no bucket copy: None


def test_the_studio_finds_clips_by_the_same_lookup(root, monkeypatch):
    from app.services import rebrand_pipeline as pipeline

    anims = root / "yas_producer" / "animations"
    anims.mkdir(parents=True)
    (anims / "anim-352.mp4").write_bytes(b"x")

    async def anim_row(anim_id):
        return _row(anim_id, f"{MAC}/anim-{anim_id}.mp4")

    monkeypatch.setattr(pipeline, "_anim_row", anim_row)
    p, row = asyncio.run(pipeline._anim_video(352, root / "cache"))
    assert p == anims / "anim-352.mp4" and row["id"] == 352


def test_a_recovered_bookend_is_the_real_veo_clip(root):
    """Run 815's shape: a bookend row carries a Mac path and today's stitch
    fell back to the card. One lookup finds the Veo clip (intended: what the
    strip and the studio show)."""
    spec = CASES["single_card_bookend"]
    io = GoldenIo(spec["world"], root)
    (root / "yas_producer" / "animations" / "anim-363.mp4").write_bytes(b"x")    # the outro's clip, here after all
    plan = asyncio.run(th_reel.resolve(spec["run"], io))
    assert plan.payload["props"]["outro_video_path"].endswith("/yas_producer/animations/anim-363.mp4")
    assert [s.src for s in plan.segments] == ["card:903", "anim:361", "anim:363"]
