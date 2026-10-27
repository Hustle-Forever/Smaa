"""The golden lock on the owner-approved Rebrand (checkpoint-rebrand-hardcoded).

The harness refactor (branch rebrand-harness) must keep the approved default
— 2.png structure, left half, 2x, behind the speaker, the intro's colour, the
left-to-right entrance, the speaker put back in front — reproducible exactly.
Three locks, strongest first:

  call log   a whole per-scene Omni run (_build_pattern_ref + _omni_clip)
             with Omni and the video tools faked: every step, every prompt,
             every target image's pixels, every guard's arguments, in order.
             Environment-independent (it compares within one process).
  prompts    the exact Omni prompt text with and without the entrance.
  pixels     the target frames' pixel hashes. Pinned to the numpy/Pillow
             versions they were recorded with (resampling can differ across
             versions), and skipped elsewhere.

Fixtures (tests/fixtures/rebrand_golden/): experiment 15's real intro and
chunk frame, and that frame's MODNet speaker mask. To re-record after an
INTENDED change: REBRAND_GOLDEN_UPDATE=1 pytest tests/test_rebrand_golden.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import numpy as np
import PIL
import pytest
from PIL import Image

from app.services import omni_client as omni
from app.services import person_matte
from app.services import rebrand_media as media
from app.services import rebrand_pattern_ref as pattern_ref
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import speaker_front
from app.services.rebrand_harness import recipe as hrecipe

FIX = Path(__file__).parent / "fixtures" / "rebrand_golden"
INTRO, FRAME, PERSON = FIX / "intro.mp4", FIX / "frame.png", FIX / "person.png"
UPDATE = os.environ.get("REBRAND_GOLDEN_UPDATE") == "1"
BRIEF = {"omni_scene_prompt": "EDIT the clip; keep #0663a3 and '42 pattern' out."}


def _pixels(path: Path) -> str:
    return hashlib.sha256(np.asarray(Image.open(path)).tobytes()).hexdigest()[:16]


def _person() -> np.ndarray:
    return np.asarray(Image.open(PERSON).convert("L"), dtype="float32") / 255.0


def _check(name: str, got):
    """Compare with the recorded fixture, or record it (REBRAND_GOLDEN_UPDATE=1)."""
    path = FIX / name
    got = json.loads(json.dumps(got, ensure_ascii=False, sort_keys=True))   # tuples -> lists, as stored
    if UPDATE or not path.exists():
        path.write_text(json.dumps(got, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"{name} did not exist and has been recorded — re-run to check it")
        return
    want = json.loads(path.read_text(encoding="utf-8"))
    assert got == want, f"{name} changed — the approved Rebrand is no longer reproduced"


# ---------------------------------------------------------------------------
# Pixels (pinned per environment)
# ---------------------------------------------------------------------------

def test_the_approved_target_frames_are_pixel_identical(tmp_path):
    env = {"numpy": np.__version__, "pillow": PIL.__version__}
    got = {"env": env}
    for aspect in ("9:16", "16:9"):
        ref = pattern_ref.build(INTRO, tmp_path / aspect.replace(":", "x"), aspect=aspect)
        got[f"colorize {aspect}"] = _pixels(ref.path)
        if aspect == "9:16":
            got["target"] = _pixels(pattern_ref.strip_preview(ref, FRAME, tmp_path / "t.png"))
            got["target+person"] = _pixels(
                pattern_ref.strip_preview(ref, FRAME, tmp_path / "tp.png", person=_person()))
    path = FIX / "pixels.json"
    if path.exists() and not UPDATE:
        want = json.loads(path.read_text(encoding="utf-8"))
        if want.get("env") != env:
            pytest.skip(f"pixel hashes were recorded with {want.get('env')}, this is {env}")
    _check("pixels.json", got)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _structure_ref() -> pattern_ref.PatternRef:
    return pattern_ref.PatternRef(slug="structure", source="2.png", path=Path("x.png"),
                                  ground="#3d3c3d", ink="#f4f4f8", fell_back=False, colour="#2ed8e8")


def _ctx(work: Path, *, target: str = "scene_0") -> pipeline.Ctx:
    intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
    scene = pipeline.Segment(kind="scene", index=0, source=work / "scene0.mp4", is_still=False)
    scene.base = work / "base_scene0.mp4"
    ctx = pipeline.Ctx(
        exp_id=15, run_id=806, aspect="9:16", width=720, height=1280, work=work,
        options=pipeline.normalize_options({"target": target}), run_ctx={"topic": "t"},
        intro=intro, scenes=[scene], outro=None,
    )
    ctx.brief = dict(BRIEF)
    ctx.target_seg = scene
    return ctx


def test_the_approved_omni_prompts_are_word_for_word(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.pattern_ref = _structure_ref()
    _check("prompts.json", {
        "hold": pipeline._omni_scene_prompt(ctx),
        "enter": pipeline._omni_scene_prompt(ctx, entrance=True),
    })


# ---------------------------------------------------------------------------
# The call log of a whole per-scene run
# ---------------------------------------------------------------------------

def _scene_run_log(tmp_path, monkeypatch, *, harness: bool) -> list:
    """The call log of one per-scene run with Omni and the video tools faked:
    the old loop, or the harness runner following the 42 default recipe."""
    log: list = []

    async def append_arm_step(exp_id, arm, step):
        log.append(["step", arm, step["name"], step["status"], step.get("detail", "")])

    async def patch_arm(exp_id, arm, patch):
        log.append(["patch", arm, sorted(k for k in patch if k not in ("started_at",))])

    def split_chunks(src, out_dir):
        return [Path(out_dir) / "c0.mp4", Path(out_dir) / "c1.mp4"]

    def extract_frame(video, out, *, at):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FRAME, out)
        return Path(out)

    async def person_mask(frame):
        log.append(["mask", Path(frame).name])
        return _person()

    async def edit_video(video, prompt, *, aspect, out, images=None):
        log.append(["omni", Path(video).name, prompt, [Path(i).name for i in images or []],
                    [_pixels(Path(i)) for i in images or []]])
        return {"path": out}

    def fit(raw, chunk, out, **kw):
        log.append(["fit", Path(raw).name, Path(chunk).name, Path(out).name])
        return out

    def restore(edited, original, out, **kw):
        log.append(["restore", Path(edited).name, Path(original).name, Path(out).name,
                    sorted(kw.items())])
        return out

    def concat(parts, out):
        log.append(["concat", [Path(p).name for p in parts], Path(out).name])
        return out

    async def finish(ctx, arm, final, parts, extra):
        log.append(["finish", arm, Path(final).name, sorted(parts), sorted(extra.items())])

    monkeypatch.setattr(store, "append_arm_step", append_arm_step)
    monkeypatch.setattr(store, "patch_arm", patch_arm)
    monkeypatch.setattr(media, "split_chunks", split_chunks)
    monkeypatch.setattr(media, "extract_frame", extract_frame)
    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(media, "fit_video_to_audio", fit)
    monkeypatch.setattr(speaker_front, "restore_speaker", restore)
    monkeypatch.setattr(media, "concat", concat)
    monkeypatch.setattr(pipeline, "_finish_arm", finish)

    ctx = _ctx(tmp_path)
    if harness:
        ctx.recipe = hrecipe.preset_42_default("scene_0")
        ctx.harness = True
    asyncio.run(pipeline._build_pattern_ref(ctx))
    assert ctx.pattern_ref is not None and ctx.pattern_error == ""
    log.append(["pattern", ctx.pattern_ref.slug, ctx.pattern_ref.colour, ctx.pattern_ref.where,
                ctx.pattern_ref.animation, ctx.pattern_ref.layout.public(), ctx.pattern_ref.clears_bars])
    asyncio.run(pipeline._omni_clip(ctx))
    return log


def test_the_approved_scene_run_is_call_for_call_identical(tmp_path, monkeypatch):
    _check("calllog.json", _scene_run_log(tmp_path, monkeypatch, harness=False))


def test_the_harness_runs_the_42_default_call_for_call_as_approved(tmp_path, monkeypatch):
    """The harness runner, following the 42 default recipe, makes exactly the
    approved run's calls — same steps, prompts, target pixels, guard
    arguments. The only addition is the run's manifest and recipe hash on
    the finished arm."""
    log = _scene_run_log(tmp_path, monkeypatch, harness=True)
    finish = log[-1]
    assert finish[0] == "finish"
    extra = dict(finish[4])
    stages, recipe_hash = extra.pop("stages"), extra.pop("recipe_hash")
    finish[4] = sorted(extra.items())
    assert recipe_hash == hrecipe.recipe_hash(hrecipe.preset_42_default("scene_0"))
    assert sorted(stages) == ["guards", "omni", "target"]
    assert [stages["guards"][c]["clear_veil_sec"] for c in ("c0", "c1")] == [3.0, 0.0]
    assert [stages["omni"][c]["entering"] for c in ("c0", "c1")] == [True, False]
    _check("calllog.json", log)
