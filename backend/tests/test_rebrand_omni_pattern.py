"""Omni's reference image is the coloured 42 pattern, never the logo.

The owner took the logo off the Omni arm: its one reference image is now one
of the five brand patterns, picked at random per experiment and coloured in
the intro's own colours (rebrand_pattern_ref). These pin the wiring around
that, with every paid call faked:

  * a scene edit sends [pattern png] to Omni, on every chunk, and never the
    logo; no pattern -> no image at all; a card restyle -> no image;
  * the scene prompt names the pattern and both hexes, and asks for no logo;
  * a pattern that cannot be built degrades (Omni runs bare), never kills;
  * the experiment's evidence records which pattern was used, or why none;
  * the brief model is shown the SAME png with a legend that says so, and
    the logo it still sees is labelled as the Veo keyframe's only.
"""
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PIL import Image

from app.services import omni_client as omni
from app.services import pattern_library
from app.services import pattern_planner
from app.services import person_matte
from app.services import speaker_front
from app.services import rebrand_brief
from app.services import rebrand_media as media
from app.services import rebrand_pattern_ref as pattern_ref
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store

INK, GROUND = "#f4f4f8", "#7f2bf2"


def _png(path: Path, rgb=(127, 43, 242)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), rgb).save(path)
    return path


def _ref(tmp_path: Path, *, fell_back: bool = False) -> pattern_ref.PatternRef:
    return pattern_ref.PatternRef(
        slug="structure", source="2.png",
        path=_png(tmp_path / "pattern" / "structure_colored.png"),
        ground=GROUND, ink=INK, fell_back=fell_back,
        intro_frame=tmp_path / "pattern" / "intro_frame.png",
    )


def _ctx(tmp_path: Path, *, ref=None, brief=None, options=None) -> pipeline.Ctx:
    intro = pipeline.Segment(kind="intro", index=0, source=tmp_path / "intro.mp4", is_still=False)
    scene = pipeline.Segment(kind="scene", index=0, source=tmp_path / "scene0.mp4", is_still=False)
    ctx = pipeline.Ctx(
        exp_id=7, run_id=3, aspect="9:16", width=720, height=1280, work=tmp_path,
        options=options or pipeline.normalize_options({}),
        run_ctx={"topic": "t", "hook": "h"},
        intro=intro, scenes=[scene], outro=None,
        logo=_png(tmp_path / "logo.png", (255, 255, 255)),
    )
    ctx.brief = brief if brief is not None else {"omni_scene_prompt": "EDIT the clip."}
    ctx.pattern_ref = ref
    return ctx


@pytest.fixture
def fake_omni(monkeypatch):
    """Omni + the media helpers around it, faked. Records every edit call and
    every arm step; split_chunks returns TWO chunks so a >10 s scene is
    covered."""
    state: dict = {}
    calls: list[dict] = []
    steps: list[dict] = []

    async def edit_video(video, prompt, *, aspect, out, images=None):
        calls.append({"video": video, "prompt": prompt, "aspect": aspect,
                      "images": list(images or []), "out": out})
        return {"path": out}

    async def patch_arm(exp_id, arm, patch):
        return None

    async def append_arm_step(exp_id, arm, step):
        steps.append({"arm": arm, **step})

    def split_chunks(src, out_dir):
        return [Path(out_dir) / "c0.mp4", Path(out_dir) / "c1.mp4"]

    previews: list[dict] = []

    def extract_frame(video, out, *, at):
        return out

    def strip_preview(ref, frame, out, *, person=None):
        previews.append({"ref": ref, "frame": frame, "out": out, "person": person})
        return out

    masks: list = []

    async def person_mask(frame):
        masks.append(frame)
        return state["mask"]

    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(store, "patch_arm", patch_arm)
    monkeypatch.setattr(store, "append_arm_step", append_arm_step)
    monkeypatch.setattr(media, "split_chunks", split_chunks)
    monkeypatch.setattr(media, "extract_frame", extract_frame)
    monkeypatch.setattr(person_matte, "person_mask", person_mask)

    fronts: list = []

    def restore_speaker(edited, original, out, *, clear_veil_sec=0.0, clear_bars=False):
        if state.get("front_fails"):
            raise RuntimeError("matte failed")
        fronts.append({"edited": edited, "original": original, "out": out,
                       "veil": clear_veil_sec, "bars": clear_bars})
        return out

    monkeypatch.setattr(speaker_front, "restore_speaker", restore_speaker)
    monkeypatch.setattr(pattern_ref, "strip_preview", strip_preview)
    monkeypatch.setattr(media, "fit_video_to_audio", lambda raw, chunk, out, **kw: out)
    monkeypatch.setattr(media, "concat", lambda parts, out: out)
    state.update({"calls": calls, "steps": steps, "previews": previews, "masks": masks,
                  "mask": "PERSON-MASK", "fronts": fronts})
    return state


# ---------------------------------------------------------------------------
# What Omni is sent
# ---------------------------------------------------------------------------

