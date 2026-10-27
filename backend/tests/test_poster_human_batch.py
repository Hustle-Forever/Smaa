"""
Guards that "Create Poster + Human" produces a 4-variant (2x2) grid via the SAME
two-stage machinery as Create Poster / Human Character — reusing the concept
helpers + PosterStyle + gemini_image_multipart — while keeping its storage on
yas_generations (this tool's table).

Both modes return a {items:[...]} batch envelope:
  - style_inspired : Stage-1 gpt-4o casts N different COMPOSITIONS of the SAME
    copy (POSTER_HUMAN_STYLE.concepts_system) -> Stage-2 render each with
    POSTER_HUMAN_STYLE.gemini_stage2_system + the real logo part.
  - template_fill  : no concept stage (fixed template) -> render the template+face
    prompt N times.
Plus a per-card Explore (/poster-human/variants) and a shared variant_set_id.

Pure source-inspection + string assembly (no DB/network), so it runs standalone:

    cd backend && python tests/test_poster_human_batch.py

It is ALSO pytest-compatible (every check is a ``test_*`` function).
"""
import inspect
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

_REPO_ROOT = _BACKEND_ROOT.parent

from app.services import yas_poster_prompts as pp   # noqa: E402
from app.routers import yas_producer as yp          # noqa: E402


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. POSTER_HUMAN_STYLE is a valid PosterStyle with a split concept/render system
# ---------------------------------------------------------------------------

def test_poster_human_style_shape():
    s = pp.POSTER_HUMAN_STYLE
    assert s.key == "poster_human"
    assert s.concepts_system and s.gemini_stage2_system
    assert isinstance(s.variation_nudges, tuple) and len(s.variation_nudges) >= 1
    # concepts_system = "what to depict" (JSON concepts + [TEXT TO RENDER]).
    assert "STRICT JSON" in s.concepts_system and "[TEXT TO RENDER]" in s.concepts_system
    # render system = "how to draw" (place attached logo + aspect-neutral).
    low = s.gemini_stage2_system.lower()
    assert "attached" in low and "logo" in low
    assert "aspect ratio named in the render directive" in low


def test_concepts_system_pins_text_verbatim():
    # All N variants share the SAME words — only the composition varies.
    s = pp.POSTER_HUMAN_STYLE.concepts_system
    low = s.lower()
    assert "verbatim" in low
    assert "same" in low and ("composition" in low or "layout" in low)


def test_poster_human_user_message_builder():
    um = pp.build_poster_human_user_message("HEADLINE: WOMEN IN TECH", 4)
    assert "exactly 4" in um
    assert "same words" in um.lower() or "verbatim" in um.lower()


# ---------------------------------------------------------------------------
# 2. Backend endpoints are batch-based and reuse the shared machinery
# ---------------------------------------------------------------------------

def test_generate_is_two_stage_batch():
    src = inspect.getsource(yp.generate)
    assert "_poster_human_concepts" in src        # Stage 1
    assert "_poster_human_batch" in src            # Stage 2
    assert "POSTER_VARIANT_COUNT" in src           # 4 variants
    assert 'return {"items"' in src                # batch envelope


def test_stage2_render_uses_split_style_system():
    # The render MUST pass the split-out Stage-2 system (rules no longer live in
    # the concept text).
    assert "POSTER_HUMAN_STYLE.gemini_stage2_system" in inspect.getsource(yp._render_ph_variant)


def test_concept_loop_reuses_shared_helpers():
    src = inspect.getsource(yp._poster_human_concepts)
    assert "build_poster_human_user_message" in src
    assert "parse_concepts" in src and "dedupe_concepts" in src
    assert "POSTER_HUMAN_STYLE.concepts_system" in src


def test_template_fill_is_batch():
    src = inspect.getsource(yp.template_fill)
    assert "_render_ph_template_variant" in src
    assert "_finalize_ph_variants" in src
    assert "POSTER_VARIANT_COUNT" in src
    assert 'return {"items"' in src


def test_explore_endpoint_reuses_variation_helpers():
    src = inspect.getsource(yp.poster_human_variants)
    assert "build_variation_user_message" in src
    assert "parent_image_path" in src          # anchors riffs on the parent poster
    assert 'return {"items"' in src


def test_variant_set_id_wired():
    # Column ensured idempotently + selected in recent/get so the grid can group.
    assert "ADD COLUMN IF NOT EXISTS variant_set_id" in inspect.getsource(yp._ensure_ph_variant_col)
    assert "g.variant_set_id" in inspect.getsource(yp.list_recent)
    assert "g.variant_set_id" in inspect.getsource(yp.get_generation)


def test_batch_route_registered():
    paths = {r.path for r in yp.router.routes}
    assert "/api/yas-producer/poster-human/variants" in paths


# ---------------------------------------------------------------------------
# 3. Frontend wiring (source-string guards, like test_animation_variants)
# ---------------------------------------------------------------------------

