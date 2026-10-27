"""Music generated in the Video Editor (Suno) is kept as music.

It used to go into yas_uploads only. The editor's Music tab and the Assets page
both list producer_assets (asset_kind=audio), so every generated track played
on the timeline once and could not be found again, and it lived only on the
disk of whichever instance downloaded it. The AI poller now saves the track to
the Assets Library through the same pipeline as "+ Add music" and points the
clip at that asset, falling back to the old upload only when the library save
fails.

Dependency-light: the poller runs against stubbed download / library / apply
calls; the B-roll path, the pipeline and the frontend are source-string guards.

Runs under pytest (``cd backend && python -m pytest tests/test_suno_music_library.py``).
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

_REPO_ROOT = _REPO_BACKEND.parent


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _job() -> dict:
    return {
        "provider_job_id": "sunojob1",
        "plan_id": "music_42",
        "pending_op": {
            "op": "add_music", "from": 0, "duck": True, "speech_segments": [],
            "style_or_query": "classical corporate; mood: calm and professional",
        },
    }


def _stub(monkeypatch, *, library_ok: bool = True, existing: int | None = None):
    from app.routers import producer_assets as pa
    from app.workers import yas_video_edit_ai_poller as poller

    seen: dict = {"process": [], "import": [], "ops": None, "merged": []}

    async def fake_download(pid, urls):
        return b"ID3-not-really-an-mp3"

    async def fake_lookup(job_id):
        return existing

    async def fake_process(**kw):
        seen["process"].append(kw)
        if not library_ok:
            raise RuntimeError("bucket unreachable")
        return {"id": 901}

    async def fake_import(**kw):
        seen["import"].append(kw)
        return {"id": 555}

    async def fake_apply(pid, *, plan_id, ops):
        seen["ops"] = ops
        return {"applied": 1, "failed": [], "rev": 12}

    async def fake_merge(pid, patch):
        seen["merged"].append(patch)

    monkeypatch.setattr(poller, "_download_first", fake_download)
    monkeypatch.setattr(poller, "_library_track_for", fake_lookup)
    monkeypatch.setattr(pa, "_process_asset_bytes", fake_process)
    monkeypatch.setattr(poller.ve, "import_audio", fake_import)
    monkeypatch.setattr(poller, "apply_plan", fake_apply)
    monkeypatch.setattr(poller.ve_ai, "_merge_job", fake_merge)
    return poller, seen


def test_a_finished_suno_track_is_saved_as_library_music(monkeypatch):
    poller, seen = _stub(monkeypatch)

    asyncio.run(poller._finish(42, _job(), ["https://cdn.example/track.mp3"]))

    assert len(seen["process"]) == 1
    call = seen["process"][0]
    assert call["asset_kind"] == "audio"
    assert call["display_name"] == "AI music: classical corporate"
    assert call["extra_metadata"]["suno_job_id"] == "sunojob1"
    assert call["extra_metadata"]["project_id"] == 42
    assert seen["import"] == []  # no stray yas_uploads copy

    (op,) = seen["ops"]
    assert op["op"] == "add_music"
    assert op["source_asset_id"] == 901
    assert "source_upload_id" not in op
    assert seen["merged"][-1]["status"] == "done"


def test_the_track_still_reaches_the_timeline_when_the_library_save_fails(monkeypatch):
    poller, seen = _stub(monkeypatch, library_ok=False)

    asyncio.run(poller._finish(42, _job(), ["https://cdn.example/track.mp3"]))

    (op,) = seen["ops"]
    assert op["source_upload_id"] == 555
    assert "source_asset_id" not in op
    assert len(seen["import"]) == 1
    assert seen["merged"][-1]["status"] == "done"


def test_a_track_already_in_the_library_is_not_saved_twice(monkeypatch):
    poller, seen = _stub(monkeypatch, existing=777)

    asyncio.run(poller._finish(42, _job(), ["https://cdn.example/track.mp3"]))

    assert seen["process"] == [] and seen["import"] == []
    (op,) = seen["ops"]
    assert op["source_asset_id"] == 777


def test_a_job_without_a_music_op_saves_nothing(monkeypatch):
    poller, seen = _stub(monkeypatch)
    job = {**_job(), "pending_op": {"op": "add_clip"}}

    asyncio.run(poller._finish(42, job, ["https://cdn.example/track.mp3"]))

    assert seen["process"] == [] and seen["import"] == [] and seen["ops"] is None
    assert seen["merged"][-1]["status"] == "failed"


def test_broll_music_goes_through_the_same_library_save():
    from app.workers import yas_video_edit_ai_poller as poller

    src = inspect.getsource(poller._check_broll)
    assert "_suno_track_source(" in src
    assert "ve.import_audio(" not in src


def test_the_asset_pipeline_records_extra_metadata():
    from app.routers import producer_assets as pa

    assert "**(extra_metadata or {})" in inspect.getsource(pa._process_asset_bytes)


def test_music_tab_looks_again_when_an_ai_job_finishes():
    panel = _read("frontend/src/components/yas/video-editor/sidebar/AudioPanel.tsx")
    assert "s.aiJob?.finished_at" in panel
    assert "}, [aiFinishedAt]);" in panel