def test_scene_edit_sends_the_pattern_never_the_logo(tmp_path, fake_omni):
    ref = _ref(tmp_path)
    ctx = _ctx(tmp_path, ref=ref)
    seg = ctx.scenes[0]
    asyncio.run(pipeline._omni_edit(ctx, "omni", seg, "p", tmp_path / "omni", "Scene 1"))

    calls = fake_omni["calls"]
    assert len(calls) == 2  # one per chunk
    previews = fake_omni["previews"]
    # Each chunk is shown ITS OWN first frame with the pattern laid in.
    assert [pv["frame"].name for pv in previews] == ["scene_1_c0_first.png", "scene_1_c1_first.png"]
    assert all(pv["ref"] is ref for pv in previews)
    for c, pv in zip(calls, previews):
        assert c["images"] == [pv["out"]]
        assert ctx.logo not in c["images"] and ref.path not in c["images"]
        # ...with the speaker's mask for THAT frame, so the pattern sits behind them.
        assert pv["person"] == "PERSON-MASK"
    assert [m.name for m in fake_omni["masks"]] == ["scene_1_c0_first.png", "scene_1_c1_first.png"]


def test_the_speaker_is_put_back_in_front_of_every_scene_chunk(tmp_path, fake_omni):
    """Omni draws the pattern over the speaker regardless; each fitted chunk
    goes through speaker_front with ITS original chunk, and the result is
    what the scene is built from."""
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    out = asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "p", tmp_path / "omni",
                                          "Scene 1"))
    fronts = fake_omni["fronts"]
    assert [f["original"].name for f in fronts] == ["c0.mp4", "c1.mp4"]
    assert [f["edited"].name for f in fronts] == ["scene_1_c0.mp4", "scene_1_c1.mp4"]
    assert [f["out"].name for f in fronts] == ["scene_1_c0_front.mp4", "scene_1_c1_front.mp4"]
    assert out.name == "scene_1.mp4"            # the concat of the two front clips


def test_only_the_entrance_chunk_is_cleared_of_a_veil(tmp_path, fake_omni):
    """Omni sometimes slides a frosted panel in with the pattern's entrance;
    only the chunk that HAS the entrance is checked for one."""
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "hold", tmp_path / "omni",
                                    "Scene 1", first_prompt="enter"))
    assert [f["veil"] for f in fake_omni["fronts"]] == [speaker_front.VEIL_GUARD_SEC, 0.0]
    fake_omni["fronts"].clear()
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "hold", tmp_path / "omni2",
                                    "Scene 1"))
    assert [f["veil"] for f in fake_omni["fronts"]] == [0.0, 0.0], "no entrance, no guard"


def test_the_bars_are_ignored_for_every_pattern_but_the_approved_one(tmp_path, fake_omni):
    """A letterbox bar is never the speaker: every pattern but structure is
    put back over Omni's output with the matte ignored on the bars (the
    target frame was cut the same way), so MODNet's specks there cannot
    paste the black bar back over the pattern. Structure keeps its
    approved pass."""
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "p", tmp_path / "omni", "Scene 1"))
    assert [f["bars"] for f in fake_omni["fronts"]] == [False, False]
    fake_omni["fronts"].clear()
    grid = dataclasses.replace(_ref(tmp_path), slug="grid", source="4.png")
    ctx = _ctx(tmp_path, ref=grid)
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "p", tmp_path / "omni2", "Scene 1"))
    assert [f["bars"] for f in fake_omni["fronts"]] == [True, True]


def test_the_entrance_asks_for_the_lines_alone(tmp_path):
    """Experiment 19: "a soft-edged wipe" came back as a frosted panel."""
    p = pipeline._omni_scene_prompt(_ctx(tmp_path, ref=_ref(tmp_path)), entrance=True)
    assert "wipe" not in p.lower()
    assert "the pattern's own lines fade in smoothly from LEFT to RIGHT" in p
    assert "no panel, card, frosted-glass sheet, white or grey shape" in p
    assert "the room stays exactly as shot the whole time" in p


def test_cards_are_not_touched_by_speaker_front(tmp_path, fake_omni):
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.intro, "card", tmp_path / "omni", "Intro"))
    assert fake_omni["fronts"] == []


def test_a_failed_front_pass_keeps_omnis_clip_and_says_so(tmp_path, fake_omni):
    fake_omni["front_fails"] = True
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "p", tmp_path / "omni", "Scene 1"))
    assert len(fake_omni["calls"]) == 2
    assert any(st["name"] == "Scene 1: speaker not put in front" and st["status"] == "failed"
               for st in fake_omni["steps"])


def test_no_speaker_mask_still_edits_and_says_so(tmp_path, fake_omni):
    fake_omni["mask"] = None
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "p", tmp_path / "omni", "Scene 1"))
    assert len(fake_omni["calls"]) == 2
    assert all(pv["person"] is None for pv in fake_omni["previews"])
    assert any(st["name"] == "Scene 1: no speaker mask" and st["status"] == "failed"
               for st in fake_omni["steps"])