def test_api_client_batch_methods():
    src = _read("frontend/src/lib/api.ts")
    assert 'http<YasGenerationBatchResponse>("/api/yas-producer/poster-human/generate"' in src
    assert 'http<YasGenerationBatchResponse>("/api/yas-producer/poster-human/template-fill"' in src
    assert 'http<YasGenerationBatchResponse>("/api/yas-producer/poster-human/variants"' in src
    assert "variants: (parentId: number)" in src
    types = _read("frontend/src/lib/types.ts")
    assert "export interface YasGenerationBatchResponse" in types
    assert "variant_set_id?" in types


def test_creator_renders_2x2_grid_with_explore():
    src = _read("frontend/src/components/yas/PosterHumanCreator.tsx")
    assert "batchStack" in src and "currentBatch" in src
    assert "grid grid-cols-2" in src
    assert "VariantCard" in src
    assert "handleExplore" in src and "handleSaveCard" in src and "handleRemoveLogo" in src
    assert "posterHuman.variants(" in src


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = sorted(
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())


# ---------------------------------------------------------------------------
# OUTPUT SHAPE
#
# Create Poster + Human rendered everything square, because nothing ever asked
# it not to. The three formats are now the SAME three Create Poster offers,
# validated against the same tuple and sent to the model the same way: a text
# directive AND the Gemini ImageConfig parameter, which is the half that
# actually forces it.
# ---------------------------------------------------------------------------

def test_both_poster_human_requests_take_an_aspect():
    for model in (yp.GenerateRequest, yp.TemplateFillRequest):
        assert "aspect" in model.model_fields, f"{model.__name__} has no aspect"
        assert model.model_fields["aspect"].default == "1:1", (
            "the default has to be the shape every existing client already "
            "gets, or an old client's posters silently change shape"
        )


def test_an_unknown_aspect_is_refused_not_corrected():
    """A silently-corrected shape is worse than a refusal: the caller gets a
    square poster and no reason for it."""
    import pytest
    from fastapi import HTTPException

    assert yp._clean_poster_aspect("9:16") == "9:16"
    assert yp._clean_poster_aspect(None) == "1:1"
    assert yp._clean_poster_aspect("") == "1:1"
    with pytest.raises(HTTPException) as exc:
        yp._clean_poster_aspect("3:4")          # a Human Character shape
    assert exc.value.status_code == 400


def test_the_variant_renderer_sends_the_api_parameter_not_only_the_sentence():
    src = inspect.getsource(yp._render_ph_variant)
    assert "aspect_ratio=aspect_ratio" in src, (
        "a render directive in the prompt ASKS for a shape; ImageConfig is "
        "what makes it come back that way"
    )
    assert "render_directive" in src, "both halves, not one"


def test_the_batch_derives_the_directive_from_the_shape():
    sig = inspect.signature(yp._poster_human_batch)
    assert "aspect" in sig.parameters
    assert "render_directive" not in sig.parameters, (
        "callers pass the shape; a pre-built sentence let the directive and "
        "the API parameter disagree"
    )
    src = inspect.getsource(yp._poster_human_batch)
    assert "_render_directive_for(aspect)" in src and "aspect_ratio=aspect" in src


def test_template_fill_states_its_aspect_because_the_default_is_wide():
    """`gemini_image` defaults to 16:9. Every template fill was asking for a
    wide image while the grid drew it in a square cell."""
    src = inspect.getsource(yp._render_ph_template_variant)
    assert "aspect_ratio=aspect" in src


def test_explore_and_remove_logo_inherit_the_parents_shape():
    for fn in (yp.poster_human_variants, yp.poster_human_remove_logo):
        src = inspect.getsource(fn)
        assert "_poster_aspect_of(parent)" in src, (
            f"{fn.__name__} would hand back a square child of a tall poster"
        )
        assert "width, height" in src, (
            "the shape is read off the parent's PIXELS, so the query has to "
            "select them"
        )


def test_human_character_formats_are_forced_not_requested():
    src = inspect.getsource(yp.human_character_generate)
    assert "aspect_ratio=aspect" in src, (
        "Human Character sent a text directive and nothing else, so its three "
        "formats were a wish"
    )


# ---------------------------------------------------------------------------
# ONE naming convention, ONE control
# ---------------------------------------------------------------------------

_CREATORS = (
    "frontend/src/components/yas/PosterCreator.tsx",
    "frontend/src/components/yas/PosterHumanCreator.tsx",
    "frontend/src/components/yas/HumanCharacterCreator.tsx",
)


def test_every_creator_draws_the_shared_format_picker():
    for rel in _CREATORS:
        src = _read(rel)
        assert "<FormatPicker" in src, f"{rel} hand-rolls the format control"
        assert 'from "./FormatPicker"' in src


def test_the_buttons_say_the_ratio_not_a_nickname():
    """"Square", "Story" and "Wide" were three invented words for three
    numbers, and they made one choice look like a different choice on each
    screen. The ratio is what the model is given, so it is what the button
    says; the line underneath carries what the shape is FOR."""
    picker = _read("frontend/src/lib/aspect.ts")
    for nickname in ('label: "Square"', 'label: "Story"', 'label: "Wide"'):
        assert nickname not in picker, nickname
    for ratio in ('label: "1:1"', 'label: "9:16"', 'label: "16:9"'):
        assert ratio in picker
    # The purposes the reader actually needs are kept.
    assert "feed post" in picker
    assert "Stories and Reels" in picker
    assert "screens and slides" in picker
