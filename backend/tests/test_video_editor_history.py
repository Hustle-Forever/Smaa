"""Video Editor undo history survives a reload.

Before this, Undo lived only in the open tab. The server's `history.checkpoints`
held a rev number and a time and nothing that could be restored, so reopening a
project started Undo from nothing and every earlier state was gone. Each save
now stores the document itself in `yas_video_edit_revisions`, and the editor
loads those versions back under Undo when a project opens.

Dependency-light: the pure helpers run directly, `save_timeline` and
`_record_revision` run against stubbed DB access, and the route and frontend
checks are source-string guards in the same style as test_library_delete_all.

Runs under pytest (``cd backend && python -m pytest tests/test_video_editor_history.py``).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

_REPO_ROOT = _REPO_BACKEND.parent


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


# --- pure helpers ------------------------------------------------------------

def test_revision_doc_drops_derived_source_urls_without_mutating():
    from app.services import yas_video_editor as ve

    clip = {"id": "c1", "from": 0, "sourceUrl": "/api/x",
            "source": {"kind": "upload", "upload_id": 3}}
    doc = ve._revision_doc({"title": "T"}, [{"id": "trk_v0"}], [clip])

    assert doc["clips"] == [{"id": "c1", "from": 0, "source": {"kind": "upload", "upload_id": 3}}]
    assert clip["sourceUrl"] == "/api/x"  # the caller's clip is untouched
    assert doc["project"] == {"title": "T"}
    assert doc["tracks"] == [{"id": "trk_v0"}]
    assert ve._revision_doc(None, None, None) == {"project": {}, "tracks": [], "clips": []}


def test_doc_hash_ignores_key_order_and_source_urls_but_not_edits():
    from app.services import yas_video_editor as ve

    a = ve._revision_doc({"title": "T", "aspect": "16:9"}, [], [{"id": "c1", "from": 0, "sourceUrl": "/a"}])
    b = ve._revision_doc({"aspect": "16:9", "title": "T"}, [], [{"from": 0, "id": "c1", "sourceUrl": "/b"}])
    moved = ve._revision_doc({"title": "T", "aspect": "16:9"}, [], [{"id": "c1", "from": 30}])

    assert ve._doc_hash(a) == ve._doc_hash(b)
    assert ve._doc_hash(a) != ve._doc_hash(moved)


# --- save_timeline hands every save to the version store ----------------------

def test_save_timeline_records_the_new_version_and_the_one_before(monkeypatch):
    from app.services import yas_video_editor as ve

    stored = {
        "project": {"title": "Old", "aspect": "16:9"},
        "tracks": [{"id": "trk_v0", "kind": "video"}],
        "clips": [{"id": "c1", "from": 0, "durationInFrames": 30, "sourceUrl": "/api/old"}],
        "history": {"rev": 7, "checkpoints": []},
        "render": {"composition_status": "ready"},
    }
    row = {"id": 42, "topic": "Old", "status": "ready", "created_at": None,
           "updated_at": None, "metadata": {"video_edit": stored}}
    calls: list[tuple[int, dict]] = []

    async def fake_fetch_row(pid):
        return row

    async def fake_write(pid, timeline, *, topic=None):
        return None

    async def fake_diff(old, new, *, rev, actor, change_summary):
        return [{"rev": rev, "summary": "c1: from 0->15"}], []

    async def fake_append(pid, **kw):
        return None

    async def fake_resolve(timeline):
        return timeline

    async def fake_record(pid, **kw):
        calls.append((pid, kw))

    monkeypatch.setattr(ve, "_fetch_row", fake_fetch_row)
    monkeypatch.setattr(ve, "_write_video_edit", fake_write)
    monkeypatch.setattr(ve, "_diff_edit_log", fake_diff)
    monkeypatch.setattr(ve, "append_memory", fake_append)
    monkeypatch.setattr(ve, "_resolve_clip_sources", fake_resolve)
    monkeypatch.setattr(ve, "_record_revision", fake_record)

    asyncio.run(ve.save_timeline(
        42,
        project={"title": "Old", "aspect": "16:9"},
        tracks=stored["tracks"],
        clips=[{"id": "c1", "from": 15, "durationInFrames": 30, "sourceUrl": "/api/old"}],
    ))

    assert len(calls) == 1
    pid, kw = calls[0]
    assert pid == 42
    assert kw["rev"] == 8 and kw["previous_rev"] == 7
    assert kw["doc"]["clips"] == [{"id": "c1", "from": 15, "durationInFrames": 30}]
    assert kw["previous_doc"]["clips"] == [{"id": "c1", "from": 0, "durationInFrames": 30}]
    assert kw["summary"] == "c1: from 0->15"  # the edit log's own words
    assert kw["actor"] == "manual"


# --- _record_revision against a fake connection --------------------------------

class _FakeCursor:
    def __init__(self, latest):
        self.latest = latest
        self.sql: list[tuple[str, tuple | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self.latest


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return self.cur


def _record(monkeypatch, latest, doc, previous_doc):
    from app.services import yas_video_editor as ve

    cur = _FakeCursor(latest)

    async def ready():
        return True

    monkeypatch.setattr(ve, "_ensure_revisions_table", ready)
    monkeypatch.setattr(ve, "get_conn", lambda: _FakeConn(cur))
    asyncio.run(ve._record_revision(
        42, rev=8, doc=doc, actor="manual", summary="moved c1",
        previous_rev=7, previous_doc=previous_doc,
    ))
    return cur.sql


_OLD = {"project": {"title": "T"}, "tracks": [], "clips": [{"id": "c1", "from": 0}]}
_NEW = {"project": {"title": "T"}, "tracks": [], "clips": [{"id": "c1", "from": 15}]}


def test_first_recorded_save_keeps_the_state_the_project_was_opened_in(monkeypatch):
    sql = _record(monkeypatch, None, _NEW, _OLD)

    inserts = [(s, p) for s, p in sql if s.startswith("INSERT INTO yas_video_edit_revisions")]
    assert [p[1] for _, p in inserts] == [7, 8]  # the baseline first, then this save
    assert inserts[0][1][2] == "baseline"
    assert inserts[1][1][2] == "manual"
    assert any(s.startswith("DELETE FROM yas_video_edit_revisions") for s, _ in sql)


def test_a_save_that_changed_nothing_is_not_stored(monkeypatch):
    from app.services import yas_video_editor as ve

    sql = _record(monkeypatch, {"doc_hash": ve._doc_hash(_NEW)}, _NEW, _OLD)

    assert len(sql) == 1 and sql[0][0].startswith("SELECT doc_hash")


def test_a_later_save_is_stored_and_the_project_trimmed_to_the_cap(monkeypatch):
    from app.services import yas_video_editor as ve

    sql = _record(monkeypatch, {"doc_hash": ve._doc_hash(_OLD)}, _NEW, _OLD)

    inserts = [p for s, p in sql if s.startswith("INSERT INTO yas_video_edit_revisions")]
    assert [p[1] for p in inserts] == [8]  # no second baseline once history exists
    prune = [p for s, p in sql if s.startswith("DELETE FROM yas_video_edit_revisions")]
    assert prune == [(42, 42, ve._MAX_REVISIONS - 1)]


# --- route + frontend wiring ----------------------------------------------------

def test_history_route_registered():
    from app.routers.yas_producer import router

    methods: set[str] = set()
    for r in router.routes:
        if r.path == "/api/yas-producer/video-editor/{project_id}/history":
            methods |= set(r.methods or set())
    assert "GET" in methods


def test_editor_loads_saved_versions_under_undo():
    api = _read("frontend/src/lib/api.ts")
    store = _read("frontend/src/lib/store.ts")

    assert "/api/yas-producer/video-editor/${projectId}/history" in api
    assert "void seedHistory(p.id, baseline" in store


def test_deleting_a_project_deletes_its_saved_versions():
    import inspect
    from app.services import yas_video_editor as ve

    assert "DELETE FROM yas_video_edit_revisions WHERE project_id = %s" in inspect.getsource(ve.delete_project)