def test_scene_edit_without_a_pattern_sends_no_image(tmp_path, fake_omni):
    ctx = _ctx(tmp_path, ref=None)
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "p", tmp_path / "omni", "Scene 1"))
    assert [c["images"] for c in fake_omni["calls"]] == [[], []]


def test_card_edit_sends_no_image_even_with_a_pattern(tmp_path, fake_omni):
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.intro, "card", tmp_path / "omni", "Intro"))
    assert fake_omni["calls"] and all(c["images"] == [] for c in fake_omni["calls"])


# ---------------------------------------------------------------------------
# The scene prompt
# ---------------------------------------------------------------------------

def test_scene_prompt_carries_the_pattern_clause_and_no_logo(tmp_path):
    brief = {
        "omni_scene_prompt": "EDIT the clip.",
        "brand_region": {"where": "left edge"},
        # An older brief still carrying a logo placement must not bring the
        # logo clause back.
        "logo": {"placement": "top right", "treatment": "white"},
    }
    p = pipeline._omni_scene_prompt(_ctx(tmp_path, ref=_ref(tmp_path), brief=brief))
    assert p.startswith("EDIT the clip.")
    assert "The attached image is the TARGET" in p
    assert "already laid into the left half of the frame, behind the speaker" in p
    assert "part of the BACKGROUND, BEHIND the speaker" in p
    assert "Never draw a single line over the speaker" in p
    assert "the same large scale" in p
    assert "Keep the pattern's own outline" in p and "never trim it to a straight edge" in p
    assert "the same shapes, the same large scale, the same position and the same colour" in p
    assert "video's own background visible between the lines" in p
    assert "a checkerboard or any other texture" in p
    assert "no logo, no letters, no numbers, no colour codes" in p
    # Nothing Omni could paint: no colour code, no "42".
    assert GROUND not in p and INK not in p and "#" not in p and "42" not in p
    # The default (and every later chunk) is told the strip is already there.
    assert "already exactly as in the attached image from the first frame" in p
    assert "ENTRANCE" not in p
    assert "real 42 logo" not in p and "Place exactly this mark" not in p
    assert not hasattr(pipeline, "_with_logo_clause")


def test_the_entrance_prompt_reveals_the_pattern_left_to_right(tmp_path):
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    p = pipeline._omni_scene_prompt(ctx, entrance=True)
    assert "ENTRANCE" in p and "fade in smoothly from LEFT to RIGHT" in p
    assert "already exactly as in the attached image" not in p
    assert "#" not in p and "42" not in p


def test_the_brief_prompt_loses_anything_omni_would_paint(tmp_path):
    """Experiment 13: the brief wrote "#0663a3" and "'42 pattern'" into the
    scene prompt, and Omni painted both into the strip as text."""
    brief = {"omni_scene_prompt": "Overlay the attached '42 pattern' image, drawing ONLY its "
                                  "#0663a3 lines, not #fff. Keep it."}
    p = pipeline._omni_scene_prompt(_ctx(tmp_path, ref=None, brief=brief))
    assert "#" not in p and "42" not in p
    assert "drawing ONLY its lines, not. Keep it." in p


def test_only_the_first_chunk_gets_the_entrance(tmp_path, fake_omni):
    """A >10 s scene is edited in chunks; if every chunk got the entrance the
    pattern would vanish and wipe in again at each chunk seam."""
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    asyncio.run(pipeline._omni_edit(
        ctx, "omni", ctx.scenes[0], "hold", tmp_path / "omni", "Scene 1",
        first_prompt="enter",
    ))
    assert [c["prompt"] for c in fake_omni["calls"]] == ["enter", "hold"]


def test_whole_reel_enters_on_the_first_scene_only(tmp_path, fake_omni, monkeypatch):
    ctx = _ctx(tmp_path, ref=_ref(tmp_path))
    ctx.scenes.append(pipeline.Segment(kind="scene", index=1, source=tmp_path / "scene1.mp4",
                                       is_still=False))
    ctx.intro = None
    for sg in ctx.scenes:
        sg.base = sg.source

    async def no_frame(*_a, **_kw):
        return tmp_path / "f.png"

    async def finish(*_a, **_kw):
        return None

    monkeypatch.setattr(pipeline, "_frame", no_frame)
    monkeypatch.setattr(pipeline, "_finish_arm", finish)
    asyncio.run(pipeline._omni_arm(ctx))
    prompts = [c["prompt"] for c in fake_omni["calls"]]
    assert len(prompts) == 4                       # 2 scenes x 2 chunks
    assert "ENTRANCE" in prompts[0]
    assert all("ENTRANCE" not in q and "already exactly as in the attached image" in q
               for q in prompts[1:])


def test_scene_prompt_without_a_pattern_is_the_brief_as_written(tmp_path):
    brief = {"omni_scene_prompt": "EDIT the clip.", "logo": {"placement": "top right"}}
    assert pipeline._omni_scene_prompt(_ctx(tmp_path, ref=None, brief=brief)) == "EDIT the clip."


def test_card_prompt_gets_no_clause(tmp_path):
    ctx = _ctx(tmp_path, ref=_ref(tmp_path), brief={"omni_card_prompt": "Keep every letter."})
    assert pipeline._omni_card_prompt(ctx) == "Keep every letter."


