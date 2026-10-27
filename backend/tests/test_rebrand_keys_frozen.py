"""The keys the Rebrand studio and the in-house engine cache by, frozen before
the Rebrand-stage round (S0).

The round adds a scene end (S9), a full-length variant (S10), bookends (S8)
and per-scene picks (S3). None of that may move a key for a main scene with
no user end: a moved key re-renders every draft, re-plans every icon and —
for the director — pays for the same answer again. Held here, as today's
code makes them:

  recipe_hash          rc.recipe_hash (what a version is stored under)
  spec memo            studio._spec_memo (a house spec, in memory)
  render key           house.render_key (a draft render / a version's frames)
  preview key          studio._preview_key (a preview folder)
  plan / timing key    the icon plan (elements/icon.plan: timing_key holds the
                       scene's duration, plan_key the place and colour)
  director inputs      director.key_inputs and director_key (the paid answer)
  house.render argv    the ffmpeg command, with no out-point

Two parts:
  * three fixture recipes on a synthetic scene: runs anywhere (the bundled
    pattern masks, fixed file bytes, the 815 words fixture, a synthetic
    speaker track);
  * run 815's real scene — its "_t188" studio folder, its words, its
    director's paid answer, its speaker track and its analysis — read only:
    copied into a temp folder, through the studio's own paths (icon_plan,
    house_spec), with every paid client, Whisper and MODNet refusing, so
    each input must come from its cache. Skipped when the files are not on
    this machine.

The pins are tests/fixtures/rebrand_keys_frozen.json, written from 9f788d3
with REBRAND_KEYS_FROZEN=write and asserted otherwise. Re-pinned on purpose
once: the icon tracks (elements/icon.py TRACKS, TRACK_RULES) show the
"Projects" hand_and_span adds, said at 7.0 s, which the old rules left out
("Said too close to the end"); a plan they change keys apart, so only its
fixtures plan_key and timing_key moved (every other key is the same). The
existing locks
stay where they are (the 42 default's hashes in test_rebrand_harness_recipe,
normalize's argv in test_rebrand_shot_cuts)."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import types
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import numpy as np
import pytest

from app import config as app_config
from app.services import omni_client as omni
from app.services import pattern_kit
from app.services import person_matte
from app.services import rebrand_media as media
from app.services import rebrand_pipeline as pipeline
from app.services import speaker_front
from app.services import vertex_client
from app.services import yas_talking_head_v2 as th
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import director as D
from app.services.rebrand_harness import house
from app.services.rebrand_harness import icon_catalog as ic
from app.services.rebrand_harness import plate as pl
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import speaker_track as T
from app.services.rebrand_harness import studio
from app.services.rebrand_harness import words as W
from app.services.rebrand_harness.elements import icon as I
from app.services.rebrand_harness.elements import pattern as pattern_el

cv2 = pytest.importorskip("cv2")
pytestmark = pytest.mark.omni_default          # as a server runs: Omni off

FIX = Path(__file__).parent / "fixtures"
PINS = FIX / "rebrand_keys_frozen.json"
WRITE = os.environ.get("REBRAND_KEYS_FROZEN") == "write"
# The fast model 815's director answer was made with (the key holds it).
MODEL = "gemini-2.5-flash"
LINE_815 = ("42 Abu Dhabi is where your future becomes uncoded: a tuition-free, peer-learning "
            "coding school without teachers, building real projects.")
TOPIC_815 = "42\nYour Future, Uncoded."
W_, H_ = 720, 1280

# Run 815's scene as the studio has it on this machine (read only).
ANIM_351 = th._YAS_ROOT / "animations" / "anim-351.mp4"
S815_NAME = "scene_0_84f81b9967_t188"
S815 = studio.STUDIO_ROOT / "815" / S815_NAME
S815_FILES = ("base/scene0.mp4", "base/scene0.cuts.json", "chunks.json", "c0_first.png", "icons/words.json",
              "icons/director.json", "icons/track.json", "icons/track.npz", "icons/track_frame0.png")
S815_OPTIONAL = ("c0_first.person.png", "colours.json", "icons/bg.npz")
HAVE_815 = ANIM_351.is_file() and all((S815 / f).is_file() for f in S815_FILES)
NO_815 = "run 815's studio folder (scene_0_84f81b9967_t188) or anim-351.mp4 is not on this machine"
ANALYSIS_ROOT_815 = studio.ANALYSIS_ROOT


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------

def _pinned(section: str, got: dict) -> None:
    """Assert `got` equals the pinned section — or, with
    REBRAND_KEYS_FROZEN=write, pin it."""
    body = json.loads(PINS.read_text(encoding="utf-8")) if PINS.is_file() else {}
    if WRITE:
        body[section] = got
        PINS.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert section in body, f"nothing pinned for {section}: run with REBRAND_KEYS_FROZEN=write on 9f788d3"
    want = body[section]
    moved = sorted(k for k in set(want) | set(got) if want.get(k) != got.get(k))
    assert not moved, f"{section}: moved {moved}\n got  {json.dumps({k: got.get(k) for k in moved})}\n" \
                      f" was  {json.dumps({k: want.get(k) for k in moved})}"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, default=str).encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Three fixture recipes
# ---------------------------------------------------------------------------

# withStartingIcons (frontend lib/rebrandIcons.ts): what a new scene opens with.
STARTING_ICONS = {"id": "icons", "kind": "icon", "enabled": True, "placement": {"anchor": "auto"},
                  "animation": {"entrance": "auto"}, "props": {"style": "circle"}}


def starting_draft(run_id: int, target: str) -> rc.Recipe:
    """The studio's first draft for a scene: the harness's default (the 42
    default made in house, the scene's seed) + the starting icons."""
    from app.routers import rebrand_harness as hr

    d = hr._default_recipe(run_id, target)
    return rc.normalize({**d, "preset": None, "elements": [*d["elements"], STARTING_ICONS]})


def hand_and_span(run_id: int, target: str) -> rc.Recipe:
    """Two patterns — the second moved, sized and turned by hand, in front,
    held on screen, timed on the timeline with an exit — and icons with one
    added, one removed and one icon's own look."""
    return rc.normalize({
        "engine": "house", "target": target, "seed": studio.default_seed(run_id, target),
        "colours": {"source": "brand"},
        "elements": [
            {"id": "pattern", "kind": "pattern", "animation": {"entrance": "slide", "duration_sec": 0.8},
             "props": {"slug": "structure", "layout": None, "colour": "auto"}},
            {"id": "pattern-2", "kind": "pattern", "z": "front",
             "placement": {"anchor": "edge-right", "offset": [0.04, -0.06], "size": 1.3, "rotate": 45.0,
                           "box": {"x": 0.62, "y": 0.2, "w": 0.3, "h": 0.5}},
             "animation": {"entrance": "fade", "start": {"mode": "at_sec", "sec": 1.5},
                           "end": {"mode": "at_sec", "sec": 6.0}, "exit": "fade", "exit_sec": 0.5},
             "props": {"slug": "beads_row", "colour": "#7f2bf2", "follow": "screen",
                       "layout": {"placement": "right", "scale": 0.5, "reach": 0.5, "anchor": "left",
                                  "rotate": 270}}},
            {"id": "icons", "kind": "icon", "placement": {"anchor": "auto"}, "animation": {"entrance": "pop"},
             "props": {"style": "circle",
                       "added": [{"glyph": {"source": "library", "id": "puzzle-piece"}, "label": "Projects"}],
                       "dismissed": ["users-three"],
                       "looks": {"piggy-bank": {"layer": "front", "colour": "#303030"}}}},
        ]})


def icons_only(run_id: int, target: str) -> rc.Recipe:
    """No pattern; icons without the AI (the keyword list), one look for
    all, a hold set by hand, the speaker not cut out."""
    return rc.normalize({
        "engine": "house", "target": target, "seed": "fixture:icons-only",
        "guards": {"speaker_front": False, "face": "warn"},
        "elements": [
            {"id": "icons", "kind": "icon", "placement": {"anchor": "left", "size": 0.2},
             "animation": {"entrance": "fade"},
             "props": {"ai": False, "style": "glyph", "colour": "#303030", "hold_sec": 2.0,
                       "added": [{"glyph": {"source": "library", "id": "code"}, "label": "Code"}],
                       "tweaks": {"code": {"hold_sec": 1.5}}}},
        ]})


RECIPES = {"starting_draft": starting_draft, "hand_and_span": hand_and_span, "icons_only": icons_only}

# Each pattern as Generate froze it (props.resolved), by element id: the
# places run 815's planner chose (tests/fixtures/rebrand_815_places.json).
FROZEN = {
    "pattern": {"layout": {"placement": "left", "scale": 2.0, "reach": 0.5, "anchor": "left", "rotate": 0},
                "colour": "#7f2bf2", "ground": "#f0f0f0", "ink": "#ffffff"},
    "pattern-2": {"layout": {"placement": "right", "scale": 0.5, "reach": 0.5, "anchor": "left", "rotate": 270},
                  "colour": "#7f2bf2", "ground": "#f0f0f0", "ink": "#ffffff", "follow": "screen"},
}
# One icon as Generate froze it (the badge 815's planner placed first).
ICONS = [{"glyph": {"source": "library", "id": "piggy-bank"}, "label": "Free", "from_sec": 2.8333,
          "settle_sec": 3.1, "to_sec": 4.3, "box": {"x": 0.04, "y": 0.295, "w": 0.16, "h": 0.09},
          "fill": "#7f2bf2", "ink": "#ffffff", "fx": {"entrance": "glitch", "seed": "studio:1:scene_0:piggy-bank"},
          "style": "badge", "word": "tuition", "purpose": "Why it helps", "max_overlap": 0.0}]


def frozen_patterns(recipe: rc.Recipe) -> rc.Recipe:
    return pattern_el.materialise(recipe, {e.id: FROZEN[e.id] for e in rc.pattern_elements(recipe)})


def fixture_words() -> dict:
    fx = json.loads((FIX / "icons_words" / "815.json").read_text(encoding="utf-8"))
    env = W.shape_envelope(fx["env"]["rms"], fx["env"]["thr"], fx["env"]["peak"], fx["env"]["voiced"])
    body = W.build_words(fx["chunks"], fx["dialogue"], None, fx["duration"], env=env)
    body["script"] = fx["dialogue"]
    return body


def _idea(w: dict, icon: str, text: str, *, priority: int, label: str) -> dict:
    by = {x["w"]: x for x in w["words"]}
    k = next(x["w"] for x in w["words"] if x["said"] and x["text"].lower() == text)
    return {"id": f"i{k}", "cand_id": None, "source": "ai", "concept": icon, "quote": by[k]["text"], "w": k,
            "w_end": k, "word": by[k]["text"], "s": by[k]["s"], "end": by[k]["e"],
            "mentions": [{"w": k, "w_end": k, "quote": by[k]["text"], "s": by[k]["s"]}], "table_mentions": [],
            "icon": icon, "label": label, "purpose": "Why", "priority": priority, "strong": True,
            "verdict": "keep", "reason": "", "list_span": False, "auto": True}


def fixture_director(w: dict) -> dict:
    """A scripted answer (no model): two ideas on 815's words."""
    ideas = [_idea(w, "piggy-bank", "tuition", priority=5, label="Free"),
             _idea(w, "code", "coding", priority=4, label="Code")]
    return {"version": D.DIRECTOR_VERSION, "key": "fixture", "source": "ai", "ideas": ideas,
            "room": {"spots": [], "home": None, "home_n": None, "home_why": ""}, "candidates": [], "note": ""}


def fixture_track(n: int = 17) -> T.Track:
    """A speaker standing still in a 720x1280 room (no MODNet)."""
    h8, w8 = H_ // 8, W_ // 8
    masks = np.zeros((n, h8, w8), np.uint8)
    masks[:, 70:, 25:65] = 255
    masks[:, 25:70, 30:60] = 255
    heads = np.tile(np.array([[240, 200, 480, 560]], np.int32), (n, 1))
    return T.Track(width=W_, height=H_, fps=30.0, frames=n * 15, step=15,
                   times=np.arange(n, dtype=np.float32) * 0.5, heads=heads, masks=masks, bars=None,
                   layout="full", picture=(0, H_, 0, W_), spots=T.spot_grid(W_, H_, None, "full"),
                   key="fixture-track")


@dataclasses.dataclass
class _Seconds:
    """What studio._chunks reads of a scene."""
    seconds: list


def fixture_plan(recipe: rc.Recipe, *, seconds: float = 7.8) -> Optional[I.IconPlan]:
    """The icon plan as studio._plan_ctx gathers its inputs (the duration,
    the chunks, the entrance and the zones, the seed, the face guard, the
    pattern preview not made yet: provisional, keyed by the preview key),
    on the fixture words, answer and track."""
    el = rc.icon_element(recipe)
    if el is None:
        return None
    words = fixture_words()
    seed = recipe.seed or studio.default_seed(1, "scene_0")
    els = rc.pattern_elements(recipe)
    pattern = (I.IconPattern(key=studio._preview_key(recipe, seed),
                             colour=pattern_el.colour_override(recipe.colours, els[0]), provisional=True)
               if els else None)
    duration = round(sum([seconds]), 3)
    ctx = I.IconCtx(
        words=words, director=fixture_director(words), track=fixture_track(), width=W_, height=H_,
        duration=duration, fps=I.FPS, chunks=studio._chunks(_Seconds([seconds])),
        enters=studio._icon_enters(recipe, None), zones=studio._icon_zones(recipe, None, duration),
        seed=seed, face=recipe.guards.face, pattern=pattern, backgrounds=None, base=None, notes=[],
        terms=studio.icon_terms([]), team=[])
    return I.plan(el, ctx)


def fixture_scene(work: PurePosixPath) -> studio.Scene:
    """A Scene as _spec_memo reads it (only its folder)."""
    return studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=W_, height=H_, work=work, ctx=None)


