"""Tests for the "Refine" (guided image edit) feature.

A finished poster / animation FRAME can be re-rendered applying ONLY a user-described
change ("make the title bigger", "change the background to blue"), kept as a NEW child
record next to the original. It's the general case of "Remove logo": the artwork is fed
back to Gemini with the user's change carried in the final render directive, NO logo part.
The mechanism lives in ONE shared helper (``card_animation.render_refine_image``) reused by:

  * Create Poster    → POST /poster/{gen_id}/refine
  * Create-Animation → POST /animation/{anim_id}/refine  (the FRAME, before Veo)

Dependency-light: the helper/prompt checks import only the two pure service modules
(``card_animation`` + ``poster_prompts``); route + frontend checks are source-string
guards, same style as ``test_remove_logo`` — no DB, no network.

Runs standalone (``cd backend && python tests/test_refine.py``) and pytest.
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


def _code_body(fn) -> str:
    """Source of a function with its leading docstring stripped, so assertions are
    about executable code (a docstring that *mentions* a symbol won't false-match)."""
    src = inspect.getsource(fn)
    return src.split('"""')[2] if src.count('"""') >= 2 else src


# --- prompt constants --------------------------------------------------------

def test_refine_prompt_constants_exist():
    from app.services import poster_prompts as pp

    assert isinstance(pp.REFINE_REF_INSTRUCTION, str) and pp.REFINE_REF_INSTRUCTION
    assert isinstance(pp.REFINE_SYSTEM, str) and pp.REFINE_SYSTEM
    # Reference instruction frames the attached image as the base to reproduce.
    assert "base artwork" in pp.REFINE_REF_INSTRUCTION.lower()
    # System prompt forces an IMAGE reply (never a conversational text answer) —
    # this is what fixes the "model returns text, no image" failure.
    sysl = pp.REFINE_SYSTEM.lower()
    assert "always" in sysl and "image" in sysl and "never" in sysl and "text" in sysl


def test_refine_instruction_injects_change_and_guards():
    from app.services import poster_prompts as pp

    out = pp.refine_instruction("make the sky red")
    assert isinstance(out, str) and out
    # The user's change is quoted into the FINAL render directive verbatim.
    assert "make the sky red" in out
    low = out.lower()
    # Render directive + same-size guard + no-new-logo guardrail.
    assert "render" in low
    assert "dimensions" in low or "aspect" in low
    assert "logo" in low  # "Never add any new logo, watermark, or unrequested text."
    # Whitespace is stripped before quoting.
    assert pp.refine_instruction("  hi  ").count('"hi"') == 1


# --- shared helper: image-to-image edit carrying the user's change -----------

def test_render_refine_image_is_async_and_omits_logo():
    from app.services import card_animation as ca

    assert inspect.iscoroutinefunction(ca.render_refine_image)
    body = _code_body(ca.render_refine_image)
    # No logo part on the edit path (the source image already carries its branding).
    assert "build_logo_parts" not in body, "refine must NOT add the logo"
    # Feeds the source image back, ending on the change-carrying render directive,
    # via the shared escalating renderer — same proven shape as render_delogo_image
    # (and the same text-reply recovery / safety-block surfacing).
    assert "REFINE_REF_INSTRUCTION" in body
    assert '("image", src_path)' in body
    assert "refine_instruction(" in body
    assert "_render_edit_escalating(" in body


# --- routes registered (POST), one per refine-able still surface -------------

def test_refine_routes_registered():
    from app.routers.yas_producer import router

    want = {
        "/api/yas-producer/poster/{gen_id}/refine",
        "/api/yas-producer/animation/{anim_id}/refine",
    }
    methods: dict[str, set] = {}
    for r in router.routes:
        if r.path in want:
            methods.setdefault(r.path, set()).update(r.methods or set())
    for p in want:
        assert p in methods and "POST" in methods[p], f"missing POST {p} -> {methods.get(p)}"


def test_refine_routes_use_shared_helper_and_map_429():
    src = _read("backend/app/routers/yas_producer.py")
    # Both surfaces route the edit through the one shared refine helper.
    assert src.count("card_animation.render_refine_image(") == 2
    assert "async def poster_refine(" in src
    assert "async def animation_refine(" in src
    # Poster child keeps the original + records the refine lineage in the existing
    # (un-migrated) columns.
    assert 'retry_kind="refine"' in src
    assert "feedback_text=change" in src
    # A quota hit maps to a clean 429 (not a scary 502), like remove-logo.
    assert src.count("_is_resource_exhausted(exc)") >= 4  # 2 remove-logo + 2 refine routes (poster_human too)
    # Empty change is rejected before any model call.
    assert 'raise HTTPException(422, "describe what to change")' in src


# --- frontend wiring guards --------------------------------------------------

def test_api_client_has_refine_methods():
    src = _read("frontend/src/lib/api.ts")
    assert "`/api/yas-producer/poster/${genId}/refine`" in src
    assert "`/api/yas-producer/animation/${animId}/refine`" in src
    assert src.count("refine: (") == 2


def test_creators_wire_refine_button_and_panel():
    poster = _read("frontend/src/components/yas/PosterCreator.tsx")
    assert "handleRefine" in poster
    assert "posterOnly.refine(" in poster
    assert "onRefine" in poster
    # Selection + change text + in-flight id (session-only state).
    assert "refineTargetId" in poster
    assert "refineText" in poster
    assert "refiningId" in poster
    # The whole prompt field is rendered under the grid via the derived target.
    assert "refineTarget" in poster

    anim = _read("frontend/src/components/yas/AnimationCreator.tsx")
    assert "handleRefine" in anim
    assert "animation.refine(" in anim
    assert "onRefine" in anim
    assert "refineTargetId" in anim
    assert "refineText" in anim
    assert "refiningId" in anim
    # Refine the FRAME only (before the Veo video locks the change in).
    assert "target.frame_url" in anim


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} refine tests passed.")
