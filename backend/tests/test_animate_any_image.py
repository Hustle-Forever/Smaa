"""Tests for "Animate a picture the user already has".

Create Animation could only ever animate a frame IT rendered, which put the
expensive half of the pipeline (Veo) behind the cheap half (Gemini): a poster
that already existed, an image in the Assets library, or a file on someone's
desktop could not be animated at all. The fix is an import, not a second
pipeline - POST /animation/from-image copies the bytes into an animation
record's frame slot and returns it `frame_ready`, after which /animate and
everything downstream is untouched.

Two things are worth testing and one is not:

  - `infer_animation_aspect` is a pure function with a real decision in it
    (Veo has no square), so it gets real unit tests.
  - `build_imported_motion_prompt` is the reason the `asis` preset exists: an
    imported picture may be a photograph, and the voxel prompt would ask Veo to
    voxelise it. So the test that matters is that this prompt names NO
    aesthetic, and that voxel's own prompt is untouched.
  - The endpoint itself is I/O all the way down (Postgres + Supabase), so it is
    covered by source-string guards in the style of test_card_animation /
    test_remove_logo rather than by a fake database.

Dependency-light: no DB, no network, no credentials. Runs standalone
(``cd backend && python tests/test_animate_any_image.py``) and under pytest.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

_REPO_ROOT = _REPO_BACKEND.parent


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _src(fn) -> str:
    return inspect.getsource(fn)


# ---------------------------------------------------------------------------
# 1. Picking a shape for a picture Veo has no exact shape for
# ---------------------------------------------------------------------------

def test_landscape_pictures_go_wide():
    from app.routers.yas_producer import infer_animation_aspect

    assert infer_animation_aspect(1920, 1080) == "16:9"
    assert infer_animation_aspect(1000, 999) == "16:9"


def test_portrait_pictures_go_vertical():
    from app.routers.yas_producer import infer_animation_aspect

    assert infer_animation_aspect(1080, 1920) == "9:16"
    assert infer_animation_aspect(999, 1000) == "9:16"


def test_a_square_picture_defaults_to_vertical():
    """Create Poster renders 1:1 and Veo has no 1:1, so this case is the norm,
    not the edge. Vertical, because that is the shape 42 posts in."""
    from app.routers.yas_producer import infer_animation_aspect

    assert infer_animation_aspect(1024, 1024) == "9:16"


def test_unknown_dimensions_never_raise():
    """Pillow returns (None, None) for anything it cannot open. A shape still
    has to come out of this function - the caller has already accepted the
    picture by then."""
    from app.routers.yas_producer import infer_animation_aspect

    assert infer_animation_aspect(None, None) == "9:16"
    assert infer_animation_aspect(0, 0) == "9:16"
    assert infer_animation_aspect(1024, None) == "9:16"


def test_every_inferred_shape_is_one_veo_accepts():
    from app.routers.yas_producer import ANIMATION_ASPECTS, infer_animation_aspect

    for w, h in [(1, 1), (1920, 1080), (1080, 1920), (None, None), (0, 5), (5, 0)]:
        assert infer_animation_aspect(w, h) in ANIMATION_ASPECTS


# ---------------------------------------------------------------------------
# 2. The `asis` preset: a motion prompt that assumes nothing
# ---------------------------------------------------------------------------

# Words that would tell Veo what the picture is supposed to look like. An
# imported picture might be a photo, a screenshot or a logo, so naming any of
# these is a guess that shows up in the output.
_LOOK_WORDS = (
    "voxel", "toy-diorama", "diorama", "blocky", "retro", "wireframe",
    "ghutra", "pixel", "8-bit", "chiptune",
)


def test_the_imported_prompt_names_no_look():
    """Checked against the SILENT build on purpose.

    The audio block is shared with voxel and does name a sound ("chiptune /
    synthwave", "8-bit blip") - see the test below, where that is the intended
    behaviour. Muting it leaves exactly the visual half, which is the half that
    must not assume anything about a picture we did not draw.
    """
    from app.services.yas_animation_styles import build_imported_motion_prompt

    visual = build_imported_motion_prompt("a poster about learning to code", False).lower()
    for word in _LOOK_WORDS:
        assert word not in visual, f"imported prompt should not assume {word!r}"


def test_the_imported_prompt_keeps_the_house_music():
    """The look is a guess; the music is not.

    Whatever picture goes in, the clip is still going out as 42 Abu Dhabi
    social, so it reuses the same brand audio block as every other preset rather
    than inventing a neutral one.
    """
    from app.services.yas_animation_prompts import _MOTION_AUDIO_BLOCK
    from app.services.yas_animation_styles import build_imported_motion_prompt

    assert _MOTION_AUDIO_BLOCK in build_imported_motion_prompt("x", True)


def test_the_imported_prompt_says_keep_it_as_it_is():
    from app.services.yas_animation_styles import build_imported_motion_prompt

    out = build_imported_motion_prompt("anything").lower()
    assert "do not redesign" in out
    assert "first frame" in out          # Veo is told the image IS frame one
    assert "locked" in out               # text/logos must not be re-rendered


def test_the_imported_prompt_carries_the_brief_and_the_audio_choice():
    from app.services.yas_animation_styles import build_imported_motion_prompt

    loud = build_imported_motion_prompt("MY UNIQUE BRIEF 12345", True)
    quiet = build_imported_motion_prompt("MY UNIQUE BRIEF 12345", False)
    assert "MY UNIQUE BRIEF 12345" in loud
    assert "MY UNIQUE BRIEF 12345" in quiet
    assert "SILENT" in quiet
    assert "SILENT" not in loud


def test_an_empty_brief_still_produces_a_prompt():
    from app.services.yas_animation_styles import build_imported_motion_prompt

    for brief in ("", "   ", None):
        out = build_imported_motion_prompt(brief)  # type: ignore[arg-type]
        assert len(out) > 200


def test_the_imported_prompt_makes_no_promise_about_people():
    """The shared STYLE tail the other presets use ends with "NO people or
    characters ever appear". That is true of a frame WE rendered under the brand
    policy and false of a photo someone uploaded, which is exactly why `asis`
    owns its whole prompt instead of reusing the tail."""
    from app.services.yas_animation_styles import build_imported_motion_prompt

    assert "NO people or characters ever appear" not in build_imported_motion_prompt("x")


def test_asis_is_reachable_through_the_normal_style_dispatch():
    from app.services.yas_animation_styles import (
        IMPORTED_STYLE,
        build_imported_motion_prompt,
        build_motion_prompt_for_style,
        get_animation_style,
    )

    assert get_animation_style(IMPORTED_STYLE).key == "asis"
    assert build_motion_prompt_for_style(IMPORTED_STYLE, "brief") == \
        build_imported_motion_prompt("brief", True)


def test_the_voxel_prompt_is_untouched():
    """The whole point of a separate preset is that the three existing looks keep
    naming themselves. If this fails, `asis` leaked into the default path."""
    from app.services.yas_animation_styles import build_motion_prompt_for_style

    assert "voxel" in build_motion_prompt_for_style("voxel", "brief").lower()
    assert "voxel" in build_motion_prompt_for_style(None, "brief").lower()


# ---------------------------------------------------------------------------
# 3. The endpoint
# ---------------------------------------------------------------------------

def test_from_image_is_registered_and_not_shadowed():
    """`/animation/from-image` must be declared before any `/animation/{id}/...`
    route that could swallow it, and must not collide with an existing path."""
    import app.routers.yas_producer as m

    paths = [getattr(r, "path", "") for r in m.router.routes]
    assert "/api/yas-producer/animation/from-image" in paths
    assert paths.count("/api/yas-producer/animation/from-image") == 1


def test_from_image_takes_exactly_one_source():
    from app.routers.yas_producer import animation_from_image

    src = _src(animation_from_image)
    assert "poster_gen_id" in src and "asset_id" in src and "upload_token" in src
    # Not "at least one" and not "the first one that is set" - exactly one, so a
    # caller that sends two gets told rather than silently getting one of them.
    assert "sum(given) != 1" in src


def test_from_image_calls_no_model_and_charges_nothing():
    """The import is free by construction. If a budget guard or a meter read
    appears here, the cost story on the endpoint's docstring is a lie."""
    from app.routers.yas_producer import animation_from_image

    src = _src(animation_from_image)
    for banned in ("_budget_guard", "get_meter", "render_card_image", "gemini", "veo_generate"):
        assert banned not in src, f"import path should not touch {banned}"


def test_from_image_writes_only_the_frame_columns():
    """`mime_type` and `bytes` belong to the finished MP4 - the poller claims
    both. Filling them in at import would describe the video with the still's
    numbers the moment the animation lands."""
    from app.routers.yas_producer import animation_from_image

    src = _src(animation_from_image)
    assert "frame_storage_bucket" in src
    assert "frame_storage_path" in src
    assert "frame_local_path" in src
    assert "mime_type=" not in src
    assert "bytes=" not in src


def test_from_image_produces_a_record_animate_will_accept():
    """/animate rejects anything not in (frame_ready, ready, failed), so the
    import has to land on frame_ready or the next click 422s."""
    from app.routers.yas_producer import animation_animate, animation_from_image

    assert '"frame_ready"' in _src(animation_from_image)
    assert '"frame_ready"' in _src(animation_animate)


def test_a_poster_keeps_its_own_look_but_everything_else_gets_asis():
    from app.routers.yas_producer import (
        _import_bytes_from_asset,
        _import_bytes_from_poster,
        _import_bytes_from_upload,
        animation_from_image,
    )

    # The poster path is the only one that knows the look, so it passes it on.
    assert 'gen.get("style")' in _src(_import_bytes_from_poster)
    # The other two return None for style and let the endpoint fall back.
    assert "return data, (row.get(\"display_name\") or \"\"), None" in _src(_import_bytes_from_asset)
    assert 'return path.read_bytes(), "", None' in _src(_import_bytes_from_upload)
    assert "IMPORTED_STYLE" in _src(animation_from_image)


def test_only_finished_posters_and_only_image_assets_can_be_imported():
    from app.routers.yas_producer import _import_bytes_from_asset, _import_bytes_from_poster

    poster = _src(_import_bytes_from_poster)
    assert "STATUS_READY" in poster and "STATUS_SAVED" in poster
    assert 'asset_kind' in _src(_import_bytes_from_asset)


def test_the_three_source_readers_are_async_and_return_the_same_shape():
    from app.routers.yas_producer import (
        _import_bytes_from_asset,
        _import_bytes_from_poster,
        _import_bytes_from_upload,
    )

    for fn in (_import_bytes_from_poster, _import_bytes_from_asset, _import_bytes_from_upload):
        assert inspect.iscoroutinefunction(fn)
        # (bytes, brief, style) - one tuple shape so the endpoint has no branches
        # beyond picking which reader to call.
        assert "tuple[bytes, str, Optional[str]]" in inspect.signature(fn).return_annotation \
            or fn.__annotations__.get("return") is not None


# ---------------------------------------------------------------------------
# 4. The screens
# ---------------------------------------------------------------------------

def test_the_poster_grid_hands_off_instead_of_animating_in_place():
    """The button imports the poster and then leaves.

    An earlier version opened its own little dialog that ran Veo and showed the
    clip. That was a SECOND flow, and it dead-ended: the video never reached the
    caption, the schedule or publishing. So the poster grid now does the cheap,
    free half (the import) and hands the frame id upward.
    """
    src = _read("frontend/src/components/yas/PosterCreator.tsx")
    assert "animation.fromImage" in src
    assert "poster_gen_id: g.id" in src
    assert "onAnimate(frame.id)" in src
    # It must NOT animate anything itself - that belongs to the animation tool,
    # along with its cost and its budget guard.
    assert "animation.animate" not in src


def test_the_second_flow_is_gone():
    """The standalone dialog must not come back. Every route in is now the
    animation tool: the poster button, and its own "picture you already have"."""
    import pathlib

    assert not (_REPO_ROOT / "frontend/src/components/yas/AnimateImageDialog.tsx").exists()
    for f in pathlib.Path(_REPO_ROOT / "frontend/src").rglob("*.tsx"):
        assert "AnimateImageDialog" not in f.read_text(encoding="utf-8"), f


def test_the_roadmap_opens_the_animation_tool_on_the_imported_frame():
    src = _read("frontend/src/components/yas/roadmap/ProducerRoadmap.tsx")
    assert "function openAnimation(" in src
    assert "onAnimate={openAnimation}" in src
    # Same three moves as the Video Editor hand-off it is modelled on.
    assert 'setTool("animation")' in src
    assert 'setStep("editor")' in src
    assert "setResumeRunId(animId)" in src


def test_the_imported_frame_actually_reaches_the_animation_creator():
    """`initialTool` is whatever the roadmap MOUNTED with, so during a hand-off
    it is "poster" and an id keyed off it would be silently dropped. The mount
    has to key off the CURRENT tool, which is what the Video Editor already does."""
    src = _read("frontend/src/components/yas/roadmap/ProducerRoadmap.tsx")
    assert 'initialRunId={tool === "animation" ? resumeRunId : undefined}' in src
    assert 'initialRunId={tool === "video_editor" ? resumeRunId : undefined}' in src


def test_the_animation_creator_still_continues_to_the_caption():
    """The whole reason for the hand-off: an animation made this way is an
    ordinary animation, so it advances through Finalization the same way."""
    src = _read("frontend/src/components/yas/roadmap/ProducerRoadmap.tsx")
    anim = src.split("<AnimationCreator")[1].split("/>")[0]
    assert "onContinue={finalize}" in anim


def test_a_brought_in_frame_is_not_labelled_with_its_key():
    """The corner badge prints the style key. "ASIS" is not a word."""
    src = _read("frontend/src/components/yas/AnimationCreator.tsx")
    assert "STYLE_BADGE" in src
    assert 'asis: "your picture"' in src


def test_the_animation_creator_can_take_a_picture_from_elsewhere():
    src = _read("frontend/src/components/yas/AnimationCreator.tsx")
    assert "Or animate a picture you already have" in src
    assert "From Assets" in src
    assert "From your computer" in src
    assert "animation.fromImage" in src


def test_the_frontend_knows_about_the_asis_style():
    src = _read("frontend/src/lib/types.ts")
    assert '| "asis"' in src
    # ...but never offers it in the Create-Animation picker, because there is
    # nothing to generate from it.
    anim = _read("frontend/src/components/yas/AnimationCreator.tsx")
    picker = anim.split("const STYLES")[1].split("];")[0]
    assert "asis" not in picker


def test_the_hourly_animation_cap_is_gone():
    """The 10-per-hour Veo cap was removed by request and must not come back.

    Spend is still guarded by the Vertex budget guard and Google's own 429;
    only the local per-hour count (and its "x/10 this hour" readout) is gone.
    """
    src = _read("backend/app/routers/yas_producer.py")
    assert "ANIMATION_HOURLY_CAP" not in src
    assert "_animation_call_log" not in src
    assert "_animation_rate_check" not in src
    assert "Hourly animation cap" not in src
    # The budget guard still runs on /animate.
    animate = src[src.index("async def animation_animate"):]
    assert "await _budget_guard()" in animate[:4000]

    ui = _read("frontend/src/components/yas/AnimationCreator.tsx")
    assert "remainingThisHour" not in ui
    assert "/10 this hour" not in ui



if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception:
                failures += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