# ---------------------------------------------------------------------------
# Guards: nothing paid, nothing shared, nothing slow asked for
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def refusing(monkeypatch, tmp_path):
    """Every paid client refuses, and so do Whisper and MODNet (a key's
    inputs must come from their caches); the director's model is pinned;
    the bundled pattern masks are read (not this machine's kit); the team's
    icons are none; every studio folder and memo is the test's own."""
    asked: list[str] = []

    def refuse(name, is_async=True):
        async def _a(*_x, **_k):
            asked.append(name)
            raise AssertionError(f"{name} in a keys test")

        def _s(*_x, **_k):
            asked.append(name)
            raise AssertionError(f"{name} in a keys test")
        return _a if is_async else _s

    for mod, name in ((vertex_client, "gemini_text"), (vertex_client, "gemini_multimodal"),
                      (vertex_client, "gemini_image"), (vertex_client, "gemini_embed"),
                      (omni, "edit_video"), (omni, "interpolate"), (person_matte, "person_mask")):
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, refuse(f"{mod.__name__}.{name}"))
    monkeypatch.setattr(W, "transcribe", refuse("whisper", is_async=False))
    monkeypatch.setattr(speaker_front, "raw_matte", refuse("modnet raw_matte", is_async=False))
    monkeypatch.setattr(speaker_front, "person_matte", refuse("modnet person_matte", is_async=False))
    monkeypatch.setattr(app_config, "config", dataclasses.replace(app_config.config, vertex_gemini_fast_model=MODEL))
    monkeypatch.setattr(pattern_kit, "KIT_DIR", tmp_path / "no_kit")
    monkeypatch.setitem(ic._TEAM, "rows", [])
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "rebrand" / "_studio")
    monkeypatch.setattr(studio, "ANALYSIS_ROOT", tmp_path / "rebrand" / "_cache" / "scene")
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    for name in ("_PREPARED", "_ANALYSES", "_ICON_TASKS", "_HOUSE_SPECS", "_PREVIEWS", "_MASK_TASKS"):
        monkeypatch.setattr(studio, name, {})
    yield asked
    assert not asked, f"refused calls were made: {asked}"


