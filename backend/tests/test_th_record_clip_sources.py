"""scripts/th_record_clip_sources.py: record the still each up-to-date TH v2
clip was made from, for clips made before clips recorded it (run 830's 351
and 354). A dry run writes nothing; --apply records only clips that are up to
date and record nothing, decided again on the locked row; a second run
changes nothing. Offline: a fake database that serves the runs and applies
the two UPDATEs the script sends.
"""
from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import pytest

from app import db as app_db
from app.services import yas_talking_head_v2 as th
from scripts import th_record_clip_sources as rc


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class FakeDb:
    def __init__(self, rows: list[dict]):
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        self.writes: list[tuple[str, tuple]] = []
        self.autocommit_writes = 0
        self.commits = 0

    def cursor(self, autocommit: bool):
        db = self

        class _Cur:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, sql, params=()):
                s = _norm(sql)
                self._out = None
                if s == _norm(rc.RUNS_SQL):
                    assert params == (th.MODE_PREVIEW,)
                    self._out = [copy.deepcopy(r) for _, r in sorted(db.rows.items())
                                 if r.get("mode", th.MODE_PREVIEW) == th.MODE_PREVIEW]
                elif s == _norm(rc.LOCK_SQL):
                    assert not autocommit, "the lock is taken in a transaction"
                    rid, mode = params
                    row = db.rows.get(rid)
                    self._out = copy.deepcopy(row) if row and row.get("mode", mode) == mode else None
                elif s == _norm(rc.MAIN_SQL):
                    assert not autocommit
                    patch, rid = params
                    db.rows[rid]["metadata"].update(copy.deepcopy(patch.obj))
                    db.writes.append((s, (patch.obj, rid)))
                elif s == _norm(rc.SCENE_SQL):
                    assert not autocommit
                    path, src, rid = params
                    assert path[0] == "scenes" and path[2] == "video_source"
                    db.rows[rid]["metadata"]["scenes"][int(path[1])]["video_source"] = src
                    db.writes.append((s, (tuple(path), src, rid)))
                else:
                    raise AssertionError(f"unexpected SQL: {s[:120]}")

            async def fetchone(self):
                return self._out

            async def fetchall(self):
                return self._out or []

        return _Cur()

    def conn(self, autocommit: bool = False):
        db = self

        @asynccontextmanager
        async def _cm():
            class _Conn:
                def cursor(self):
                    return db.cursor(autocommit)

            yield _Conn()

        return _cm()

    def pool(self):
        """A stand-in for app.db's pool: the real get_conn runs on it."""
        db = self

        class _Pooled:
            autocommit = False

            async def set_autocommit(self, on: bool) -> None:
                self.autocommit = on

            def cursor(self):
                return db.cursor(self.autocommit)

            async def commit(self) -> None:
                db.commits += 1

            async def rollback(self) -> None:
                raise AssertionError("nothing is rolled back")

        class _Pool:
            @asynccontextmanager
            async def connection(self):
                yield _Pooled()

        return _Pool()


def _scene(i: int, **kw) -> dict:
    return {"index": i, "keyframe_id": 500 + i if i else None, "animation_id": None,
            "video_stale": False, **kw}


def _runs() -> list[dict]:
    return [
        # 830-like: two scenes, both clips up to date, neither recorded.
        {"id": 830, "local_path": "th/830/main.png", "storage_path": None, "metadata": {
            "scene_count": 2, "scenes": [_scene(0, animation_id=351), _scene(1, animation_id=354)]}},
        # One scene, an up-to-date legacy main clip (veo_animation_id only).
        {"id": 700, "local_path": None, "storage_path": "yas/talking-head-v2/700/main.png",
         "metadata": {"veo_animation_id": 12, "main_video_stale": False}},
        # A stale clip, a recorded clip, no clip, no still: left alone.
        {"id": 701, "local_path": "th/701/main.png", "storage_path": None,
         "metadata": {"main_animation_id": 13, "main_video_stale": True}},
        {"id": 702, "local_path": "th/702/main.png", "storage_path": None,
         "metadata": {"main_animation_id": 14, "main_video_source": "main:th/702/main-old.png|"}},
        {"id": 703, "local_path": "th/703/main.png", "storage_path": None, "metadata": {}},
        {"id": 704, "local_path": None, "storage_path": None, "metadata": {"main_animation_id": 15}},
        # Multi-scene: one scene stale, one recorded, one to record.
        {"id": 705, "local_path": "th/705/main.png", "storage_path": None, "metadata": {
            "scene_count": 3, "scenes": [
                _scene(0, animation_id=21, video_stale=True),
                _scene(1, animation_id=22, video_source="scene:9"),
                _scene(2, animation_id=23)]}},
        # Another mode is never read.
        {"id": 706, "mode": "talking_head_v2_scene", "local_path": "x.png", "storage_path": None,
         "metadata": {"main_animation_id": 16}},
    ]


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb(_runs())
    monkeypatch.setattr(rc, "_conn", fake.conn)
    monkeypatch.setattr(app_db, "open_pool", _no_pool)
    monkeypatch.setattr(app_db, "close_pool", _no_pool)
    return fake


