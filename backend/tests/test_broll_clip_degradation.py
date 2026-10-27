"""B-roll survives a broken CLIP stack (issue #32).

A real B-roll run failed with ``NameError: name 'nn' is not defined`` — raised
from inside the unpinned sentence-transformers/torch stack, not from our code —
and took the WHOLE job down: ``ops_applied: 0``, ``rev_after: null``, dead at
the "sequencing the B-roll for flow…" step.

Both the module docstring and ``extract_edge_embeddings``' own docstring promise
that this step is best-effort and falls back to the clips' current order. Nothing
enforced it: the try block carried only a ``finally``. These tests pin the
contract down.

Pure functions — no DB / network. Runs standalone or under pytest.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import clip_service  # noqa: E402
from app.services import yas_video_edit_ai_broll as broll  # noqa: E402
from app.services import yas_video_editor as ve  # noqa: E402


def _clips() -> list[dict]:
    return [
        {"id": "c1", "trackId": "trk_v0", "kind": "video", "from": 0,
         "durationInFrames": 90, "trimBefore": 0, "trimAfter": 90},
        {"id": "c2", "trackId": "trk_v0", "kind": "video", "from": 90,
         "durationInFrames": 90, "trimBefore": 0, "trimAfter": 90},
        {"id": "c3", "trackId": "trk_v0", "kind": "video", "from": 180,
         "durationInFrames": 90, "trimBefore": 0, "trimAfter": 90},
    ]


class _Patch:
    """Minimal setattr/restore so this file runs without pytest fixtures."""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def set(self, obj: object, name: str, value: object) -> None:
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def _stub_frame_extraction(p: _Patch, tmp: Path) -> None:
    """Make extract_edge_embeddings reach the CLIP call with real JPG paths."""
    p.set(ve, "VE_ROOT", tmp)
    p.set(ve, "_clip_source_kind_id", lambda c: ("upload", 1))

    async def _resolve(kind, sid):          # noqa: ANN001 - test stub
        src = tmp / "src.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"not-a-real-video")
        return src

    p.set(ve, "resolve_source_local", _resolve)

    def _save_thumb(src, dest, t):          # noqa: ANN001 - test stub
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpg")
        return True

    p.set(ve, "_save_thumb", _save_thumb)


# --------------------------------------------------------------------------- #
def test_embeddings_swallow_a_broken_clip_stack(tmp_path: Path) -> None:
    """The exact #32 failure must degrade to {}, not raise."""
    p = _Patch()
    try:
        _stub_frame_extraction(p, tmp_path)
        p.set(clip_service, "enabled", lambda: True)

        def _boom(paths, **kw):             # noqa: ANN001 - test stub
            raise NameError("name 'nn' is not defined")

        p.set(clip_service, "encode_image_paths", _boom)

        emb = asyncio.run(broll.extract_edge_embeddings(7, _clips()))
        assert emb == {}, "a library-level failure must degrade, not propagate"
    finally:
        p.restore()


def test_embeddings_swallow_a_broken_source(tmp_path: Path) -> None:
    """Non-CLIP failures inside the same block degrade the same way."""
    p = _Patch()
    try:
        _stub_frame_extraction(p, tmp_path)
        p.set(clip_service, "enabled", lambda: True)

        def _explode(src, dest, t):         # noqa: ANN001 - test stub
            raise OSError("ffmpeg: Invalid data found when processing input")

        p.set(ve, "_save_thumb", _explode)

        assert asyncio.run(broll.extract_edge_embeddings(7, _clips())) == {}
    finally:
        p.restore()


def test_temp_frames_are_cleaned_up_on_failure(tmp_path: Path) -> None:
    """The `finally` cleanup must still run once an `except` is in front of it."""
    p = _Patch()
    try:
        _stub_frame_extraction(p, tmp_path)
        p.set(clip_service, "enabled", lambda: True)
        p.set(clip_service, "encode_image_paths",
              lambda paths, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

        asyncio.run(broll.extract_edge_embeddings(7, _clips()))
        assert not (tmp_path / "ai" / "broll" / "p7").exists()
    finally:
        p.restore()


# --------------------------------------------------------------------------- #
def test_best_order_keeps_current_order_without_embeddings() -> None:
    """The fallback the degradation relies on: no vectors ⇒ untouched order."""
    clips = _clips()
    assert broll.best_order(clips, {}) == ["c1", "c2", "c3"]


def test_pin_and_order_survives_without_embeddings() -> None:
    """The uploaded-bookend path degrades too (it delegates to best_order)."""
    ordered = broll.pin_and_order(_clips(), {}, None, None)
    assert [c["id"] for c in ordered] == ["c1", "c2", "c3"]


# --------------------------------------------------------------------------- #
def test_clip_service_never_raises_and_latches_off(tmp_path: Path) -> None:
    """encode_image_paths absorbs the NameError and stops retrying the load."""
    p = _Patch()
    calls: list[int] = []
    try:
        p.set(clip_service, "_broken", False)
        p.set(clip_service, "_model", None)
        p.set(clip_service, "enabled", lambda: True)

        def _load_boom():
            calls.append(1)
            raise NameError("name 'nn' is not defined")

        p.set(clip_service, "_load", _load_boom)

        jpg = tmp_path / "a.jpg"
        _write_probe_jpeg(jpg)

        first = clip_service.encode_image_paths([jpg])
        assert first == [None], "a broken stack yields no vectors, not an exception"
        assert clip_service._broken is True

        second = clip_service.encode_image_paths([jpg])
        assert second == [None]
        assert len(calls) == 1, "the expensive model load must not be retried"
    finally:
        p.restore()


def _write_probe_jpeg(dest: Path) -> None:
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(dest, "JPEG")


if __name__ == "__main__":  # allow `python tests/test_broll_clip_degradation.py`
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