# ---------------------------------------------------------------------------
# The fixture recipes (run anywhere)
# ---------------------------------------------------------------------------

async def _refs(recipe: rc.Recipe, out: Path) -> list[tuple]:
    job = pattern_el.PatternJob(exp_id=0, aspect="9:16", out_dir=out, intro=None, intro_is_still=False,
                                seed=recipe.seed or "", plan_frame=studio._no_frame, step=studio._quiet_step,
                                person_mask=None)
    pairs = []
    for el in rc.pattern_elements(recipe):
        res = await pattern_el.from_resolved(el, job)
        assert res.ref is not None, res.error
        pairs.append((el, res.ref))
    return pairs


def test_the_fixture_recipes_keys_are_frozen(tmp_path):
    source = tmp_path / "scene0.mp4"
    source.write_bytes(b"a scene clip's bytes, fixed")
    under = tmp_path / "under.mp4"
    under.write_bytes(b"a version's clip under the plate, fixed")
    got: dict[str, Any] = {}
    for name, make in RECIPES.items():
        draft = make(1, "scene_0")
        seed = draft.seed or studio.default_seed(1, "scene_0")
        stored = frozen_patterns(draft)
        if rc.icon_element(stored) is not None:
            el = rc.icon_element(stored)
            stored = rc.normalize(stored.model_copy(update={"elements": [
                e.model_copy(update={"props": {**e.props, "resolved": ICONS}}) if e.id == el.id else e
                for e in stored.elements]}))
        work = PurePosixPath("/studio/1/scene_0_0123456789_t188")
        got[f"{name}.recipe_hash"] = rc.recipe_hash(draft)
        got[f"{name}.stored_recipe_hash"] = rc.recipe_hash(stored)
        got[f"{name}.spec_memo"] = studio._spec_memo(fixture_scene(work), stored, None)
        got[f"{name}.spec_memo_on_v25"] = studio._spec_memo(fixture_scene(work), stored,
                                                             hbase.BaseSpec(version_id=25, mode="stack"))
        got[f"{name}.preview_key"] = studio._preview_key(draft, seed)
        got[f"{name}.preview_key_on_v25"] = studio._preview_key(draft, seed, base={"version_id": 25,
                                                                                   "mode": "stack"})
        p = fixture_plan(draft)
        got[f"{name}.plan_key"] = p.plan_key if p is not None else None
        got[f"{name}.timing_key"] = p.timing_key if p is not None else None
        pairs = asyncio.run(_refs(stored, tmp_path / "frozen" / name))
        spec = house.plate_spec(stored, pairs, width=360, height=640, fps=30.0, n=234, bars=None, stamper=None,
                                icons=ICONS if rc.icon_element(stored) is not None else ())
        got[f"{name}.render_key"] = house.render_key(spec, source=source, camera_key="camera-fixture",
                                                     bank_key="bank-fixture")
        got[f"{name}.render_key_under"] = house.render_key(spec, source=source, under=under,
                                                           camera_key="camera-fixture", bank_key="bank-fixture")
    _pinned("fixtures", got)


