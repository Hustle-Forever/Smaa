"""Tests for the "Remove logo" (de-logo edit) feature.

A finished still can be re-rendered with the 42 brand mark removed, kept as a NEW
child record next to the original. The edit is image-to-image: the artwork is fed
back to Gemini with a remove-logo instruction and NO logo part — the exact inverse
of the add-logo path. The mechanism lives in ONE shared helper
(``card_animation.render_delogo_image``) that all surfaces reuse:

  * Create Poster        → POST /poster/{gen_id}/remove-logo
  * Poster + Human       → POST /poster-human/{gen_id}/remove-logo
  * Create-Animation     → POST /animation/{anim_id}/remove-logo  (the FRAME)
  * Library detail modal → reuses the poster / poster-human routes

These are dependency-light: the helper/prompt checks import only the two pure
service modules (``card_animation`` + ``poster_prompts``); route + frontend checks
are source-string guards in the same style as ``test_card_animation`` — no DB, no
network.

Runs standalone (``cd backend && python tests/test_remove_logo.py``) and pytest.
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

def test_remove_logo_prompt_constants_exist():
    from app.services import poster_prompts as pp

    assert isinstance(pp.REMOVE_LOGO_INSTRUCTION, str) and pp.REMOVE_LOGO_INSTRUCTION
    assert isinstance(pp.REMOVE_LOGO_SYSTEM, str) and pp.REMOVE_LOGO_SYSTEM
    instr = pp.REMOVE_LOGO_INSTRUCTION.lower()
    # Names the mark + the removal action + the "preserve everything else" rule.
    assert "42" in pp.REMOVE_LOGO_INSTRUCTION
    assert "remove" in instr
    assert "anything else" in instr or "everything else" in instr.replace("every other", "everything else")
    # OWNERSHIP framing is load-bearing: it stops the image model soft-refusing the
    # task as "watermark removal". Guard it so a future edit can't quietly drop it,
    # and keep the worst watermark-removal trigger verbs out of the prompt.
    sys_low = pp.REMOVE_LOGO_SYSTEM.lower()
    assert "own" in instr and "own" in sys_low, "must frame it as the user's OWN artwork"
    assert "inpaint" not in instr, "drop watermark-removal trigger language"
    assert "watermark" not in instr


# --- shared helper: image-to-image edit that OMITS the logo ------------------

def test_render_delogo_image_is_async_and_omits_logo():
    from app.services import card_animation as ca

    assert inspect.iscoroutinefunction(ca.render_delogo_image)
    body = _code_body(ca.render_delogo_image)
    # The whole point: no logo part is built on this path.
    assert "build_logo_parts" not in body, "de-logo must NOT add the logo"
    # It feeds the source image back with the remove instruction, via the shared
    # escalating renderer (a no-image/text reply re-issues once with an image-only
    # directive; a safety block surfaces). That wrapper calls render_card_image.
    assert "REMOVE_LOGO_INSTRUCTION" in body
    assert '("image", src_path)' in body
    assert "_render_edit_escalating(" in body


# --- routes registered (POST), one per logo-baking still surface -------------

def test_remove_logo_routes_registered():
    from app.routers.yas_producer import router

    want = {
        "/api/yas-producer/poster/{gen_id}/remove-logo",
        "/api/yas-producer/poster-human/{gen_id}/remove-logo",
        "/api/yas-producer/animation/{anim_id}/remove-logo",
    }
    methods: dict[str, set] = {}
    for r in router.routes:
        if r.path in want:
            methods.setdefault(r.path, set()).update(r.methods or set())
    for p in want:
        assert p in methods and "POST" in methods[p], f"missing POST {p} -> {methods.get(p)}"


def test_remove_logo_routes_use_shared_helper_and_keep_original():
    src = _read("backend/app/routers/yas_producer.py")
    # Every surface routes the edit through the one shared de-logo helper.
    assert src.count("card_animation.render_delogo_image(") == 3
    # Poster + poster-human keep the original (new child via parent linkage).
    assert "async def poster_remove_logo(" in src
    assert "async def poster_human_remove_logo(" in src
    assert "async def animation_remove_logo(" in src
    assert "parent_generation_id=gen_id" in src  # poster child links to original


# --- frontend wiring guards --------------------------------------------------

def test_api_client_has_remove_logo_methods():
    src = _read("frontend/src/lib/api.ts")
    assert "`/api/yas-producer/poster/${genId}/remove-logo`" in src
    assert "`/api/yas-producer/poster-human/${genId}/remove-logo`" in src
    assert "`/api/yas-producer/animation/${animId}/remove-logo`" in src
    assert src.count("removeLogo:") == 3


def test_creators_wire_remove_logo_button():
    poster = _read("frontend/src/components/yas/PosterCreator.tsx")
    assert "handleRemoveLogo" in poster
    assert "posterOnly.removeLogo(" in poster
    assert "onRemoveLogo" in poster

    human = _read("frontend/src/components/yas/PosterHumanCreator.tsx")
    # Per-card action after the 2x2-grid rework (was runRemoveLogo pre-batch).
    assert "handleRemoveLogo" in human
    assert "posterHuman.removeLogo(" in human
    assert "Remove logo" in human

    anim = _read("frontend/src/components/yas/AnimationCreator.tsx")
    assert "handleRemoveLogo" in anim
    assert "animation.removeLogo(" in anim
    # Only on the FRAME (before the Veo video locks the logo in). The 4-variant
    # grid rework makes Remove-logo a per-card action gated on the card's frame.
    assert "g.frame_url" in anim


def test_library_modal_wires_remove_logo_for_stills_only():
    src = _read("frontend/src/components/yas/YasLibrary.tsx")
    assert "onRemoveLogo" in src
    # Poster + poster_human stills only, never a finished video.
    assert 'openItem.type === "poster_human"' in src
    assert "!openItem.has_video" in src
    assert "posterOnly.removeLogo(id)" in src
    assert "posterHuman.removeLogo(id)" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} remove-logo tests passed.")