# ---------------------------------------------------------------------------
# Building the ref (degrades, never kills)
# ---------------------------------------------------------------------------

def test_build_pattern_ref_reports_the_pattern(tmp_path, monkeypatch, fake_omni):
    ref = _ref(tmp_path)
    seen: dict = {}

    def build(intro, out_dir, *, aspect, is_still=False, **kw):
        seen.update(intro=intro, out_dir=out_dir, aspect=aspect, is_still=is_still, **kw)
        return ref

    monkeypatch.setattr(pattern_ref, "build", build)
    ctx = _ctx(tmp_path)
    asyncio.run(pipeline._build_pattern_ref(ctx))

    assert ctx.pattern_ref is ref and ctx.pattern_error == ""
    # No pattern picked: structure (2.png), the approved setup, from the library.
    structure = pattern_library.BUILTINS["structure"]
    assert seen == {"intro": ctx.intro.source, "out_dir": tmp_path / "pattern",
                    "aspect": "9:16", "is_still": False, "slug": "structure",
                    "layout": structure.layout, "mask": None, "animation": "left_to_right",
                    "name": "Structure", "source_file": "2.png"}
    step = fake_omni["steps"][-1]
    assert step["arm"] == "omni" and step["status"] == "done"
    assert step["name"] == f"Pattern: structure (2.png) — {GROUND}, transparent background"


def test_build_pattern_ref_failure_degrades(tmp_path, monkeypatch, fake_omni):
    def build(*_a, **_kw):
        raise RuntimeError("kit missing")

    monkeypatch.setattr(pattern_ref, "build", build)
    ctx = _ctx(tmp_path)
    asyncio.run(pipeline._build_pattern_ref(ctx))

    assert ctx.pattern_ref is None
    assert "kit missing" in ctx.pattern_error
    assert fake_omni["steps"][-1]["status"] == "failed"
    # ...and Omni then runs bare: no image, no clause, no logo.
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], pipeline._omni_scene_prompt(ctx),
                                    tmp_path / "omni", "Scene 1"))
    assert all(c["images"] == [] for c in fake_omni["calls"])
    assert all(c["prompt"] == "EDIT the clip." for c in fake_omni["calls"])


# ---------------------------------------------------------------------------
# Evidence on the experiment
# ---------------------------------------------------------------------------

def _run_with(tmp_path, monkeypatch, ctx) -> tuple[dict, dict]:
    """Drive pipeline._run with _prepare, the brief, the arms and the store
    faked. Returns (the kwargs build_brief got, the evidence stored)."""
    brief_kw: dict = {}
    updates: list[dict] = []

    async def prepare(exp_id, run_id, options):
        return ctx

    async def build_brief(**kw):
        brief_kw.update(kw)
        return {"omni_scene_prompt": "EDIT."}, {"logo_attached": True}

    async def update_experiment(exp_id, **fields):
        updates.append(fields)

    async def get_experiment(exp_id):
        return {"arms": {"omni": {"status": store.ARM_READY}}}

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(pipeline, "_prepare", prepare)
    monkeypatch.setattr(rebrand_brief, "build_brief", build_brief)
    monkeypatch.setattr(pipeline, "_omni_arm", noop)
    monkeypatch.setattr(store, "update_experiment", update_experiment)
    monkeypatch.setattr(store, "get_experiment", get_experiment)
    monkeypatch.setattr(store, "store_memory", noop)
    asyncio.run(pipeline._run(7, 3, ctx.options))
    # _run swallows a crash into the row, so check it really got to the end.
    assert updates[-1].get("status") == store.ST_READY, updates[-1]
    evidence = next(u["evidence"] for u in updates if "evidence" in u)
    return brief_kw, evidence


def test_run_records_the_pattern_in_evidence(tmp_path, monkeypatch):
    ref = _ref(tmp_path)
    ctx = _ctx(tmp_path, ref=ref, options=pipeline.normalize_options({"run_veo": False}))
    brief_kw, evidence = _run_with(tmp_path, monkeypatch, ctx)
    assert brief_kw["pattern_ref"] is ref
    assert evidence["omni_pattern"] == ref.public()
    assert evidence["omni_pattern"]["slug"] == "structure"
    assert "omni_pattern_error" not in evidence


def test_run_records_why_there_is_no_pattern(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, ref=None, options=pipeline.normalize_options({"run_veo": False}))
    ctx.pattern_error = "kit missing"
    brief_kw, evidence = _run_with(tmp_path, monkeypatch, ctx)
    assert brief_kw["pattern_ref"] is None
    assert evidence["omni_pattern_error"] == "kit missing"
    assert "omni_pattern" not in evidence


# ---------------------------------------------------------------------------
# The brief model sees the pattern
# ---------------------------------------------------------------------------