async def _no_pool() -> None:
    return None


def test_the_dry_run_reports_and_writes_nothing(db):
    before = copy.deepcopy(db.rows)
    out = asyncio.run(rc.run(False))
    assert db.writes == [] and db.rows == before
    assert out["dry_run"] is True and out["runs"] == 7 and out["written"] == 0
    got = {e["run_id"]: e["clips"] for e in out["by_run"]}
    assert got == {
        830: [{"clip": "scene_0", "video_source": "main:th/830/main.png|"},
              {"clip": "scene_1", "video_source": "scene:501"}],
        700: [{"clip": "main", "video_source": "main:|yas/talking-head-v2/700/main.png"}],
        705: [{"clip": "scene_2", "video_source": "scene:502"}],
    }
    assert out["clips"] == 4


def test_apply_records_only_up_to_date_clips_and_is_idempotent(db):
    out = asyncio.run(rc.run(True))
    assert out["written"] == 4 and len(db.writes) == 4
    md = {rid: r["metadata"] for rid, r in db.rows.items()}
    assert [s.get("video_source") for s in md[830]["scenes"]] == ["main:th/830/main.png|", "scene:501"]
    assert md[700]["main_video_source"] == "main:|yas/talking-head-v2/700/main.png"
    assert "main_video_source" not in md[701] and md[702]["main_video_source"].endswith("main-old.png|")
    assert "main_video_source" not in md[703] and "main_video_source" not in md[704]
    assert [s.get("video_source") for s in md[705]["scenes"]] == [None, "scene:9", "scene:502"]
    assert "main_video_source" not in md[706]
    # Each clip now reads up to date for its current still, so an Undo back
    # to it keeps it so.
    for rid, i in ((830, 0), (830, 1), (700, 0), (705, 2)):
        row = db.rows[rid]
        assert th._stale_for(md[rid], i, th._still_key(row, md[rid], i)) is False
    # Again: nothing left to do.
    again = asyncio.run(rc.run(True))
    assert again["clips"] == 0 and again["written"] == 0 and len(db.writes) == 4


def test_apply_decides_again_on_the_locked_row(db, monkeypatch):
    # Between the read and the lock the clip went out of date: nothing is
    # written for it.
    real = rc._apply_one

    async def stale_meanwhile(run_id):
        if run_id == 700:
            db.rows[700]["metadata"]["main_video_stale"] = True
        return await real(run_id)

    monkeypatch.setattr(rc, "_apply_one", stale_meanwhile)
    out = asyncio.run(rc.run(True, [700, 830]))
    assert out["runs"] == 2 and "main_video_source" not in db.rows[700]["metadata"]
    by = {e["run_id"]: e for e in out["by_run"]}
    assert by[700]["written"] == [] and len(by[830]["written"]) == 2


def test_only_and_limit(db):
    out = asyncio.run(rc.run(False, [705]))
    assert out["runs"] == 1 and [e["run_id"] for e in out["by_run"]] == [705]
    out = asyncio.run(rc.run(False, None, 1))
    assert [e["run_id"] for e in out["by_run"]] == [700]


def test_the_cli_is_a_dry_run_unless_apply(db, capsys):
    assert rc.main([]) == 0
    printed = capsys.readouterr().out
    assert "Dry run: nothing was written" in printed and db.writes == []
    assert json.loads(printed.split("\nDry run")[0])["clips"] == 4
    assert rc.main(["--apply", "--run", "700"]) == 0
    assert db.rows[700]["metadata"]["main_video_source"] and len(db.writes) == 1


def test_the_cli_opens_the_database_pool_and_closes_it(monkeypatch, capsys):
    # Run as a script there is no app startup to open the pool: get_conn
    # raised "DB pool not initialised" before any query, dry run or --apply.
    # Here the real _conn and get_conn run, on a stand-in pool.
    fake = FakeDb(_runs())
    calls: list[str] = []

    async def open_pool():
        calls.append("open")
        app_db._pool = fake.pool()

    async def close_pool():
        calls.append("close")
        app_db._pool = None

    monkeypatch.setattr(app_db, "_pool", None)
    monkeypatch.setattr(app_db, "open_pool", open_pool)
    monkeypatch.setattr(app_db, "close_pool", close_pool)
    assert rc.main([]) == 0
    assert calls == ["open", "close"] and fake.writes == [] and app_db._pool is None
    assert json.loads(capsys.readouterr().out.split("\nDry run")[0])["clips"] == 4
    assert rc.main(["--apply", "--run", "700"]) == 0
    assert calls == ["open", "close"] * 2
    assert fake.rows[700]["metadata"]["main_video_source"] and len(fake.writes) == 1
    assert fake.commits == 1, "the run's write is committed"
