"""Tests for the Library "Delete poster" feature.

A poster (Create Poster) generation can be hard-deleted from the Library detail
modal. The delete removes it everywhere the local store is read (the Library
grid, the Create-Poster history strip, "Continue editing"):

  * ``yas_local_store.delete(gen_id)`` hard-deletes the row and RETURNs it so the
    router can clean up the Storage object,
  * ``DELETE /poster/{gen_id}`` (router) best-effort-deletes the Storage object
    then the row, gated to poster-mode records only, and
  * the frontend wires a poster-only "Delete" button that calls the endpoint and
    drops the tile from the cached grid.

These are dependency-light: the store check imports only ``yas_local_store`` (which
pulls in ``..db`` and nothing heavy); everything else is a source-string guard in
the same style as ``test_card_animation``'s reuse guards — no DB, no network.

Runs standalone (``cd backend && python tests/test_poster_delete.py``) and under
pytest.
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


# --- store: delete() is an async hard-delete that RETURNs the row -------------

def test_store_delete_is_async_hard_delete():
    from app.services import yas_local_store as s

    assert hasattr(s, "delete"), "yas_local_store.delete is missing"
    assert inspect.iscoroutinefunction(s.delete), "delete must be async"
    params = list(inspect.signature(s.delete).parameters)
    assert params == ["gen_id"], f"unexpected delete signature: {params}"

    src = inspect.getsource(s.delete)
    # A real row delete, and it RETURNs the row so the caller can clean storage.
    assert "DELETE FROM yas_poster_generations" in src
    assert "RETURNING *" in src


# --- router: DELETE /poster/{gen_id}, poster-gated, storage-then-row ----------

def test_router_registers_poster_delete():
    src = _read("backend/app/routers/yas_producer.py")
    assert '@router.delete("/poster/{gen_id}")' in src, "DELETE route not declared"
    assert "async def poster_delete(" in src


def test_router_delete_is_poster_gated_and_cleans_storage():
    src = _read("backend/app/routers/yas_producer.py")
    # Isolate the handler body so the assertions are about THIS route.
    start = src.index("async def poster_delete(")
    body = src[start : start + 2600]
    # 404 unless it's actually a poster-mode record (don't delete other modes).
    assert 'gen.get("mode") != POSTER_MODE' in body
    # Storage object removed best-effort, then the row.
    assert "asset_storage.delete(" in body
    assert "ystore.delete(gen_id)" in body
    # Storage failure must not block the row delete.
    assert "except Exception" in body


def test_router_delete_cleans_sibling_tables():
    """'Removed from everywhere' — the delete must also drop the dangling
    scheduled Instagram post + the mid-roadmap resume state for this poster."""
    src = _read("backend/app/routers/yas_producer.py")
    start = src.index("async def poster_delete(")
    body = src[start : start + 2600]
    assert 'sched_store.delete_by_ref("poster", gen_id)' in body
    assert 'roadmap_state.delete("poster", gen_id)' in body
    # Surfaced so the UI could report how many scheduled posts were cancelled.
    assert "scheduled_removed" in body


def test_scheduled_delete_by_ref_keeps_published():
    """Cancelling a deleted poster's bookings must NOT erase the audit trail of a
    real published Instagram post."""
    from app.services import scheduled_posts as sp

    assert inspect.iscoroutinefunction(sp.delete_by_ref)
    params = list(inspect.signature(sp.delete_by_ref).parameters)
    assert params == ["tool", "ref_id"], f"unexpected signature: {params}"
    src = inspect.getsource(sp.delete_by_ref)
    assert "DELETE FROM scheduled_instagram_posts" in src
    assert "status <> 'published'" in src  # published rows are preserved


# --- frontend wiring guards ---------------------------------------------------

def test_api_client_has_poster_delete():
    src = _read("frontend/src/lib/api.ts")
    assert "delete: (genId: number) =>" in src
    assert "`/api/yas-producer/poster/${genId}`" in src
    assert 'method: "DELETE"' in src


def test_library_store_has_remove():
    src = _read("frontend/src/lib/store.ts")
    assert "remove: (type: YasLibraryType, id: number) => void;" in src
    assert "remove: (type, id) =>" in src
    # remove() also broadcasts the deletion so a co-mounted creator can react.
    assert "lastRemoved: { type, id }" in src


def test_poster_creator_prunes_deleted_poster():
    """The Create-Poster history strip must drop a poster deleted from the
    Library (otherwise the stale tile lingers and 404s on click)."""
    src = _read("frontend/src/components/yas/PosterCreator.tsx")
    assert "useYasLibraryStore" in src
    assert "s.lastRemoved" in src
    assert 'lastRemoved.type !== "poster"' in src
    assert "setAttempts((prev) => prev.filter((g) => g.id !== delId))" in src


def test_library_modal_wires_poster_only_delete_button():
    src = _read("frontend/src/components/yas/YasLibrary.tsx")
    # Delete is gated to posters only (the user's explicit scope).
    assert 'openItem.type === "poster"' in src
    assert "api.yasProducer.posterOnly.delete(id)" in src
    assert "store.remove(type, id)" in src
    # The button renders only when onDelete is supplied, and confirms first via
    # our in-app dialog (useConfirm) — not the browser's native window.confirm.
    assert "{onDelete && (" in src
    assert "await confirm({" in src
    assert "window.confirm(" not in src
    assert ">Delete<" in src or "Deleting…" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} poster-delete tests passed.")