@pytest.fixture
def brief_env(monkeypatch):
    """build_brief with every DB / Vertex dependency faked; captures what
    the brief model is sent. `fail=True` makes the model call raise."""
    from app.agents.producer import memory
    from app.services import yas_edit_recipes

    sent: dict = {"fail": False}

    async def embed(texts):
        return [[0.0] * 4]

    async def requirements():
        return {"primary_colors": ["violet"], "secondary_colors": ["white"]}

    async def empty(*_a, **_kw):
        return []

    async def multimodal(prompt, *, images, system, **_kw):
        sent.update(prompt=prompt, images=list(images), system=system)
        if sent["fail"]:
            raise RuntimeError("model down")
        return {"omni_scene_prompt": "EDIT with the attached 42 pattern."}

    monkeypatch.setattr(rebrand_brief, "gemini_embed", embed)
    monkeypatch.setattr(rebrand_brief, "gemini_multimodal", multimodal)
    monkeypatch.setattr(rebrand_brief, "_ig_rows", empty)
    monkeypatch.setattr(rebrand_brief, "_ig_frames", empty)
    monkeypatch.setattr(store, "past_lessons", empty)
    monkeypatch.setattr(memory, "build_brand_requirements", requirements)
    monkeypatch.setattr(yas_edit_recipes, "nearest_recipes", empty)
    return sent


def _frames(tmp_path: Path) -> dict:
    return {k: _png(tmp_path / f"{k}.png") for k in ("intro_last", "th_first", "th_last", "outro_first")}


def test_brief_attaches_the_pattern_with_its_legend(tmp_path, brief_env):
    ref = _ref(tmp_path)
    logo = _png(tmp_path / "logo.png", (255, 255, 255))
    brief, evidence = asyncio.run(rebrand_brief.build_brief(
        exp_id=7, run_ctx={"topic": "t", "hook": "h", "aspect": "9:16"}, frames=_frames(tmp_path),
        logo=logo, options=pipeline.normalize_options({}), pattern_ref=ref,
    ))
    imgs = brief_env["images"]
    # Four boundary frames, then the pattern, then the logo.
    assert imgs[4] == ref.path and imgs[5] == logo
    p = brief_env["prompt"]
    assert ("Image 5: the brand pattern (structure) — only its lines are content, the rest is "
            "truly empty (alpha); coloured from the intro. Omni is shown it already laid into "
            "the left half of the frame, behind the speaker of the clip's own frame") in p
    assert GROUND not in p, "a colour code handed to Gemini ends up painted by Omni"
    assert "must explicitly forbid a checkerboard" in p
    assert "already laid into the left half of the frame, behind the speaker (its fixed place" in p
    assert "BEHIND the speaker (the speaker covers it" in p
    assert 'NEVER write a colour code (like #1a2b3c), a number or the word "42"' in p
    assert "Image 6: the real 42 logo (white PNG) — for the Veo keyframe" in p
    assert "OMNI'S BRAND PATTERN (image 5)" in p
    assert "Omni gets no logo" in brief_env["system"]
    assert "THE BRAND PATTERN" in brief_env["system"]
    assert evidence["pattern_attached"] is True and evidence["logo_attached"] is True
    assert brief["omni_scene_prompt"].startswith("EDIT")


def test_brief_system_still_protects_the_cards_own_logo():
    """The no-logo rule is about what Omni ADDS. The card restyle is the one
    Omni prompt that must still name the logo: the card already carries the
    real mark, Omni redraws every pixel it is given, and a prompt forbidden
    from mentioning the mark gives it no reason to keep it."""
    system = rebrand_brief.BRIEF_SYSTEM
    assert "no omni_* prompt mentions" not in system
    rule = system[system.index("Omni gets no logo"):][:700]
    assert "omni_scene_prompt and the omni transition prompts never mention" in rule
    assert "omni_card_prompt" in rule and "keep the card's existing" in rule
    card = rebrand_brief.BRIEF_SCHEMA["properties"]["omni_card_prompt"]["description"]
    assert "the logo exactly" in card


def test_brief_legend_names_the_brand_colour_fallback(tmp_path, brief_env):
    asyncio.run(rebrand_brief.build_brief(
        exp_id=7, run_ctx={"topic": "t"}, frames=_frames(tmp_path), logo=None,
        options=pipeline.normalize_options({}), pattern_ref=_ref(tmp_path, fell_back=True),
    ))
    assert "the intro gave no usable colour" in brief_env["prompt"]


def test_brief_without_a_pattern_attaches_none(tmp_path, brief_env):
    _, evidence = asyncio.run(rebrand_brief.build_brief(
        exp_id=7, run_ctx={"topic": "t"}, frames=_frames(tmp_path), logo=None,
        options=pipeline.normalize_options({}),
    ))
    assert len(brief_env["images"]) == 4
    assert "brand pattern (" not in brief_env["prompt"]
    assert "OMNI'S BRAND PATTERN" not in brief_env["prompt"]
    assert evidence["pattern_attached"] is False