def test_the_director_key_inputs_are_frozen(tmp_path):
    frame0 = tmp_path / "track_frame0.png"
    frame0.write_bytes(b"frame 0 of a scene, fixed bytes")
    spots = [{"id": "tl", "box": {"x": 0.04, "y": 0.2, "w": 0.3, "h": 0.2}},
             {"id": "br", "box": {"x": 0.66, "y": 0.7, "w": 0.3, "h": 0.2}}]
    got: dict[str, Any] = {}
    for name, kw in (("plain", {}),
                     ("full", {"topic": TOPIC_815, "others": ["The second scene's line."], "frame0": frame0,
                               "spots": spots, "take": 1})):
        ctx = D.DirectorCtx(words=fixture_words(), script=LINE_815, work_dir=tmp_path / "icons", **kw)
        inputs = D.key_inputs(ctx, D.default_catalog())
        got[f"{name}.inputs"] = inputs
        got[f"{name}.inputs_digest"] = _digest(inputs)
        got[f"{name}.director_key"] = D.director_key(ctx, D.default_catalog())
    assert list(got["full.inputs"]) == ["version", "words", "script", "topic", "others", "catalog", "spots",
                                        "take", "model"]
    _pinned("director", got)


# ---------------------------------------------------------------------------
# house.render: the argv, with no out-point
# ---------------------------------------------------------------------------

