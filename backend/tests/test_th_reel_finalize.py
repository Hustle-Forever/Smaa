"""Rebrand stage, S1: a reel that can be finalized again (services/th_reel,
the composition poller's owned path, the reel's file route).

  finalize      one compare-and-set: rendering {render_job_id "", owner,
                seq+1, the frozen plan, its inputs' sha}; composition_path
                untouched; a double press makes one render; a reel whose
                inputs are its last good render's is not made again; a render
                whose owner is gone 20 min is taken over
  the poller    only the owner submits (final-<seq>.mp4) and watches it;
                done: the new file is served, two kept; failed or lost
                (4 x 404): a whole file is taken, else the last good reel
                stays ("ready" with the error) or, with none, "failed"
  old code      the production VPS polls the same rows with the code at
                9f788d3: its phase 2 never picks our row and its
                _check_render makes no call for it
  serving       "private, no-cache", the last good file whatever the status,
                the bucket only when it holds the good seq; ?v=<good seq>;
                saved_current

No database (every get_conn is a tripwire; the reel's store is in memory, or
a fake cursor that runs the very SQL strings), no Remotion (a mock
transport), no bucket, no paid call.
"""
from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as app_db
from app.services import asset_storage
from app.services import th_reel
from app.services import yas_talking_head_v2 as svc
from app.workers import yas_th_v2_composition_poller as poller

BACKEND = Path(__file__).resolve().parents[1]
RUN = 9301
OLD_COMMIT = "9f788d3"
_REAL_CLIENT = httpx.AsyncClient


# ---------------------------------------------------------------------------
# The world: a data root in tmp, clips on it, the reel's store in memory
# ---------------------------------------------------------------------------

class World:
    def __init__(self, root: Path):
        self.root = root
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self.jobs: dict[str, Any] = {}
        self.next_job = 0
        self.backups: list[str] = []
        self.backup_answer: tuple = (None, None)
        self.frames: dict[str, int] = {}
        self.store = th_reel.MemoryStore()

    def clip(self, anim_id: int, *, sec: int = 8) -> dict:
        p = self.root / "yas_producer" / "animations" / f"anim-{anim_id}.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"clip")
        row = {"id": anim_id, "status": "ready", "local_path": str(p), "duration_sec": sec,
               "storage_bucket": None, "storage_path": None, "error": None}
        self.store.anim_rows[anim_id] = row
        return row

    def comp(self, name: str) -> Path:
        return self.root / "yas_producer" / "talking-head-v2" / str(RUN) / name

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            self.posts.append(body)
            self.next_job += 1
            job = f"job-{self.next_job}"
            self.jobs[job] = {"status": "queued"}
            return httpx.Response(200, json={"job_id": job})
        self.gets.append(str(request.url))
        job = str(request.url).rsplit("/", 1)[-1]
        if job not in self.jobs or self.jobs[job] == 404:
            return httpx.Response(404, json={"error": "job not found"})
        return httpx.Response(200, json=self.jobs[job])

    def md(self) -> dict:
        return self.store.rows[RUN]["metadata"]


def _single_md(**kw) -> dict:
    md = {"aspect_ratio": "9:16", "scene_count": 1, "composition_status": "videos_ready",
          "main_animation_id": 351, "veo_animation_id": 351, "intro_animation_id": 352,
          "outro_animation_id": 353, "intro_excluded": False, "outro_excluded": False,
          "refs": {"intro_video_upload_id": None, "outro_video_upload_id": None,
                   "intro_asset_id": None, "outro_asset_id": None}}
    md.update(kw)
    return md