def test_fallback_brief_builds_the_strip_from_the_attached_pattern(tmp_path, brief_env):
    brief_env["fail"] = True
    brief, evidence = asyncio.run(rebrand_brief.build_brief(
        exp_id=7, run_ctx={"topic": "t"}, frames=_frames(tmp_path), logo=None,
        options=pipeline.normalize_options({}), pattern_ref=_ref(tmp_path),
    ))
    assert brief.get("_fallback") and "brief_error" in evidence
    omni_p = brief["omni_scene_prompt"]
    assert "the attached pattern" in omni_p and GROUND not in omni_p and INK not in omni_p
    assert "never draw a checkerboard" in omni_p and "video's own background stays visible" in omni_p
    assert brief["brand_region"]["where"] == "left edge"
    assert "never a line over the speaker" in omni_p and "Nothing enters the right half" in omni_p
    assert all("beside the speaker" in i["placement"] for i in brief["icons"])
    assert "measuring grid" not in omni_p
    assert "No logo" in omni_p
    assert "attached pattern" in brief["brand_region"]["description"]
    # Cards and the Veo keyframe never receive the pattern image: unchanged.
    assert "measuring grid" in brief["keyframe_edit_prompt"]
    assert "attached" not in brief["omni_card_prompt"]


def test_fallback_brief_without_a_pattern_keeps_the_drawn_panel():
    brief = rebrand_brief._fallback_brief({}, {"topic": "t"}, None)
    assert "measuring grid" in brief["omni_scene_prompt"]
    assert "attached" not in brief["omni_scene_prompt"]
    assert "No logo" in brief["omni_scene_prompt"]


# ---------------------------------------------------------------------------
# The pattern library in the pipeline: options, choice, animation, Veo gone
# ---------------------------------------------------------------------------

def test_options_retire_veo_and_default_to_the_approved_pattern():
    o = pipeline.normalize_options({"run_veo": True, "veo_quality": "quality"})
    assert not any(k == "run_veo" or k.startswith("veo_") for k in o), "Veo is retired"
    assert o["pattern_on"] is True and o["pattern"] == "structure"
    assert o["pattern_animation"] is None, "None = the pattern's own entrance"
    o = pipeline.normalize_options({"pattern": "Beads_Row", "pattern_animation": "GLITCH",
                                    "pattern_on": False})
    assert (o["pattern"], o["pattern_animation"], o["pattern_on"]) == ("beads_row", "glitch", False)
    o = pipeline.normalize_options({"pattern": "../../etc", "pattern_animation": "spin"})
    assert o["pattern"] == "structure" and o["pattern_animation"] is None


def test_pattern_off_means_no_pattern_and_says_so(tmp_path, monkeypatch, fake_omni):
    def build(*_a, **_kw):
        raise AssertionError("no pattern should be built")

    monkeypatch.setattr(pattern_ref, "build", build)
    ctx = _ctx(tmp_path, options=pipeline.normalize_options({"pattern_on": False}))
    asyncio.run(pipeline._build_pattern_ref(ctx))
    assert ctx.pattern_ref is None
    assert fake_omni["steps"][-1]["name"] == "Pattern: off"
    assert pipeline._entrance_prompt(ctx) is None


def test_a_picked_pattern_and_animation_reach_the_renderer(tmp_path, monkeypatch, fake_omni):
    seen: dict = {}

    def build(intro, out_dir, *, aspect, is_still=False, **kw):
        seen.update(kw)
        return _ref(tmp_path)

    monkeypatch.setattr(pattern_ref, "build", build)
    ctx = _ctx(tmp_path, options=pipeline.normalize_options(
        {"pattern": "beads_row", "pattern_animation": "glitch"}))
    asyncio.run(pipeline._build_pattern_ref(ctx))
    row = pattern_library.BUILTINS["beads_row"]
    assert seen["slug"] == "beads_row" and seen["layout"] == row.layout
    assert seen["animation"] == "glitch" and seen["source_file"] == "1.png"


def test_no_animation_means_no_entrance_and_no_veil_guard(tmp_path, fake_omni):
    import dataclasses

    ref = dataclasses.replace(_ref(tmp_path), animation="none")
    ctx = _ctx(tmp_path, ref=ref)
    assert pipeline._entrance_prompt(ctx) is None
    asyncio.run(pipeline._omni_edit(ctx, "omni", ctx.scenes[0], "hold", tmp_path / "omni",
                                    "Scene 1", first_prompt=pipeline._entrance_prompt(ctx)))
    assert [c["prompt"] for c in fake_omni["calls"]] == ["hold", "hold"]
    assert [f["veil"] for f in fake_omni["fronts"]] == [0.0, 0.0]
    # Any other animation does enter, with its own sentence.
    glitch = dataclasses.replace(_ref(tmp_path), animation="glitch")
    p = pipeline._entrance_prompt(_ctx(tmp_path, ref=glitch))
    assert p and "glitch in" in p and "BEHIND the speaker" in p


def test_an_unknown_pattern_is_refused_at_start(monkeypatch):
    from fastapi import HTTPException

    async def run_row(run_id):
        return {"metadata": {}}

    async def missing(slug):
        return None

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(omni, "providers", lambda: ["vertex"])
    monkeypatch.setattr(pattern_library, "get", missing)
    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(1, {"target": "scene_0", "pattern": "u-gone-abc123"}))
    assert err.value.status_code == 400 and "u-gone-abc123" in err.value.detail