class _Popen:
    seen: list = []

    def __init__(self, cmd, stdin=None, stderr=None):
        _Popen.seen.append({"cmd": list(cmd), "piped": 0})
        self._rec = _Popen.seen[-1]
        rec = self._rec

        class _In(io.BytesIO):
            def write(self, b):
                rec["piped"] += len(b)
                return len(b)

        self.stdin = _In()
        self.stderr = io.BytesIO(b"")

    def wait(self, timeout=None):
        Path(self._rec["cmd"][-1]).write_bytes(b"rendered")
        return 0

    def kill(self):
        pass


def test_house_render_runs_the_very_command_it_always_ran(tmp_path, monkeypatch):
    """The render's ffmpeg command, with no out-point: every frame piped (n
    plates of W x H x 4 bytes), -frames:v n, the source's sound copied —
    and with a version under the plate, that clip as the picture and the
    source still the sound. (S9 adds "-t" only when a user end cuts the
    scene short.)"""
    # The house module's own subprocess only (ffmpeg_exe and the rest keep theirs).
    monkeypatch.setattr(house, "subprocess", types.SimpleNamespace(Popen=_Popen, PIPE=subprocess.PIPE))
    _Popen.seen = []
    spec = pl.PlateSpec(width=16, height=32, fps=30.0, n=3)
    src, under = tmp_path / "scene0.mp4", tmp_path / "under.mp4"
    tags = "colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
    for kw, inputs, plates, audio in (({}, ["-i", str(src)], 1, "0:a?"),
                                      ({"under": under}, ["-i", str(under), "-i", str(src)], 2, "1:a?")):
        out = tmp_path / "out" / "draft.mp4"
        house.render(spec, src, out, **kw)
        tmp = out.with_name(f"draft.{os.getpid()}.{threading.get_ident()}.part.mp4")
        assert _Popen.seen[-1]["cmd"] == [
            media.ffmpeg_exe(), "-y", "-loglevel", "error", *inputs,
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "16x32", "-r", "30.000000", "-i", "-",
            "-filter_complex",
            f"[0:v]setparams={tags}[m];"
            f"[{plates}:v]scale=out_color_matrix=bt709:out_range=tv,format=yuva444p[p];"
            "[m][p]overlay=format=yuv444:alpha=straight:eof_action=pass,format=yuv420p[v]",
            "-map", "[v]", "-map", audio, "-frames:v", "3",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
        assert _Popen.seen[-1]["piped"] == 3 * 16 * 32 * 4
        assert out.read_bytes() == b"rendered" and not tmp.exists()
    assert house.COLOUR_TAGS == tags and (house.RENDER_CRF, house.RENDER_PRESET) == (18, "veryfast")


# ---------------------------------------------------------------------------
# Run 815's real scene (read only; skipped without it)
# ---------------------------------------------------------------------------

def _snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _scene_815() -> studio.Scene:
    """Run 815's scene as prepare() leaves it (no database: the run's words
    are its one line and its topic, as the reel gives them), on a copy of
    its studio folder under the test's own STUDIO_ROOT."""
    work = studio.STUDIO_ROOT / "815" / S815_NAME
    for rel in (*S815_FILES, *S815_OPTIONAL):
        if (S815 / rel).is_file():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(S815 / rel, work / rel)
    clip = work / "base" / "scene0.mp4"
    seg = pipeline.Segment(kind="scene", index=0, source=ANIM_351, is_still=False, base=clip)
    ctx = pipeline.Ctx(exp_id=0, run_id=815, aspect="9:16", width=W_, height=H_, work=work,
                       options=pipeline.normalize_options({"target": "scene_0"}),
                       run_ctx={"scene_dialogue": {0: LINE_815}, "topic": TOPIC_815, "target": "scene_0"},
                       intro=None, scenes=[seg], outro=None, logo=None)
    ctx.target_seg = seg
    info = media.scene_info(clip) or {}
    scene = studio.Scene(run_id=815, target="scene_0", aspect="9:16", width=W_, height=H_, work=work, ctx=ctx,
                         chunks=[clip], firsts=[work / "c0_first.png"],
                         seconds=[round(media.probe_duration(clip) or 0.0, 2)],
                         cuts=[int(c) for c in info.get("cuts") or []],
                         trimmed_sec=float(info.get("trimmed_sec") or 0.0))
    scene.colours = studio._read_colours(work / "colours.json") if (work / "colours.json").is_file() else None
    studio._PREPARED[(815, "scene_0")] = scene
    return scene


def _analysis_815(clip: Path) -> bool:
    """The owner's analysis of the scene clip (its matte bank and camera),
    copied in; False when this machine has none."""
    from app.services.rebrand_harness import stages as st

    name = (st.digest(clip) or "none")[:16]
    src = ANALYSIS_ROOT_815 / name
    if not src.is_dir() or not any(src.iterdir()):
        return False
    shutil.copytree(src, studio.ANALYSIS_ROOT / name)
    return True


async def _plan_815(recipe: rc.Recipe) -> dict:
    """studio.icon_plan as the stage asks it, each second, until it is
    ready (its inputs from their caches)."""
    for _ in range(600):
        body = await studio.icon_plan(815, "scene_0", recipe)
        if body.get("state") == "ready":
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"815's icon plan never got ready: {body.get('steps')}")