class _Tripped(AssertionError):
    pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "data"
    w = World(root)
    for aid, sec in ((351, 8), (352, 4), (353, 4)):
        w.clip(aid, sec=sec)
    w.store.rows[RUN] = {"id": RUN, "topic": "t", "saved_asset_id": None, "metadata": _single_md()}
    th_reel.set_store(w.store)
    monkeypatch.setattr(poller, "config", types.SimpleNamespace(asset_root=root / "assets", producer_assets_root=root / "assets" / "assets_library"))
    monkeypatch.setattr(poller, "COMP_DIR", root / "yas_producer" / "talking-head-v2")

    def client(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_CLIENT(*args, transport=httpx.MockTransport(w.handler), **kwargs)

    monkeypatch.setattr(poller, "httpx", types.SimpleNamespace(AsyncClient=client))

    def trip(*_a, **_k):
        raise _Tripped("the database was reached")

    for mod in (poller, th_reel, svc, app_db):
        monkeypatch.setattr(mod, "get_conn", trip)

    async def no_lookup(*_a, **_k):
        raise _Tripped("a card/upload/asset was looked up in the database")

    for name in ("_fetch_card_path", "_fetch_upload_path", "_fetch_asset_path"):
        monkeypatch.setattr(poller, name, no_lookup)

    async def backup(*, abs_path, bucket_key, mime="video/mp4"):
        w.backups.append(bucket_key)
        return w.backup_answer

    monkeypatch.setattr(asset_storage, "persist_local_file", backup)
    monkeypatch.setattr(poller, "_counted_frames", lambda p: w.frames.get(Path(p).name))
    # Every clip decodes as a clean 8 s Veo clip (no tail flash) unless a
    # test says otherwise.
    w.facts = {}
    monkeypatch.setattr(th_reel, "clip_facts", lambda p: w.facts.get(Path(p).name, th_reel.Facts(192, 24.0, None)))
    monkeypatch.setattr(poller, "_house_busy", lambda: False)
    cancelled: list[int] = []
    monkeypatch.setattr(th_reel, "_cancel_run_drafts", lambda run_id: cancelled.append(run_id) or 0)
    w.cancelled = cancelled
    w.monkeypatch = monkeypatch
    poller._GATE_WAITS.clear()
    return w


def run(coro):
    return asyncio.run(coro)


async def _tick(w: World) -> None:
    """One poller tick over the in-memory rows: phase 2b and phase 3, fed
    by the same rules as its SQL (checked against the SQL below)."""
    for row in [copy.deepcopy(r) for r in w.store.rows.values()]:
        md = row["metadata"]
        if (md.get("composition_status") == "rendering" and md.get("render_owner") == th_reel.INSTANCE_ID
                and not md.get("render_job_id")):
            await poller._kick_plan(row)
    for row in [copy.deepcopy(r) for r in w.store.rows.values()]:
        md = row["metadata"]
        if (md.get("composition_status") == "rendering" and md.get("render_job_id")
                and (not md.get("render_owner") or md.get("render_owner") == th_reel.INSTANCE_ID)):
            await poller._check_render(row)


def _finish(w: World, *, frames: int = 456) -> Path:
    """Remotion finishes the newest job: writes its file."""
    body = w.posts[-1]
    out = Path(body["output_path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"reel")
    w.frames[out.name] = frames
    w.jobs[f"job-{w.next_job}"] = {"status": "done", "cost_usd": 0}
    return out


# ---------------------------------------------------------------------------
# §5.3, transition by transition
# ---------------------------------------------------------------------------

def test_finalize_sets_one_owned_render_and_leaves_the_path(world):
    world.md()["composition_path"] = "yas_producer/talking-head-v2/9301/final.mp4"
    got = run(th_reel.finalize(RUN))
    md = world.md()
    assert got["status"] == "rendering" and got["rendering"] is True
    assert md["composition_status"] == "rendering" and md["render_job_id"] == ""
    assert md["render_owner"] == th_reel.INSTANCE_ID and md["render_404s"] == 0
    assert md["composition_seq"] == 1 and md["composition_plan"]["seq"] == 1
    assert md["composition_inputs_sha"] == got["sha"] and len(got["sha"]) == 16
    assert md["composition_path"] == "yas_producer/talking-head-v2/9301/final.mp4"   # the last good one
    assert _utc(md["render_started_at"]) > datetime.now(timezone.utc) - timedelta(minutes=1)
    assert world.cancelled == [RUN]
    plan = md["composition_plan"]
    assert plan["payload"]["template"] == "talking-head-v2-with-cards" and "output_path" not in plan["payload"]
    assert [s["target"] for s in plan["segments"]] == ["intro", "scene_0", "outro"]
    assert plan["total_frames"] == 456 == 120 + 240 + 120 - 24


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_the_owner_submits_the_frozen_payload_then_serves_the_new_file(world):
    run(th_reel.finalize(RUN))
    frozen = copy.deepcopy(world.md()["composition_plan"]["payload"])
    run(_tick(world))
    assert len(world.posts) == 1
    body = world.posts[0]
    assert list(body) == ["template", "props", "output_path"]
    assert {k: body[k] for k in ("template", "props")} == frozen
    assert Path(body["output_path"]) == world.comp("final-1.mp4")
    md = world.md()
    assert md["render_job_id"] == "job-1" and md["composition_output_path"].replace("\\", "/").endswith(
        "talking-head-v2/9301/final-1.mp4")
    run(_tick(world))                                    # still rendering: nothing changes
    assert world.md()["composition_status"] == "rendering"
    _finish(world)
    run(_tick(world))
    md = world.md()
    assert md["composition_status"] == "ready" and md["composition_good_seq"] == 1
    assert md["composition_good_sha"] == md["composition_inputs_sha"]
    assert th_reel.good_path(md) == world.comp("final-1.mp4")
    assert world.backups == [f"yas/compositions/talking-head/{RUN}.mp4"]          # the same key as today
    assert "composition_storage_seq" not in md                                    # the backup failed (402)
    assert len(world.posts) == 1


def test_a_backup_that_lands_records_its_seq(world):
    world.backup_answer = ("producer-assets", f"yas/compositions/talking-head/{RUN}.mp4")
    run(th_reel.finalize(RUN))
    run(_tick(world))
    _finish(world)
    run(_tick(world))
    md = world.md()
    assert md["composition_storage_seq"] == 1 == md["composition_good_seq"]
    assert th_reel.bucket_current(md)


def test_a_backup_that_hangs_is_given_up(world, monkeypatch):
    monkeypatch.setattr(poller, "BACKUP_TIMEOUT_SEC", 0.05)

    async def hang(**_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(asset_storage, "persist_local_file", hang)
    run(th_reel.finalize(RUN))
    run(_tick(world))
    _finish(world)
    run(_tick(world))
    assert world.md()["composition_status"] == "ready" and "composition_storage_seq" not in world.md()


def _ready_once(w: World) -> None:
    run(th_reel.finalize(RUN))
    run(_tick(w))
    _finish(w)
    run(_tick(w))
    assert w.md()["composition_status"] == "ready"


def test_the_same_inputs_again_make_no_render(world):
    _ready_once(world)
    before = copy.deepcopy(world.md())
    got = run(th_reel.finalize(RUN))
    assert got == {"status": "ready", "rendering": False, "sha": before["composition_good_sha"]}
    assert world.md() == before and len(world.posts) == 1


def test_a_changed_reel_renders_again_and_a_failure_keeps_the_old_one(world):
    _ready_once(world)
    world.md()["outro_excluded"] = True                    # the inputs change
    got = run(th_reel.finalize(RUN))
    md = world.md()
    assert got["status"] == "rendering" and md["composition_seq"] == 2
    assert th_reel.good_path(md) == world.comp("final-1.mp4")      # still served
    run(_tick(world))
    assert Path(world.posts[-1]["output_path"]) == world.comp("final-2.mp4")
    world.jobs["job-2"] = {"status": "failed", "error": "renderer crashed"}
    run(_tick(world))
    md = world.md()
    assert md["composition_status"] == "ready" and md["composition_error"] == "renderer crashed"
    assert md["render_failed_seq"] == 2 and md["composition_good_seq"] == 1
    assert th_reel.good_path(md) == world.comp("final-1.mp4")
    # Finalize again: the error is cleared by the compare-and-set itself.
    run(th_reel.finalize(RUN))
    md = world.md()
    assert md["composition_seq"] == 3 and "composition_error" not in md and "render_failed_seq" not in md


def test_a_first_render_that_fails_fails_the_run(world):
    run(th_reel.finalize(RUN))
    run(_tick(world))
    world.jobs["job-1"] = {"status": "failed", "error": "boom"}
    run(_tick(world))
    md = world.md()
    assert md["composition_status"] == "failed" and md["composition_error"] == "boom"
    # … and it can be finalized again from failed.
    run(th_reel.finalize(RUN))
    assert world.md()["composition_status"] == "rendering" and world.md()["composition_seq"] == 2


def test_a_kick_refused_by_remotion_keeps_the_old_reel(world):
    _ready_once(world)
    world.md()["outro_excluded"] = True
    run(th_reel.finalize(RUN))

    def refuse(request):
        return httpx.Response(500, text="busy")

    world.handler = refuse
    run(_tick(world))
    md = world.md()
    assert md["composition_status"] == "ready" and md["composition_error"].startswith("remotion_kick_failed: 500")
    assert md["render_failed_seq"] == 2


def test_a_failed_renders_frames_are_counted_off_the_event_loop(world, monkeypatch):
    """Review finding 7: a render said failed is taken when its file is
    whole, and counting its frames is a whole decode: it ran on the
    poller's loop. It runs in a thread now."""
    import threading

    threads: list[int] = []

    def counted(p):
        threads.append(threading.get_ident())
        return world.frames.get(Path(p).name)

    monkeypatch.setattr(poller, "_counted_frames", counted)
    run(th_reel.finalize(RUN))
    run(_tick(world))
    out = Path(world.posts[-1]["output_path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"reel")
    world.frames[out.name] = world.md()["composition_plan"]["total_frames"]
    world.jobs["job-1"] = {"status": "failed", "error": "the job was lost"}

    async def tick():
        loop = threading.get_ident()
        await _tick(world)
        return loop

    loop = run(tick())
    assert world.md()["composition_status"] == "ready" and world.md()["composition_good_seq"] == 1
    assert threads and loop not in threads


def test_an_uploaded_bookend_is_probed_once_and_off_the_event_loop(world, monkeypatch):
    """Review finding 7: a single-scene reel's uploaded bookend was probed
    (ffmpeg) on the loop on every read of the reel. It is probed in a
    thread, once per file (path, size and mtime)."""
    import threading

    up = world.root / "uploads" / "intro.mp4"
    up.parent.mkdir(parents=True, exist_ok=True)
    up.write_bytes(b"upload")
    world.md()["refs"]["intro_video_upload_id"] = 77

    async def upload(uid):
        return up if int(uid) == 77 else None

    threads: list[int] = []

    def probe(p):
        threads.append(threading.get_ident())
        return 3.5

    monkeypatch.setattr(poller, "_fetch_upload_path", upload)
    monkeypatch.setattr(poller, "_probe_video_duration", probe)

    async def reads(n):
        loop = threading.get_ident()
        for _ in range(n):
            block = await th_reel.reel_of(RUN)
        assert block["targets"][0]["src"] == "upload:77"
        return loop

    loop = run(reads(3))
    assert len(threads) == 1 and loop not in threads
    up.write_bytes(b"another upload")                          # another file: probed again
    run(reads(1))
    assert len(threads) == 2


def test_four_404s_give_up_unless_the_file_is_whole(world):
    _ready_once(world)
    world.md()["outro_excluded"] = True
    run(th_reel.finalize(RUN))
    run(_tick(world))
    world.jobs["job-2"] = 404                                 # Remotion restarted
    for n in (1, 2, 3):
        run(_tick(world))
        assert world.md()["composition_status"] == "rendering" and world.md()["render_404s"] == n
    run(_tick(world))
    md = world.md()
    assert md["composition_status"] == "ready" and md["render_failed_seq"] == 2
    assert md["composition_good_seq"] == 1

    # The same, but Remotion had written the whole file before it went.
    world.md()["outro_excluded"] = False
    run(th_reel.finalize(RUN))
    assert world.md()["composition_seq"] == 3
    run(_tick(world))
    out = _finish(world, frames=world.md()["composition_plan"]["total_frames"])
    world.jobs[f"job-{world.next_job}"] = 404
    for _ in range(4):
        run(_tick(world))
    md = world.md()
    assert md["composition_status"] == "ready" and md["composition_good_seq"] == 3
    assert th_reel.good_path(md) == out and "composition_error" not in md


def test_a_short_file_is_not_taken(world):
    run(th_reel.finalize(RUN))
    run(_tick(world))
    _finish(world, frames=300)                               # cut short
    world.jobs["job-1"] = {"status": "failed", "error": "killed"}
    run(_tick(world))
    assert world.md()["composition_status"] == "failed"


def test_a_404_resets_once_the_job_answers(world):
    run(th_reel.finalize(RUN))
    run(_tick(world))
    world.jobs["job-1"] = 404
    run(_tick(world))
    assert world.md()["render_404s"] == 1
    world.jobs["job-1"] = {"status": "rendering"}
    run(_tick(world))
    assert world.md()["render_404s"] == 0


def test_the_submit_waits_for_the_studio_at_most_eight_ticks(world, monkeypatch):
    monkeypatch.setattr(poller, "_house_busy", lambda: True)
    run(th_reel.finalize(RUN))
    for _ in range(poller.GATE_WAIT_TICKS):
        run(_tick(world))
        assert world.posts == []
    run(_tick(world))
    assert len(world.posts) == 1


def test_pruning_keeps_the_new_reel_and_the_one_before(world):
    d = world.comp("x").parent
    d.mkdir(parents=True, exist_ok=True)
    for name in ("final.mp4", "final-1.mp4", "final-2.mp4", "notes.txt"):
        (d / name).write_bytes(b"x")
    md = world.md()
    md.update({"composition_status": "ready", "composition_path": str(d / "final-2.mp4"),
               "composition_good_seq": 2, "composition_seq": 2, "render_owner": th_reel.INSTANCE_ID})
    md["outro_excluded"] = True
    run(th_reel.finalize(RUN))
    run(_tick(world))
    _finish(world)
    run(_tick(world))
    assert sorted(p.name for p in d.iterdir()) == ["final-2.mp4", "final-3.mp4", "notes.txt"]


def test_a_legacy_final_counts_as_the_one_before(world):
    d = world.comp("x").parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "final.mp4").write_bytes(b"x")
    world.md().update({"composition_status": "ready", "composition_path": "yas_producer/talking-head-v2/9301/final.mp4"})
    _ready_once(world)
    assert sorted(p.name for p in d.iterdir()) == ["final-1.mp4", "final.mp4"]
    world.md()["outro_excluded"] = True
    run(th_reel.finalize(RUN))
    run(_tick(world))
    _finish(world)
    run(_tick(world))
    assert sorted(p.name for p in d.iterdir()) == ["final-1.mp4", "final-2.mp4"]


# ---------------------------------------------------------------------------
# Gates, the double press, the stale owner
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patch, words", [
    ({"composition_status": "queued"}, th_reel.NOT_READY),
    ({"composition_status": None}, th_reel.NOT_READY),
    ({"main_video_stale": True}, "These clips are out of date"),
    ({"intro_video_stale": True}, "Intro"),
])
def test_finalize_refuses_what_isnt_ready(world, patch, words):
    world.md().update(patch)
    with pytest.raises(Exception) as err:
        run(th_reel.finalize(RUN))
    assert err.value.status_code == 409 and words in err.value.detail
    assert world.md().get("composition_status") == patch.get("composition_status", "videos_ready")
    assert "render_owner" not in world.md()


def test_an_excluded_stale_clip_does_not_block(world):
    world.md().update({"outro_video_stale": True, "outro_excluded": True})
    assert run(th_reel.finalize(RUN))["status"] == "rendering"


def test_a_clip_still_generating_blocks(world):
    world.store.anim_rows[353]["status"] = "generating"
    with pytest.raises(Exception) as err:
        run(th_reel.finalize(RUN))
    assert err.value.status_code == 409 and err.value.detail == th_reel.NOT_READY


@pytest.mark.parametrize("answer", ["500", "network"])
def test_a_bucket_download_that_fails_is_the_409_naming_the_scene(world, monkeypatch, answer):
    """Review finding 8: the bucket's error (or the network's) went up
    through resolve() as a 500. It is a clip not on this computer."""
    for aid in (351, 352):
        row = world.store.anim_rows[aid]
        Path(row["local_path"]).unlink()
        row.update(storage_bucket="producer-assets", storage_path=f"yas/animations/{aid}.mp4")

    async def url_for(**_kw):
        return "https://bucket.example/clip.mp4?sig=1"

    def reply(_request):
        if answer == "network":
            raise httpx.ConnectError("no route to host")
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(asset_storage, "url_for", url_for)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: _REAL_CLIENT(*a, transport=httpx.MockTransport(reply), **kw))
    with pytest.raises(Exception) as err:
        run(th_reel.finalize(RUN))
    assert err.value.status_code == 409 and err.value.detail == "Main's clip isn't on this computer."
    assert world.md()["composition_status"] == "videos_ready"
    # The studio's intro (resolve_target, remote): nothing found, no error.
    intro = run(th_reel.resolve_target(world.store.rows[RUN], "intro", remote=True))
    assert intro.path is None and intro.src is None
    cache = th_reel.cache_dir()
    assert not cache.exists() or not list(cache.glob("anim-*"))          # no half-written clip left


def test_a_missing_main_clip_is_a_409_naming_it(world):
    Path(world.store.anim_rows[351]["local_path"]).unlink()
    with pytest.raises(Exception) as err:
        run(th_reel.finalize(RUN))
    assert err.value.status_code == 409 and err.value.detail == "Main's clip isn't on this computer."
    assert world.md()["composition_status"] == "videos_ready"


def test_a_clip_the_studio_fetched_leaves_the_reels_hash_alone(world):
    """Review finding 1: the main clip is only in the bucket. The page reads
    the reel; opening the scene in the studio fetches the clip into the
    rebrand cache; Finalize with the hash the page read was "The reel
    changed in another tab…", every time, until a reload. A main scene is
    its clip whether or not the file is here: the hash doesn't move."""
    Path(world.store.anim_rows[351]["local_path"]).unlink()
    page = run(th_reel.reel_of(RUN))
    main = next(t for t in page["targets"] if t["target"] == "scene_0")
    assert main["src"] == "anim:351"
    with pytest.raises(Exception) as err:                       # nothing to choose on until it is here
        run(th_reel.put_choice(RUN, "scene_0", {"end_frame": 200}))
    assert err.value.status_code == 409 and err.value.detail == "Main's clip isn't on this computer."
    cached = th_reel.cache_dir() / "anim-351.mp4"               # what the studio's prepare leaves
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"clip")
    assert run(th_reel.reel_of(RUN))["inputs_sha"] == page["inputs_sha"]
    got = run(th_reel.finalize(RUN, expect_sha=page["inputs_sha"]))
    assert got["status"] == "rendering" and got["sha"] == page["inputs_sha"]
    rel = Path(world.md()["composition_plan"]["segments"][1]["file_rel"])
    assert rel.parts[-2:] == ("_cache", "anim-351.mp4")


def test_a_choice_survives_its_clip_leaving_this_computer(world):
    run(th_reel.put_choice(RUN, "scene_0", {"end_frame": 200}))
    before = run(th_reel.reel_of(RUN))
    Path(world.store.anim_rows[351]["local_path"]).unlink()
    after = run(th_reel.reel_of(RUN))
    assert after["inputs_sha"] == before["inputs_sha"]
    main = next(t for t in after["targets"] if t["target"] == "scene_0")
    assert main["src"] == "anim:351" and main["end"]["user"] == 200


def test_expect_sha_from_another_tab_is_a_409(world):
    sha = run(th_reel.current_sha(world.store.rows[RUN]))
    with pytest.raises(Exception) as err:
        run(th_reel.finalize(RUN, expect_sha="0" * 16))
    assert err.value.status_code == 409 and err.value.detail == th_reel.CHANGED_ELSEWHERE
    assert run(th_reel.finalize(RUN, expect_sha=sha))["sha"] == sha


class _Interleaving(th_reel.MemoryStore):
    """Every read yields, so two presses read the row before either writes."""

    async def run(self, run_id):
        await asyncio.sleep(0)
        return await super().run(run_id)

    async def anims(self, ids):
        await asyncio.sleep(0)
        return await super().anims(ids)


def test_a_double_press_makes_one_render(world):
    store = _Interleaving(world.store.rows, world.store.anim_rows)
    th_reel.set_store(store)

    async def both():
        return await asyncio.gather(th_reel.finalize(RUN), th_reel.finalize(RUN))

    a, b = run(both())
    assert a["status"] == b["status"] == "rendering" and a["sha"] == b["sha"]
    assert sum(1 for w in store.writes if w[0] == "finalize") == 2        # both tried …
    assert store.rows[RUN]["metadata"]["composition_seq"] == 1             # … one won
    world.store = store
    run(_tick(world))
    run(_tick(world))
    assert len(world.posts) == 1


class _ReadEarlier(th_reel.MemoryStore):
    """The first read of the run is a copy taken earlier (tab B read the
    row, then tab A's whole render happened before B's compare-and-set)."""

    def __init__(self, inner: th_reel.MemoryStore, stale: dict):
        super().__init__(inner.rows, inner.anim_rows, inner.exps)
        self.writes = inner.writes
        self.stale: dict | None = stale

    async def run(self, run_id):
        if self.stale is not None:
            row, self.stale = self.stale, None
            return copy.deepcopy(row)
        return await super().run(run_id)


def test_a_press_that_read_before_another_whole_render_never_renders_over_it(world):
    """Review finding 2: B's read came before A's Finalize and its whole
    render; B's compare-and-set took the same seq, and rendered to the very
    final-1.mp4 being served. The seq is checked now: the reel A made is the
    one B asked for, so B renders nothing."""
    stale = copy.deepcopy(world.store.rows[RUN])
    _ready_once(world)
    served = th_reel.good_path(world.md())
    assert served == world.comp("final-1.mp4")
    store = _ReadEarlier(world.store, stale)
    th_reel.set_store(store)
    got = run(th_reel.finalize(RUN))
    assert got == {"status": "ready", "rendering": False, "sha": world.md()["composition_good_sha"]}
    assert world.md()["composition_seq"] == 1 and world.md()["composition_status"] == "ready"
    posts = len(world.posts)
    run(_tick(world))
    assert len(world.posts) == posts and th_reel.good_path(world.md()) == served


def test_a_stale_press_for_another_reel_is_told_the_reel_changed(world):
    stale = copy.deepcopy(world.store.rows[RUN])
    stale["metadata"]["outro_excluded"] = True                   # B saw (and wants) another reel
    _ready_once(world)
    th_reel.set_store(_ReadEarlier(world.store, stale))
    with pytest.raises(Exception) as err:
        run(th_reel.finalize(RUN))
    assert err.value.status_code == 409 and err.value.detail == th_reel.CHANGED_ELSEWHERE
    assert world.md()["composition_seq"] == 1 and world.md()["composition_status"] == "ready"


def test_a_second_press_while_rendering_is_the_same_render(world):
    run(th_reel.finalize(RUN))
    before = copy.deepcopy(world.md())
    assert run(th_reel.finalize(RUN))["status"] == "rendering"
    assert world.md() == before


def test_a_press_while_another_reel_renders_is_refused(world):
    run(th_reel.finalize(RUN))
    world.md()["outro_excluded"] = True
    with pytest.raises(Exception) as err:
        run(th_reel.finalize(RUN))
    assert err.value.status_code == 409 and err.value.detail == th_reel.ALREADY_RENDERING


@pytest.mark.parametrize("minutes, taken", [(21, True), (19, False)])
def test_a_render_whose_owner_is_gone_20_minutes_is_taken_over(world, minutes, taken):
    started = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    world.md().update({"composition_status": "rendering", "render_owner": "old-container", "render_job_id": "j",
                       "render_started_at": started, "composition_seq": 4, "composition_inputs_sha": "x" * 16})
    if taken:
        run(th_reel.finalize(RUN))
        md = world.md()
        assert md["render_owner"] == th_reel.INSTANCE_ID and md["composition_seq"] == 5 and md["render_job_id"] == ""
    else:
        with pytest.raises(Exception) as err:
            run(th_reel.finalize(RUN))
        assert err.value.status_code == 409


def test_a_legacy_rendering_row_is_never_taken_over(world):
    started = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    world.md().update({"composition_status": "rendering", "render_job_id": "j", "render_started_at": started})
    with pytest.raises(Exception):
        run(th_reel.finalize(RUN))
    assert "render_owner" not in world.md()


def test_finalize_cancels_the_runs_draft_renders(monkeypatch, tmp_path):
    from app.services.rebrand_harness import studio

    mine = studio._Draft(key="a", folder=str(studio.STUDIO_ROOT / str(RUN) / "scene_0_x"), out=tmp_path / "a.mp4",
                         state="running")
    other = studio._Draft(key="b", folder=str(studio.STUDIO_ROOT / "815" / "scene_0_y"), out=tmp_path / "b.mp4",
                          state="running")
    monkeypatch.setattr(studio, "_DRAFTS", {"a": mine, "b": other})
    assert th_reel._cancel_run_drafts(RUN) == 1
    assert mine.state == "cancelled" and mine.stop.is_set() and other.state == "running"


# ---------------------------------------------------------------------------
# The very SQL: the compare-and-set, and what old code sees of our row
# ---------------------------------------------------------------------------

def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _jsonb(v: Any) -> Any:
    return getattr(v, "obj", v)


class FakeDb:
    """yas_generations as the statements this round runs (and the ones the
    old poller runs) see it: each statement is recognised by its exact text
    and applied to the rows in Python, so an edit to the SQL fails here
    until the mirror is edited too."""

    def __init__(self, rows: dict[int, dict]):
        self.rows = rows
        self.seen: list[str] = []

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def select(self, pred) -> list[dict]:
        return [copy.deepcopy(r) for r in self.rows.values() if pred(r["metadata"])]

    def execute(self, sql: str, params: tuple) -> list[dict]:
        s = _norm(sql)
        self.seen.append(s)
        if s == _norm(th_reel.FINALIZE_SQL):
            patch, rid, seq, statuses = _jsonb(params[0]), params[1], params[2], params[3]
            row = self.rows.get(rid)
            if row is None:
                return []
            md = row["metadata"]
            if int(md.get("composition_seq") or 0) != seq:
                return []
            started = th_reel._utc(md.get("render_started_at"))
            ok = md.get("composition_status") in statuses or (
                md.get("composition_status") == "rendering" and "render_owner" in md and started is not None
                and started < self._now() - timedelta(minutes=20))
            if not ok:
                return []
            md = {k: v for k, v in md.items() if k not in ("composition_error", "render_failed_seq")}
            row["metadata"] = {**md, **patch}
            return [{"id": rid}]
        if s == _norm(th_reel.PATCH_SQL):
            patch, rid = _jsonb(params[0]), params[1]
            if rid in self.rows:
                self.rows[rid]["metadata"] = {**self.rows[rid]["metadata"], **patch}
            return []
        if s == _norm(th_reel.RUN_SQL):
            row = self.rows.get(params[0])
            return [copy.deepcopy(row)] if row and params[1] == th_reel.MODE_PREVIEW else []
        rendering = lambda md, p: md.get("composition_status") == p  # noqa: E731
        if s == _norm(UNSUBMITTED_SQL):                          # today's, and the old code's
            return self.select(lambda md: rendering(md, params[1]) and "render_job_id" not in md)
        if s == _norm(OLD_SUBMITTED_SQL):
            return self.select(lambda md: rendering(md, params[1]) and "render_job_id" in md)
        if s == _norm(OWNED_SQL):
            return self.select(lambda md: rendering(md, params[1]) and md.get("render_owner") == params[2]
                               and (md.get("render_job_id") or "") == "")
        if s == _norm(SUBMITTED_SQL):
            return self.select(lambda md: rendering(md, params[1]) and "render_job_id" in md
                               and (md.get("render_job_id") or "") != ""
                               and ("render_owner" not in md or md.get("render_owner") == params[2]))
        raise AssertionError(f"unknown SQL: {s}")


UNSUBMITTED_SQL = """
                SELECT id, topic, local_path, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                   AND NOT (metadata ? 'render_job_id')
                """
OLD_SUBMITTED_SQL = """
                SELECT id, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                   AND metadata ? 'render_job_id'
                """
OWNED_SQL = """
                SELECT id, topic, local_path, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                   AND metadata->>'render_owner' = %s
                   AND COALESCE(metadata->>'render_job_id', '') = ''
                """
SUBMITTED_SQL = """
                SELECT id, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                   AND metadata ? 'render_job_id'
                   AND COALESCE(metadata->>'render_job_id', '') <> ''
                   AND (NOT (metadata ? 'render_owner') OR metadata->>'render_owner' = %s)
                """


class _Cur:
    def __init__(self, db: FakeDb):
        self.db, self.out = db, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params=()):
        self.out = self.db.execute(sql, params)

    async def fetchone(self):
        return self.out[0] if self.out else None

    async def fetchall(self):
        return list(self.out)