# ---------------------------------------------------------------------------
# An auto-placed pattern (1.png, or an upload allowed several places)
# ---------------------------------------------------------------------------

AUTO_REPORT = {"mode": "auto", "seed": "rebrand:7:beads_row", "reason": "near-best",
               "picked": {"index": 3, "label": "top-right band", "layout": {}},
               "pool": [{"index": 3, "label": "top-right band", "p": 0.6},
                        {"index": 2, "label": "top-left band", "p": 0.4}],
               "candidates": [{"index": 3, "label": "top-right band", "rel": 1.0},
                              {"index": 2, "label": "top-left band", "rel": 0.9}]}


@pytest.fixture
def auto_env(tmp_path, monkeypatch, fake_omni):
    """The colours, the planner and the renderer faked around the real
    _build_pattern_ref: records what the planner and build() are handed."""
    seen: dict = {}
    colours = pattern_ref.Colours(ground=GROUND, ink=INK, fell_back=False, frame=None,
                                  colour="#2ed8e8")
    pick = pattern_library.BUILTINS["beads_row"].candidates[3]          # top-right band

    def dig(intro, out_dir, *, is_still=False):
        seen["dig"] = {"intro": intro, "out_dir": out_dir, "is_still": is_still}
        return colours

    def choose(candidates, frame, person, *, seed, slug, mask=None, colour=None, work_dir=None):
        seen["choose"] = {"candidates": candidates, "frame": frame, "person": person, "seed": seed,
                          "slug": slug, "mask": mask, "colour": colour, "work_dir": work_dir}
        return pick, dict(AUTO_REPORT)

    def build(intro, out_dir, *, aspect, is_still=False, **kw):
        seen["build"] = kw
        return dataclasses.replace(_ref(tmp_path), slug=kw["slug"], layout=kw["layout"],
                                   animation=kw["animation"],
                                   where=pattern_ref.where_text(kw["layout"]))

    monkeypatch.setattr(pattern_ref, "dig_colours", dig)
    monkeypatch.setattr(pattern_planner, "choose", choose)
    monkeypatch.setattr(pattern_ref, "build", build)
    seen.update(colours=colours, pick=pick)
    return seen


def test_an_auto_pattern_is_placed_on_the_first_scene_and_built_there(tmp_path, auto_env, fake_omni):
    ctx = _ctx(tmp_path, options=pipeline.normalize_options({"pattern": "beads_row"}))
    ctx.frames["th_first"] = _png(tmp_path / "base" / "th_first.png")
    asyncio.run(pipeline._build_pattern_ref(ctx))

    row = pattern_library.BUILTINS["beads_row"]
    plan = auto_env["choose"]
    # Planned on the reel's first frame, with the speaker's mask from it, in
    # the pattern's own (intro) colour, seeded by the experiment + pattern.
    assert plan["frame"] == ctx.frames["th_first"]
    assert fake_omni["masks"] == [ctx.frames["th_first"]] and plan["person"] == "PERSON-MASK"
    assert plan["candidates"] == row.candidates and plan["slug"] == "beads_row"
    assert plan["colour"] == "#2ed8e8" and plan["seed"] == "rebrand:7:beads_row"
    # Built ONCE, in the drawn place, with the colours already dug.
    built = auto_env["build"]
    assert built["layout"] == auto_env["pick"] and built["colours"] is auto_env["colours"]
    assert built["slug"] == "beads_row" and built["source_file"] == "1.png"
    assert built["animation"] == "right_to_left", "a top-right band sweeps in from the right"
    assert ctx.pattern_ref.layout == auto_env["pick"]
    assert ctx.placement_report["picked"]["label"] == "top-right band"
    # The step says where it went and why; the pattern step follows as before.
    placed = next(s for s in fake_omni["steps"] if s["name"].startswith("Pattern placed"))
    assert placed["name"] == "Pattern placed: top-right band (auto)" and placed["status"] == "done"
    assert "top-right band 60%" in placed["detail"] and "top-left band 40%" in placed["detail"]
    assert fake_omni["steps"][-1]["name"].startswith("Pattern: beads_row")
    assert "top-right corner" in fake_omni["steps"][-1]["detail"]


def test_a_per_clip_run_places_the_pattern_on_its_own_clip(tmp_path, auto_env, fake_omni):
    ctx = _ctx(tmp_path, options=pipeline.normalize_options({"pattern": "beads_row",
                                                            "target": "scene_1"}))
    ctx.frames["th_first"] = _png(tmp_path / "base" / "th_first.png")
    other = pipeline.Segment(kind="scene", index=1, source=tmp_path / "scene1.mp4", is_still=False,
                             base=tmp_path / "base" / "scene1.mp4")
    ctx.scenes.append(other)
    ctx.target_seg = other
    asyncio.run(pipeline._build_pattern_ref(ctx))
    frame = auto_env["choose"]["frame"]
    assert frame == tmp_path / "pattern" / "plan_scene1_first.png", "that clip's own first frame"
    assert fake_omni["masks"] == [frame]