@pytest.mark.skipif(not HAVE_815, reason=NO_815)
def test_815s_scene_folder_is_the_one_it_was():
    """scene_base on the real anim-351: the same folder (its source hash and
    the tail flash cut at source frame 188), so no scene is prepared again."""
    assert studio.scene_base(815, "scene_0", ANIM_351).name == S815_NAME
    assert media.tail_cut(ANIM_351) == 188


@pytest.mark.skipif(not HAVE_815, reason=NO_815)
def test_815s_keys_are_frozen():
    before = _snapshot(S815)
    scene = _scene_815()
    clip = studio._scene_clip(scene)
    have_analysis = _analysis_815(clip)
    got: dict[str, Any] = {"scene.seconds": scene.seconds, "scene.trimmed_sec": scene.trimmed_sec}

    # The director: the scene's own words, line, topic and room give the key
    # its paid answer is stored under.
    words = json.loads((scene.work / "icons" / "words.json").read_text(encoding="utf-8"))
    track = T.load(scene.work / "icons")
    assert track is not None
    dctx = D.DirectorCtx(words=words, script=LINE_815, work_dir=scene.work / "icons", topic=TOPIC_815,
                         others=[], frame0=track.frame0, spots=list(track.spots), take=0)
    inputs = D.key_inputs(dctx, D.default_catalog())
    got["director.inputs"] = inputs
    got["director.inputs_digest"] = _digest(inputs)
    got["director.key"] = D.director_key(dctx, D.default_catalog())
    stored_answer = json.loads((scene.work / "icons" / "director.json").read_text(encoding="utf-8"))
    assert got["director.key"] == stored_answer["key"], "815's paid answer no longer answers its key"

    async def run() -> None:
        for name, make in RECIPES.items():
            draft = make(815, "scene_0")
            seed = draft.seed or studio.default_seed(815, "scene_0")
            got[f"{name}.recipe_hash"] = rc.recipe_hash(draft)
            got[f"{name}.preview_key"] = studio._preview_key(draft, seed)
            stored = draft
            if rc.icon_element(draft) is not None:
                body = await _plan_815(draft)
                # The words heard and — with the AI on — the paid answer, both
                # from their caches (Whisper and every model refuse here).
                assert body["source"]["words"] == "whisper", body["source"]
                if rc.icon_props(rc.icon_element(draft)).ai:
                    assert body["source"]["director"] == "ai", body["source"]
                got[f"{name}.plan_key"] = body["plan_key"]
                got[f"{name}.timing_key"] = body["timing_key"]
                got[f"{name}.shown"] = [it["glyph"]["id"] for it in body["items"]]
                # As Generate stores it (studio.as_generated): the plan shown.
                kept = studio.kept_plan(815, "scene_0", body["plan_key"])
                el = rc.icon_element(draft)
                stored = rc.normalize(draft.model_copy(update={"elements": [
                    e.model_copy(update={"props": {**e.props, "resolved": kept.get("resolved") or []}})
                    if e.id == el.id else e for e in draft.elements]}))
            stored = frozen_patterns(stored)
            got[f"{name}.stored_recipe_hash"] = rc.recipe_hash(stored)
            # The folder re-rooted onto a fixed POSIX root: the memo holds the
            # folder's path, and only its name (the source's hash, _t188) is
            # the scene's — where the data lives is not.
            fixed = dataclasses.replace(scene, work=PurePosixPath("/studio/815") / S815_NAME)
            got[f"{name}.spec_memo"] = studio._spec_memo(fixed, stored, None)
            if have_analysis:
                hs = await studio.house_spec(815, "scene_0", stored)
                got[f"{name}.render_key"] = hs.key
                got[f"{name}.render_n"] = hs.spec.n

    asyncio.run(run())
    if not have_analysis:
        pytest.skip("run 815's scene analysis (matte bank, camera) is not on this machine: render keys unchecked")
    _pinned("run_815", got)
    assert got["starting_draft.render_n"] == 234, "the trimmed scene is 234 frames"
    assert _snapshot(S815) == before, "815's own folder was written to"