class _Conn:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def cursor(self, *_a, **_k):
        return _Cur(self.db)


def _fake_get_conn(db: FakeDb):
    def get_conn(*_a, **_k):
        return _Conn(db)
    return get_conn


def _old_poller() -> types.ModuleType:
    """The composition poller as the production VPS runs it (9f788d3 ==
    main's), loaded from git as its own module."""
    try:
        src = subprocess.run(["git", "show", f"{OLD_COMMIT}:backend/app/workers/yas_th_v2_composition_poller.py"],
                             cwd=BACKEND.parent, capture_output=True, text=True, encoding="utf-8",
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:     # pragma: no cover — no git here
        pytest.fail(f"the old poller can't be read from git: {e}")
    mod = types.ModuleType("app.workers._th_poller_old")
    mod.__package__ = "app.workers"
    exec(compile(src, f"yas_th_v2_composition_poller@{OLD_COMMIT}", "exec"), mod.__dict__)
    return mod


def test_the_compare_and_set_sql_runs_as_the_memory_store_does(world, monkeypatch):
    db = FakeDb({RUN: copy.deepcopy(world.store.rows[RUN])})
    monkeypatch.setattr(th_reel, "get_conn", _fake_get_conn(db))
    pg = th_reel.PgStore()
    mem = th_reel.MemoryStore({RUN: copy.deepcopy(world.store.rows[RUN])})
    stale = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    # (state, the seq the press read, taken?)
    cases = [
        ({"composition_status": "videos_ready"}, 0, True),
        ({"composition_status": "ready", "composition_error": "x", "render_failed_seq": 2,
          "composition_seq": 2}, 2, True),
        ({"composition_status": "failed", "composition_seq": 1}, 1, True),
        ({"composition_status": "queued"}, 0, False),
        ({"composition_status": "rendering", "render_owner": "gone", "render_started_at": stale}, 0, True),
        ({"composition_status": "rendering", "render_owner": "here", "render_started_at": fresh}, 0, False),
        ({"composition_status": "rendering", "render_started_at": stale}, 0, False),       # legacy: never
        # Read before another press's render took the next seq: refused.
        ({"composition_status": "ready", "composition_seq": 1}, 0, False),
        ({"composition_status": "videos_ready", "composition_seq": 3}, 2, False),
        ({"composition_status": "rendering", "render_owner": "gone", "render_started_at": stale,
          "composition_seq": 4}, 3, False),
    ]
    for state, seq, ok in cases:
        base = {**_single_md(), **state}
        db.rows[RUN]["metadata"] = copy.deepcopy(base)
        mem.rows[RUN]["metadata"] = copy.deepcopy(base)
        patch = {"composition_status": "rendering", "render_job_id": "", "render_owner": th_reel.INSTANCE_ID}
        assert run(pg.finalize(RUN, patch, th_reel.FINALIZE_FROM, seq)) is ok, state
        assert run(mem.finalize(RUN, patch, th_reel.FINALIZE_FROM, seq)) is ok, state
        assert db.rows[RUN]["metadata"] == mem.rows[RUN]["metadata"], state
        if ok:
            assert "composition_error" not in db.rows[RUN]["metadata"]
    assert run(pg.run(RUN))["metadata"] == mem.rows[RUN]["metadata"]
    run(pg.patch(RUN, {"render_404s": 2}))
    assert db.rows[RUN]["metadata"]["render_404s"] == 2


def test_the_pollers_queries_pick_only_what_they_own(world, monkeypatch):
    db = FakeDb({})
    monkeypatch.setattr(poller, "get_conn", _fake_get_conn(db))
    md = dict(_single_md(), composition_status="rendering")
    db.rows = {
        1: {"id": 1, "metadata": dict(md)},                                                      # legacy, unsubmitted
        2: {"id": 2, "metadata": dict(md, render_job_id="j2")},                                  # legacy, submitted
        3: {"id": 3, "metadata": dict(md, render_job_id="", render_owner=th_reel.INSTANCE_ID)},  # ours, unsubmitted
        4: {"id": 4, "metadata": dict(md, render_job_id="j4", render_owner=th_reel.INSTANCE_ID)},
        5: {"id": 5, "metadata": dict(md, render_job_id="", render_owner="another-host")},
        6: {"id": 6, "metadata": dict(md, render_job_id="j6", render_owner="another-host")},
    }
    ids = lambda rows: sorted(int(r["id"]) for r in rows)  # noqa: E731
    assert ids(run(poller._pending_rendering_unsubmitted())) == [1]
    assert ids(run(poller._pending_rendering_owned())) == [3]
    assert ids(run(poller._pending_rendering_submitted())) == [2, 4]


def test_old_code_never_touches_our_row(world, monkeypatch):
    """The production VPS runs the poller at 9f788d3 against the same rows:
    right after our compare-and-set its phase 2 skips our row, and its
    phase 3 selects it but its _check_render returns before any call."""
    run(th_reel.finalize(RUN))
    ours = copy.deepcopy(world.store.rows[RUN])
    old = _old_poller()
    db = FakeDb({RUN: ours})
    calls: list[str] = []

    def recording(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_CLIENT(*args, transport=httpx.MockTransport(
            lambda r: calls.append(str(r.url)) or httpx.Response(404)), **kwargs)

    monkeypatch.setattr(old, "get_conn", _fake_get_conn(db))
    monkeypatch.setattr(old, "httpx", types.SimpleNamespace(AsyncClient=recording))
    patched: list = []

    async def old_patch(run_id, patch):
        patched.append(patch)

    monkeypatch.setattr(old, "_patch_metadata", old_patch)
    assert run(old._pending_rendering_unsubmitted()) == []                  # phase 2: not picked
    picked = run(old._pending_rendering_submitted())
    assert [int(r["id"]) for r in picked] == [RUN]                          # phase 3 selects it …
    for row in picked:
        run(old._check_render(row))                                         # … and returns at once
    assert calls == [] and patched == []
    # The whole old tick, too: nothing sent, nothing written.
    monkeypatch.setattr(old, "_pending_queued", lambda: _async([]))
    run(old._poll_once())
    assert calls == [] and patched == [] and db.rows[RUN] == ours


async def _async(v):
    return v


def test_the_legacy_path_is_unchanged_for_a_row_without_an_owner(world):
    """A legacy row (no render_owner): a 404 leaves it alone forever (as
    today), a done job is taken the old way (final.mp4 in place)."""
    patches: list = []

    async def legacy_patch(run_id, patch):
        patches.append(patch)

    world.monkeypatch.setattr(poller, "_patch_metadata", legacy_patch)
    row = {"id": RUN, "metadata": dict(_single_md(), composition_status="rendering", render_job_id="job-9",
                                       composition_output_path="yas_producer/talking-head-v2/9301/final.mp4")}
    for _ in range(6):
        run(poller._check_render(copy.deepcopy(row)))
    assert len(world.gets) == 6 and patches == [] and world.store.writes == []


# ---------------------------------------------------------------------------
# Serving: the route, the URL, the Library tile, saved_current
# ---------------------------------------------------------------------------

def _owned_md(tmp: Path, **kw) -> dict:
    f = tmp / "final-2.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
    md = _single_md(composition_status="rendering", render_owner=th_reel.INSTANCE_ID, render_job_id="job",
                    composition_path=str(f), composition_good_seq=2, composition_seq=3)
    md.update(kw)
    return md


def _yp_client(monkeypatch, row: dict) -> TestClient:
    from app.routers import yas_producer as yp

    async def fetch(run_id):
        return copy.deepcopy(row)

    monkeypatch.setattr(yp.th_v2_service, "_fetch_run_row", fetch)
    app = FastAPI()
    app.include_router(yp.router)
    return TestClient(app)


def test_the_reel_is_served_whatever_its_status_and_never_as_immutable(world, monkeypatch, tmp_path):
    row = {"id": RUN, "metadata": _owned_md(tmp_path)}
    client = _yp_client(monkeypatch, row)
    r = client.get(f"/api/yas-producer/talking-head-v2/{RUN}/composition/file?v=2")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-cache"
    assert r.headers.get("etag") and r.headers.get("last-modified")
    r = client.get(f"/api/yas-producer/talking-head-v2/{RUN}/composition/file?v=2&download=1")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "private, no-cache"


def test_a_legacy_reel_keeps_the_ready_gate(world, monkeypatch, tmp_path):
    md = _owned_md(tmp_path)
    md.pop("render_owner")
    client = _yp_client(monkeypatch, {"id": RUN, "metadata": md})
    assert client.get(f"/api/yas-producer/talking-head-v2/{RUN}/composition/file").status_code == 409
    md["composition_status"] = "ready"
    client = _yp_client(monkeypatch, {"id": RUN, "metadata": md})
    r = client.get(f"/api/yas-producer/talking-head-v2/{RUN}/composition/file")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-cache"


@pytest.mark.parametrize("storage_seq, served", [(2, True), (1, False), (None, False)])
def test_the_bucket_copy_counts_only_when_it_holds_the_good_seq(world, monkeypatch, tmp_path, storage_seq, served):
    md = _owned_md(tmp_path, composition_status="ready", composition_path=str(tmp_path / "gone.mp4"),
                   composition_storage_bucket="producer-assets",
                   composition_storage_path=f"yas/compositions/talking-head/{RUN}.mp4")
    if storage_seq is not None:
        md["composition_storage_seq"] = storage_seq

    async def signed(**_k):
        return "https://bucket.example/signed?token=1"

    monkeypatch.setattr(asset_storage, "url_for", signed)
    client = _yp_client(monkeypatch, {"id": RUN, "metadata": md})
    r = client.get(f"/api/yas-producer/talking-head-v2/{RUN}/composition/file", follow_redirects=False)
    assert r.status_code == (307 if served else 410)
    assert th_reel.bucket_current(md) is served


def test_the_url_names_the_good_seq(tmp_path):
    md = _owned_md(tmp_path)
    assert svc.composition_url(RUN, md) == f"/api/yas-producer/talking-head-v2/{RUN}/composition/file?v=2"
    assert svc.composition_url(RUN, dict(md, composition_path=None)) is None
    legacy = dict(md)
    legacy.pop("render_owner")
    assert svc.composition_url(RUN, legacy) is None                               # rendering: not ready
    assert svc.composition_url(RUN, dict(legacy, composition_status="ready")) == \
        f"/api/yas-producer/talking-head-v2/{RUN}/composition/file"
    # A legacy reel being made again (no good seq yet) is served as v=0.
    assert svc.composition_url(RUN, dict(md, composition_good_seq=None)).endswith("?v=0")


def test_the_library_tile_shows_the_last_good_reel_while_a_new_one_renders(tmp_path):
    from app.routers import yas_producer as yp

    row = {"id": RUN, "mode": svc.MODE_PREVIEW, "metadata": {
        "composition_status": "rendering", "render_owner": "x", "composition_path": "p", "composition_good_seq": 4}}
    item = yp._library_item(row)
    assert item["video_url"] == f"/api/yas-producer/talking-head-v2/{RUN}/composition/file?v=4"
    assert item["has_video"] and item["preview_kind"] == "final"
    legacy = {"id": RUN, "mode": svc.MODE_PREVIEW, "metadata": {"composition_status": "ready"}}
    assert yp._library_item(legacy)["video_url"] == f"/api/yas-producer/talking-head-v2/{RUN}/composition/file"


@pytest.mark.parametrize("md, saved, current", [
    ({"render_owner": "x", "composition_good_seq": 2, "saved_seq": 2}, 7, True),
    ({"render_owner": "x", "composition_good_seq": 3, "saved_seq": 2}, 7, False),
    ({"render_owner": "x", "composition_good_seq": 1}, 7, False),     # saved before the first Finalize here
    ({"render_owner": "x", "composition_good_seq": None}, 7, True),   # … and that reel is still served
    ({"render_owner": "x", "composition_good_seq": 2, "saved_seq": 2}, None, False),
    ({}, 7, True),
    ({}, None, False),
])
def test_saved_current(md, saved, current):
    assert svc.saved_current({"saved_asset_id": saved}, md) is current


def test_fetch_run_says_what_is_served(world, monkeypatch, tmp_path):
    md = _owned_md(tmp_path, composition_error="renderer crashed")
    row = {"id": RUN, "topic": "t", "status": "ready", "saved_asset_id": 5, "metadata": md,
           "local_path": None, "created_at": None, "updated_at": None}

    async def fetch(run_id):
        return copy.deepcopy(row)

    async def anim(anim_id):
        return {"id": anim_id, "status": "ready", "local_path": None, "duration_sec": 8}

    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(svc, "_fetch_run_row", fetch)
    monkeypatch.setattr(svc, "_fetch_animation_row", anim)
    monkeypatch.setattr(svc, "_fetch_card_row", none)
    got = run(svc.fetch_run(RUN))
    assert got["composition_url"].endswith("/composition/file?v=2")
    assert got["reel_ready"] is True and got["composition_seq"] == 3 and got["composition_good_seq"] == 2
    assert got["composition_error"] == "renderer crashed" and got["saved_current"] is False
    row["metadata"]["main_video_stale"] = True
    assert run(svc.fetch_run(RUN))["reel_ready"] is False


def test_save_to_assets_records_which_reel_was_saved(world, monkeypatch, tmp_path):
    md = _owned_md(tmp_path)                                   # a new render is under way
    row = {"id": RUN, "topic": "t", "metadata": md}
    sql: list = []

    async def fetch(run_id):
        return copy.deepcopy(row)

    async def upload(**_k):
        return {"storage_bucket": None, "storage_path": None, "local_path": "x.mp4"}

    async def embed(_texts):
        return [None]

    class Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, s, params=()):
            sql.append((_norm(s), params))

        async def fetchone(self):
            return {"id": 77}

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def cursor(self, *_a, **_k):
            return Cur()

    monkeypatch.setattr(svc, "_fetch_run_row", fetch)
    monkeypatch.setattr(svc.asset_storage, "upload", upload)
    monkeypatch.setattr(svc, "gemini_embed", embed)
    monkeypatch.setattr(svc, "get_conn", lambda *a, **k: Conn())
    assert run(svc.save_to_assets(run_id=RUN))["id"] == 77
    update = next(p for s, p in sql if s.startswith("UPDATE yas_generations"))
    assert "metadata = metadata ||" in next(s for s, p in sql if s.startswith("UPDATE yas_generations"))
    assert _jsonb(update[2]) == {"saved_seq": 2}


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def test_the_stitch_route_takes_an_optional_body(world, monkeypatch):
    from app.routers import yas_producer as yp

    seen: list = []

    async def finalize_reel(run_id, *, expect_sha=None):
        seen.append((run_id, expect_sha))
        return {"id": run_id}

    monkeypatch.setattr(yp.th_v2_service, "finalize_reel", finalize_reel)
    app = FastAPI()
    app.include_router(yp.router)
    client = TestClient(app)
    assert client.post(f"/api/yas-producer/talking-head-v2/{RUN}/stitch").status_code == 200
    assert client.post(f"/api/yas-producer/talking-head-v2/{RUN}/stitch", json={}).status_code == 200
    assert client.post(f"/api/yas-producer/talking-head-v2/{RUN}/stitch", json={"expect_sha": "abc"}).status_code == 200
    assert seen == [(RUN, None), (RUN, None), (RUN, "abc")]


def test_a_double_post_through_the_route_makes_one_render(world, monkeypatch):
    from app.routers import yas_producer as yp

    async def fetch_run(run_id):
        return {"id": run_id, "composition_status": world.md()["composition_status"]}

    monkeypatch.setattr(svc, "fetch_run", fetch_run)
    app = FastAPI()
    app.include_router(yp.router)
    client = TestClient(app)
    for _ in range(2):
        r = client.post(f"/api/yas-producer/talking-head-v2/{RUN}/stitch", json={})
        assert r.status_code == 200 and r.json()["composition_status"] == "rendering"
    run(_tick(world))
    run(_tick(world))
    assert len(world.posts) == 1 and world.md()["composition_seq"] == 1


def test_the_instance_is_the_video_editors():
    from app.services import yas_video_editor as ve

    assert th_reel.INSTANCE_ID == ve.INSTANCE_ID and th_reel.INSTANCE_ID


def test_the_conftest_guard_keeps_the_reel_in_memory():
    assert isinstance(th_reel.store(), th_reel.MemoryStore)