def test_the_runs_own_animation_is_kept_for_an_auto_pattern(tmp_path, auto_env, fake_omni):
    ctx = _ctx(tmp_path, options=pipeline.normalize_options({"pattern": "beads_row",
                                                            "pattern_animation": "glitch"}))
    ctx.frames["th_first"] = _png(tmp_path / "base" / "th_first.png")
    asyncio.run(pipeline._build_pattern_ref(ctx))
    assert auto_env["build"]["animation"] == "glitch"


def test_a_fixed_pattern_is_never_planned(tmp_path, auto_env, fake_omni):
    ctx = _ctx(tmp_path, options=pipeline.normalize_options({"pattern": "beads_field"}))
    ctx.frames["th_first"] = _png(tmp_path / "base" / "th_first.png")
    asyncio.run(pipeline._build_pattern_ref(ctx))
    assert "choose" not in auto_env and "dig" not in auto_env and ctx.placement_report is None
    assert "colours" not in auto_env["build"]
    assert auto_env["build"]["layout"] == pattern_library.BUILTINS["beads_field"].layout
    assert not any(s["name"].startswith("Pattern placed") for s in fake_omni["steps"])


def test_a_fixed_right_strip_enters_from_the_right(tmp_path, monkeypatch, auto_env, fake_omni):
    """An upload allowed only the right strip is fixed, not auto — its
    entrance is still aimed at the edge it grows from, exactly as the same
    strip drawn by the planner would be; the run's own pick still wins."""
    right = pattern_ref.Layout(placement="right", scale=2.0, reach=0.5)
    entry = pattern_library.PatternEntry(
        slug="u-right-abc123", name="Right", source="upload", description="",
        layout=right, default_animation="left_to_right", source_file="right.png")
    assert not entry.auto

    async def get(slug):
        return entry

    monkeypatch.setattr(pattern_library, "get", get)
    ctx = _ctx(tmp_path, options=pipeline.normalize_options({"pattern": "structure"}))
    asyncio.run(pipeline._build_pattern_ref(ctx))
    assert auto_env["build"]["layout"] == right and "choose" not in auto_env
    assert auto_env["build"]["animation"] == "right_to_left"
    ctx = _ctx(tmp_path, options=pipeline.normalize_options(
        {"pattern": "structure", "pattern_animation": "left_to_right"}))
    asyncio.run(pipeline._build_pattern_ref(ctx))
    assert auto_env["build"]["animation"] == "left_to_right", "the run's own pick"


def test_the_fixed_builtins_keep_their_own_entrances(tmp_path, auto_env, fake_omni):
    for slug in ("structure", "beads_field", "blueprint", "grid"):
        ctx = _ctx(tmp_path, options=pipeline.normalize_options({"pattern": slug}))
        asyncio.run(pipeline._build_pattern_ref(ctx))
        assert auto_env["build"]["animation"] == pattern_library.BUILTINS[slug].default_animation


def test_the_placement_report_reaches_the_evidence(tmp_path, monkeypatch):
    ref = _ref(tmp_path)
    ctx = _ctx(tmp_path, ref=ref, options=pipeline.normalize_options({"run_veo": False}))
    ctx.placement_report = dict(AUTO_REPORT)
    _, evidence = _run_with(tmp_path, monkeypatch, ctx)
    assert evidence["omni_pattern"]["placement_report"] == AUTO_REPORT
    rest = {k: v for k, v in evidence["omni_pattern"].items() if k != "placement_report"}
    assert rest == ref.public()


REAL_MASKS = all(pattern_ref._mask_path(s).exists() for s in pattern_library.BUILTINS)


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_the_real_planner_places_the_real_row(tmp_path, monkeypatch, fake_omni):
    """No fakes between the pipeline, the planner and the renderer: a
    letterboxed frame puts 1.png into a bar, and the report is JSON."""
    import numpy as np

    def dig(intro, out_dir, *, is_still=False):
        return pattern_ref.Colours(GROUND, INK, False, None, GROUND)

    monkeypatch.setattr(pattern_ref, "dig_colours", dig)
    frame = np.full((1280, 720, 3), 128, np.uint8)
    frame[:300] = 0
    frame[-300:] = 0
    Image.fromarray(frame).save(tmp_path / "th_first.png")
    person = np.zeros((1280, 720), np.float32)
    person[450:620, 290:430] = 1.0
    person[620:980, 240:480] = 1.0
    fake_omni["mask"] = person
    ctx = _ctx(tmp_path, options=pipeline.normalize_options({"pattern": "beads_row"}))
    ctx.frames["th_first"] = tmp_path / "th_first.png"
    asyncio.run(pipeline._build_pattern_ref(ctx))
    report = ctx.placement_report
    assert report["reason"] == "near-best", report
    assert ctx.pattern_ref.layout.placement in ("top", "bottom")
    assert ctx.pattern_ref.layout in pattern_library.BUILTINS["beads_row"].candidates
    assert ctx.pattern_ref.path.exists()
    json.dumps(report)
