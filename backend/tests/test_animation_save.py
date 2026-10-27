"""
Tests for the Create-Animation "Save to Assets" feature.

The animation creator's frame (a branded poster) can be saved to the Assets
library via POST /animation/{id}/save-to-assets. Design invariants this suite
locks down (pure source/string checks — no DB/network):

  1. Saved-ness rides a dedicated `saved_asset_id` column — the endpoint must
     NEVER touch `status`, because status drives the animate flow
     (frame_ready → animating → ready) and a saved frame must stay animatable.
  2. The store knows the new column (DDL + _COLUMNS + idempotent ALTER for
     existing tables), so update()/SELECT * carry it end-to-end.
  3. The frame's image mime is derived from bytes, not the record's mime_type
     (which the poller overwrites to video/mp4 once animated).
  4. The frontend exposes the Save action and rides saved_asset_id.

Runs standalone (``cd backend && python tests/test_animation_save.py``) and
under pytest.
"""
import inspect
import re
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
_REPO_ROOT = _BACKEND_ROOT.parent


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The endpoint never touches `status` (saved frames stay animatable)
# ---------------------------------------------------------------------------

def test_save_endpoint_never_updates_status():
    from app.routers import yas_producer as yp

    src = inspect.getsource(yp.animation_save)
    for call in re.findall(r"anim_store\.update\(([^)]*)\)", src):
        assert "status" not in call, \
            f"animation_save passes status= to anim_store.update: ({call})"
    assert "saved_asset_id" in src


def test_save_endpoint_guards_and_mime():
    from app.routers import yas_producer as yp

    src = inspect.getsource(yp.animation_save)
    # 409 on double-save, keyed on saved_asset_id (not a status flip).
    assert "already_saved" in src
    # Saving allowed from BOTH frame_ready and ready (frame persists after animate).
    assert '"frame_ready", "ready"' in src
    # Image mime derived from the frame bytes — rec["mime_type"] holds the
    # VIDEO mime after animating, so it must not be trusted here.
    assert "_ext_for_image_bytes(image_bytes)" in src
    # Reads the FRAME columns, not the video ones.
    assert "frame_storage_bucket" in src and "frame_storage_path" in src
    assert 'rec.get("video_storage_path")' not in src


def test_saved_asset_appears_as_poster_asset():
    from app.routers import yas_producer as yp

    src = inspect.getsource(yp.animation_save)
    assert '"content_category": "img_poster"' in src
    assert '"source": "yas_producer_animation"' in src


# ---------------------------------------------------------------------------
# 2. The store carries the new column end-to-end
# ---------------------------------------------------------------------------

def test_store_knows_saved_asset_id():
    from app.services import yas_animation_store as st

    assert "saved_asset_id" in st._COLUMNS, "update() would silently drop it"
    assert "saved_asset_id" in st._CREATE_TABLE, "fresh installs miss the column"


def test_store_backfills_existing_tables():
    from app.services import yas_animation_store as st

    src = inspect.getsource(st._ensure_table)
    assert "ADD COLUMN IF NOT EXISTS saved_asset_id" in src, \
        "existing deployments never get the column (no ALTER in _ensure_table)"


# ---------------------------------------------------------------------------
# 3. Frontend wiring — Save button + saved_asset_id ride-through
# ---------------------------------------------------------------------------

def test_frontend_api_and_type_wired():
    api_ts = _read("frontend/src/lib/api.ts")
    assert "animation/${animId}/save-to-assets" in api_ts
    types_ts = _read("frontend/src/lib/types.ts")
    m = re.search(r"interface YasAnimationGen \{(.*?)\}", types_ts, re.S)
    assert m and "saved_asset_id" in m.group(1), \
        "YasAnimationGen is missing saved_asset_id (Saved state won't rehydrate)"


def test_frontend_creator_has_save_action():
    src = _read("frontend/src/components/yas/AnimationCreator.tsx")
    assert "handleSaveCard" in src
    assert "saveToAssets" in src
    # Saved-ness must key off saved_asset_id, never a status flip.
    assert "saved_asset_id != null" in src
    assert '"saved_to_assets"' not in src


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — report every failure, keep going
            failures += 1
            print(f"FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {fn.__name__}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed.")
    sys.exit(1 if failures else 0)
