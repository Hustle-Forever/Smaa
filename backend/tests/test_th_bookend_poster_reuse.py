"""
Guards that Talking Head Studio's auto-generated INTRO/OUTRO bookend cards use
the IDENTICAL recipe to the standalone Create Animation frame — Create Poster's
own Stage-1 concept brain + Stage-2 render brain, via the same shared
``card_animation`` primitives. There are exactly TWO intended differences:
(1) the concept count (Talking Head asks for n=1; Create Animation asks for
ANIMATION_VARIANT_COUNT); and (2) the SCRIPT's intro/outro words are pinned
VERBATIM on the card (Talking Head is scripted — the exact headline / subtitle,
incl. a URL / CTA, must render unchanged).

Everything else matches Create Animation — in particular the no-ref path has:
  - no wrapped "title card for a reel about ..." brief (topic is fed raw),
  - no edge-to-edge aspect note.

The shared ``ANIMATION_*`` constants + the standalone Create Animation tool must
stay UNTOUCHED, and the reference-picked (new_vibe / text_only) branches remain.

Pure source-inspection — no DB/network — so it runs standalone:

    cd backend && python tests/test_th_bookend_poster_reuse.py

It is ALSO pytest-compatible (every check is a ``test_*`` function).
"""
import inspect
import sys
from pathlib import Path

# tests/ -> backend/ is parents[1]; make ``import app...`` resolve from anywhere.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import yas_talking_head_v2 as th          # noqa: E402
from app.services import yas_talking_head_v2_prompts as thp  # noqa: E402
from app.services import yas_animation_prompts as ap         # noqa: E402


def _card_src() -> str:
    return inspect.getsource(th._render_card_animation_style)


# ---------------------------------------------------------------------------
# 1. The no-ref bookend path uses Create Poster's Stage-1 + Stage-2 (n=1)
# ---------------------------------------------------------------------------

def test_bookend_uses_create_poster_stack():
    src = _card_src()
    # Voxel PosterStyle resolved (gives concepts_system + gemini_stage2_system).
    assert "get_poster_style(DEFAULT_POSTER_STYLE)" in src
    # Stage-1 = Create Poster concept brain, ONE concept.
    assert "style.concepts_system" in src
    assert "build_concepts_user_message" in src and "parse_concepts" in src
    assert "n=1" in src
    # Stage-2 = Create Poster render brain.
    assert "style.gemini_stage2_system" in src


def test_bookend_dropped_the_animation_fork():
    src = _card_src()
    # The forked animation prompts must not drive the no-ref bookend.
    assert "ANIMATION_IMAGE_SYSTEM" not in src
    assert "TH_CARD_RENDER_SYSTEM" not in src


# ---------------------------------------------------------------------------
# 2. No Talking-Head-specific divergence in the no-ref path
# ---------------------------------------------------------------------------

def test_bookend_pins_verbatim_script_text():
    src = _card_src()
    # The one deliberate change vs Create Animation: the no-ref card swaps the
    # concept's invented text for the script's EXACT headline/subtitle (incl. a
    # URL/CTA), so the scripted words render unchanged.
    assert 'split("[TEXT TO RENDER]")' in src
    assert "text_block" in src  # the authoritative _th_text_block is appended


def test_bookend_render_is_not_edge_to_edge():
    src = _card_src()
    # The no-ref render must use the plain aspect note (matching Create Animation),
    # not the Talking-Head edge-to-edge variant. The plain call appears in the
    # no-ref branch; edge_to_edge=True remains only for the ref-picked branches.
    assert "card_animation.aspect_render_note(aspect)" in src


def test_reference_picked_branches_preserved():
    # new_vibe / text_only intentionally keep their TH-owned systems + edge-to-edge.
    src = _card_src()
    assert "TH_NEWVIBE_RENDER_SYSTEM" in src
    assert "TH_TEXT_ONLY_RENDER_SYSTEM" in src
    assert "edge_to_edge=True" in src  # still used by the ref-picked branches


# ---------------------------------------------------------------------------
# 3. Parity: TH bookend and Create Animation frame share the same recipe
# ---------------------------------------------------------------------------

def test_bookend_matches_create_animation_recipe():
    from app.routers.yas_producer import _render_animation_frame

    th_src = _card_src()
    ca_src = inspect.getsource(_render_animation_frame)
    # Both assemble the SAME shared card-render primitives.
    for token in ("build_logo_parts", "aspect_render_note", "render_card_image",
                  "gemini_stage2_system"):
        assert token in th_src, f"TH bookend missing shared primitive {token!r}"
        assert token in ca_src, f"Create Animation frame missing {token!r}"
    # The intended difference is the concept count: TH=1, CA=ANIMATION_VARIANT_COUNT.
    assert "n=1" in th_src
    assert "ANIMATION_VARIANT_COUNT" not in th_src


# ---------------------------------------------------------------------------
# 4. Dead TH constants removed; shared ANIMATION_* untouched (decoupling)
# ---------------------------------------------------------------------------

def test_dead_th_card_constants_removed():
    assert not hasattr(thp, "TH_CARD_RENDER_SYSTEM")
    assert not hasattr(thp, "build_th_card_image_user_message")


def test_shared_animation_constants_untouched():
    # Create Animation (and the animation-styles registry) still rely on these.
    assert hasattr(ap, "ANIMATION_IMAGE_SYSTEM")
    assert hasattr(ap, "ANIMATION_RENDER_SYSTEM")


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
        except Exception as exc:  # noqa: BLE001 — report every failure, keep going
            failures += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
