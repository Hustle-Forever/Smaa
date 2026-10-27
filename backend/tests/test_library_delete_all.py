"""Tests for "Delete works for EVERY Library type" (poster already covered by
test_poster_delete.py).

The Library grid shows poster / poster_human / animation / talking_head_v2 /
video_editor. Each now has a hard-delete that removes the generation row(s) + the
storage it OWNS + the two sibling tables (non-published scheduled IG posts +
roadmap state), while PRESERVING published IG posts and any Save-to-assets copy.

Dependency-light: the service checks import only the three pure store/service
modules; route + frontend checks are source-string guards (no DB, no network),
in the same style as test_card_animation / test_poster_delete.

Runs standalone (``cd backend && python tests/test_library_delete_all.py``) and
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


# --- service-layer delete primitives -----------------------------------------

def test_animation_store_delete_is_async_hard_delete():
    from app.services import yas_animation_store as a

    assert inspect.iscoroutinefunction(a.delete)
    src = _src(a.delete)
    assert "DELETE FROM yas_animation_generations" in src
    assert "RETURNING *" in src


def test_th_v2_delete_run_deletes_by_parent_linkage_not_pointers():
    from app.services import yas_talking_head_v2 as th

    assert inspect.iscoroutinefunction(th.delete_run)
    src = _src(th.delete_run)
    # Child rows are found by the parent linkage (catches regen-orphaned rows),
    # NOT by metadata pointers; animations come by the FK; run row guarded by mode.
    assert "metadata->>'parent_talking_head_id'" in src
    assert "FROM yas_animations WHERE yas_generation_id" in src
    assert "mode = %s" in src  # run row delete is mode-guarded
    # The ::int cast is regex-guarded (CASE short-circuits) so a stray non-numeric
    # metadata value can't abort the delete query.
    assert "~ '^-?[0-9]+$'" in src
    # The stitched composition (local file) is cleaned up too.
    assert "composition_path" in src


def test_ve_delete_project_removes_render_dir_only():
    from app.services import yas_video_editor as ve

    assert inspect.iscoroutinefunction(ve.delete_project)
    src = _src(ve.delete_project)
    assert "DELETE FROM yas_generations WHERE id = %s AND mode = %s" in src
    # Only the id-scoped project render dir is removed (never shared bin caches).
    assert "VE_ROOT / str(project_id)" in src
    assert "rmtree" in src


# --- routes registered (DELETE), one per type --------------------------------

def test_all_library_delete_routes_registered():
    from app.routers.yas_producer import router

    want = {
        "/api/yas-producer/poster-human/{gen_id}",
        "/api/yas-producer/animation/{anim_id}",
        "/api/yas-producer/talking-head-v2/{run_id}",
        "/api/yas-producer/video-editor/{project_id}",
        "/api/yas-producer/poster/{gen_id}",  # the original, still there
    }
    methods: dict[str, set] = {}
    for r in router.routes:
        if r.path in want:
            methods.setdefault(r.path, set()).update(r.methods or set())
    for p in want:
        assert "DELETE" in methods.get(p, set()), f"missing DELETE {p} -> {methods.get(p)}"


def test_routes_clean_storage_and_siblings():
    src = _read("backend/app/routers/yas_producer.py")
    # Shared sibling-cleanup helper used by all the new routes.
    assert "async def _delete_library_siblings(" in src
    assert "delete_by_ref(tool, ref_id)" in src
    assert "roadmap_state.delete(tool, ref_id)" in src
    # poster_human: deletes its animations (storage + rows) + the gen row.
    ph = src[src.index("async def poster_human_delete(") : src.index("async def animation_delete(")]
    assert "FROM yas_animations WHERE yas_generation_id" in ph
    assert "DELETE FROM yas_animations WHERE yas_generation_id" in ph
    assert 'row.get("mode") not in (None, "poster_human", "template_fill")' in ph
    assert '_delete_library_siblings("poster_human"' in ph
    # animation: deletes BOTH frame and video objects.
    an = src[src.index("async def animation_delete(") : src.index("async def th_v2_delete(")]
    assert "frame_storage_bucket" in an and "video_storage_bucket" in an
    assert '_delete_library_siblings("animation"' in an
    # th_v2 + ve delegate the heavy work to the service, then sibling cleanup.
    assert "th_v2_service.delete_run(run_id)" in src
    assert '_delete_library_siblings("talking_head_v2"' in src
    assert "ve_service.delete_project(project_id)" in src
    assert '_delete_library_siblings("video_editor"' in src


# --- frontend wiring guards ---------------------------------------------------

def test_api_client_has_all_delete_methods():
    src = _read("frontend/src/lib/api.ts")
    assert "`/api/yas-producer/poster-human/${genId}`" in src
    assert "`/api/yas-producer/animation/${animId}`" in src
    assert "`/api/yas-producer/talking-head-v2/${runId}`" in src
    assert "`/api/yas-producer/video-editor/${projectId}`" in src
    # 5 delete: methods total (poster + the 4 new).
    assert src.count("delete: (") >= 5


def test_library_dispatch_covers_every_type():
    src = _read("frontend/src/components/yas/YasLibrary.tsx")
    assert "async function deleteLibraryItem(type: YasLibraryType, id: number)" in src
    for t, call in [
        ('case "poster":', "posterOnly.delete(id)"),
        ('case "poster_human":', "posterHuman.delete(id)"),
        ('case "animation":', "animation.delete(id)"),
        ('case "talking_head_v2":', "talkingHeadV2.delete(id)"),
        ('case "video_editor":', "videoEditor.delete(id)"),
    ]:
        assert t in src and call in src, f"missing dispatch {t} / {call}"
    assert "store.remove(type, id)" in src


def test_specialized_detail_modals_have_delete_button():
    th = _read("frontend/src/components/yas/TalkingHeadV2LibraryDetail.tsx")
    assert "onDelete?:" in th and "{deleting ? \"Deleting…\" : \"Delete\"}" in th
    assert "useToast" in th
    ve = _read("frontend/src/components/yas/VideoEditorLibraryDetail.tsx")
    assert "onDelete?:" in ve and "{deleting ? \"Deleting…\" : \"Delete\"}" in ve
    assert "useToast" in ve


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} library-delete-all tests passed.")
