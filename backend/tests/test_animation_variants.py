"""Tests for "Animation Generate-frame → 4 variants + per-variant Explore".

The Animation creator used to render ONE hero frame per click. It now renders a
Midjourney-style batch of ANIMATION_VARIANT_COUNT frame variants (mirroring Create
Poster) and offers an "Explore" action (4 close riffs anchored on the chosen
frame). Each variant stays independently animatable (Veo i2v). NO schema change —
the 4-up grouping lives in frontend state; Explore children are plain frame_ready
records.

Dependency-light: the service checks import only ``app.routers.yas_producer`` and
inspect its functions; route + frontend checks are source-string guards (no DB, no
network), in the same style as test_card_animation / test_remove_logo.

Runs standalone (``cd backend && python tests/test_animation_variants.py``) and
under pytest.
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


# --- service-layer: the shared frame-render helper ----------------------------

def test_render_animation_frame_is_async_and_reuses_card_recipe():
    from app.routers.yas_producer import _render_animation_frame

    assert inspect.iscoroutinefunction(_render_animation_frame)
    src = _src(_render_animation_frame)
    # Reuses the shared card-render recipe (logo + aspect note + Gemini render).
    assert "build_logo_parts" in src
    assert "aspect_render_note" in src
    assert "render_card_image" in src
    # Explore anchor: when a parent frame is given it's sent as a reference part.
    assert "variation_ref_path" in src
    assert "VARIATION_REF_INSTRUCTION" in src
    # Per-variant isolation: starts 'generating', ends frame_ready/failed, never
    # raises (so a sibling in a gather() can still succeed).
    assert '"generating"' in src
    assert '"frame_ready"' in src
    assert '"failed"' in src


def test_finalize_animation_batch_splits_cost():
    from app.routers.yas_producer import _finalize_animation_batch

    assert inspect.iscoroutinefunction(_finalize_animation_batch)
    src = _src(_finalize_animation_batch)
    assert "frame_cost_usd" in src
    assert "frame_ready" in src


# --- generate-frame is now a batch -------------------------------------------

def test_variant_count_is_four():
    from app.routers.yas_producer import ANIMATION_VARIANT_COUNT

    assert ANIMATION_VARIANT_COUNT == 4


def test_generate_frame_renders_a_batch():
    from app.routers.yas_producer import animation_generate_frame

    src = _src(animation_generate_frame)
    # Stage-1 asks for N concepts (not 1), then renders them concurrently.
    assert "ANIMATION_VARIANT_COUNT" in src
    assert "asyncio.gather" in src
    assert "_render_animation_frame" in src
    assert "_finalize_animation_batch" in src
    # Returns a batch envelope, not a single record.
    assert 'return {"items"' in src


# --- Explore route mirrors poster_variants -----------------------------------

def test_animation_variants_route_explores_anchored_on_parent_frame():
    from app.routers.yas_producer import animation_variants

    assert inspect.iscoroutinefunction(animation_variants)
    src = _src(animation_variants)
    # Skips Stage 1: inherits the parent's stored prompt + nudges, anchors on the
    # parent FRAME image (frame_storage_*), renders the batch concurrently.
    assert "build_variation_user_message" in src
    assert "VARIATION_NUDGES" in src or "variation_nudges" in src
    assert "frame_storage_path" in src
    assert "variation_ref_path=parent_img" in src
    assert "asyncio.gather" in src
    assert 'return {"items"' in src


def test_both_batch_routes_registered():
    from app.routers.yas_producer import router

    want = {
        "/api/yas-producer/animation/generate-frame",
        "/api/yas-producer/animation/variants",
    }
    methods: dict[str, set] = {}
    for r in router.routes:
        if r.path in want:
            methods.setdefault(r.path, set()).update(r.methods or set())
    for p in want:
        assert "POST" in methods.get(p, set()), f"missing POST {p} -> {methods.get(p)}"


# --- no schema migration ------------------------------------------------------

def test_animation_store_has_no_parent_column():
    # The whole point of the frontend-only batch grouping: the shared Supabase
    # table is untouched. No parent/variant column was added.
    src = _read("backend/app/services/yas_animation_store.py")
    assert "parent_id" not in src
    assert "variant_set_id" not in src


# --- frontend wiring guards ---------------------------------------------------

def test_api_client_has_animation_batch_methods():
    src = _read("frontend/src/lib/api.ts")
    # generateFrame + variants both return the batch envelope.
    assert 'http<YasAnimationBatchResponse>("/api/yas-producer/animation/generate-frame"' in src
    assert 'http<YasAnimationBatchResponse>("/api/yas-producer/animation/variants"' in src
    assert "variants: (parentId: number)" in src
    types = _read("frontend/src/lib/types.ts")
    assert "export interface YasAnimationBatchResponse" in types


def test_animation_creator_wires_batch_explore_and_multi_poll():
    src = _read("frontend/src/components/yas/AnimationCreator.tsx")
    # 2×2 grid batch model (same presentation as Create Poster).
    assert "batchStack" in src
    assert "currentBatch" in src
    assert "grid grid-cols-2" in src
    assert "AnimVariantCard" in src
    # Per-card actions: animate in place + explore (anchored on that card).
    assert "async function handleExplore(" in src
    assert "async function runAnimate(" in src
    assert "animation.variants(" in src
    assert "Explore →" in src
    # Generalised poll across every animating variant (not just one).
    assert "animatingKey" in src
    # Remove-logo preserved (per-card).
    assert "handleRemoveLogo" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} animation-variants tests passed.")
