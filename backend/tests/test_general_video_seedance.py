"""The Seedance engine inside General AI Video.

General AI Video makes a still, then animates it with Veo (the default) or
Seedance 2.5 via Higgsfield. These tests drive the real app with the store and
the Higgsfield client replaced by in-memory fakes: no database, no network.
They pin the rules the engine switch adds:

- Veo refuses the shapes only Seedance makes; bad choices are refused before
  anything is claimed or paid.
- The still is claimed before any paid call, so a second request for it gets a
  409, and it is put back as it was when the engine never took the job.
- Seedance gets one image-to-video request built from the still, carrying one
  Idempotency-Key that is reused when the answer is lost.
- Spend is reserved in a ledger before paying: the cap refuses up front, a
  refused submit is written off, an unanswered one stays counted, and
  re-animating a clip never erases what was already spent.
- The poller finishes a clip, writes off jobs Higgsfield did not charge,
  retries a paid video it could not save, and keeps going when one clip fails.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

import app.routers.yas_producer as yp
from app.services import higgsfield_client as hf
from app.workers import yas_general_video_poller as poller


def _rec(**over):
    rec = {
        "id": 5, "prompt": "students coding in the courtyard", "status": "frame_ready",
        "aspect_ratio": "1:1", "frame_storage_bucket": "producer-media",
        "frame_storage_path": "general-video/5-frame.png", "frame_local_path": None,
        "engine": None, "error": None, "veo_operation_name": None,
        "seedance_request_id": None, "seedance_status_url": None,
        "created_at": "2026-10-07T10:00:00+00:00", "updated_at": "2026-10-07T10:00:00+00:00",
    }
    rec.update(over)
    return rec


def _job(n):
    return {"request_id": f"req-{n}", "status": "queued",
            "status_url": f"https://api.example/requests/req-{n}/status",
            "cancel_url": f"https://api.example/requests/req-{n}/cancel",
            "correlation_id": "cid"}


async def _async(value):
    return value


@pytest.fixture
def client(monkeypatch):
    from app import access
    monkeypatch.setattr(access, "enabled", lambda: False)

    state = {
        "rec": _rec(), "updates": [], "claims": [], "ledger": [], "connected": True,
        "uploaded": [], "submitted": [], "submit_script": [],
    }

    async def _get(gen_id):
        return dict(state["rec"]) if int(gen_id) == 5 else None

    async def _update(gen_id, **fields):
        state["updates"].append(fields)
        state["rec"] = {**state["rec"], **fields}
        return dict(state["rec"])

    async def _claim(gen_id, engine, from_statuses=("frame_ready", "ready", "failed")):
        state["claims"].append(engine)
        if state["rec"]["status"] not in from_statuses:
            return None
        state["rec"] = {**state["rec"], "status": "animating", "engine": engine, "error": None,
                        "veo_operation_name": None, "seedance_request_id": None,
                        "seedance_status_url": None}
        return dict(state["rec"])

    async def _reserve(gen_id, cost_usd, *, cap_usd, within_days=30):
        spent = sum(r["cost"] for r in state["ledger"] if not r["written_off"])
        if cap_usd > 0 and spent + cost_usd > cap_usd:
            return None, spent
        state["ledger"].append({"id": len(state["ledger"]) + 1, "gen_id": gen_id, "cost": cost_usd,
                                "request_id": None, "written_off": False})
        return len(state["ledger"]), spent

    async def _attach(spend_id, request_id):
        state["ledger"][spend_id - 1]["request_id"] = request_id

    async def _write_off(*, spend_id=None, request_id=None):
        for r in state["ledger"]:
            if (spend_id is not None and r["id"] == spend_id) or (request_id and r["request_id"] == request_id):
                r["written_off"] = True

    async def _upload(data, mime):
        state["uploaded"].append(mime)
        return "https://cdn.example/still.png"

    async def _submit(route, payload, *, idempotency_key=None, **kw):
        state["submitted"].append((route, payload, idempotency_key))
        step = state["submit_script"].pop(0) if state["submit_script"] else None
        if isinstance(step, Exception):
            raise step
        return _job(len(state["submitted"]))

    monkeypatch.setattr(yp.gv_store, "get", _get)
    monkeypatch.setattr(yp.gv_store, "update", _update)
    monkeypatch.setattr(yp.gv_store, "claim_for_animation", _claim)
    monkeypatch.setattr(yp.gv_store, "reserve_seedance_spend", _reserve)
    monkeypatch.setattr(yp.gv_store, "attach_seedance_request", _attach)
    monkeypatch.setattr(yp.gv_store, "write_off_seedance_spend", _write_off)
    monkeypatch.setattr(yp, "_media_bytes", lambda **k: _async(b"\x89PNG\r\n\x1a\n" + b"0" * 32))
    monkeypatch.setattr(yp, "_GV_SEEDANCE_RETRY_BACKOFF_SEC", 0)
    monkeypatch.setattr(yp, "_GV_RECORD_RETRY_SEC", 0)
    monkeypatch.setattr(yp, "config", dataclasses.replace(yp.config, seedance_budget_usd=50.0))
    monkeypatch.setattr(yp.higgsfield, "connected", lambda: state["connected"])
    monkeypatch.setattr(yp.higgsfield, "upload_file", _upload)
    monkeypatch.setattr(yp.higgsfield, "submit", _submit)

    from app.main import app
    c = TestClient(app)
    c.state_ = state  # type: ignore[attr-defined]
    return c


def _animate(client, **body):
    return client.post("/api/yas-producer/general-video/5/animate", json=body)


def test_engines_lists_what_each_engine_accepts(client):
    body = client.get("/api/yas-producer/general-video/engines").json()
    assert body["default"] == "veo"
    assert body["veo"]["aspects"] == ["9:16", "16:9"]
    assert body["veo"]["durations"] == [4, 6, 8]
    sd = body["seedance"]
    assert sd["connected"] is True
    assert set(sd["aspects"]) == {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9"}
    assert (sd["duration_min"], sd["duration_max"]) == (4, 30)
    assert sd["resolutions"] == ["480p", "720p", "1080p"]


def test_veo_refuses_a_shape_only_seedance_makes(client):
    r = _animate(client, engine="veo")
    assert r.status_code == 422
    assert "Seedance" in r.json()["detail"]
    assert client.state_["claims"] == [] and client.state_["submitted"] == []


def test_seedance_sends_one_image_to_video_request_from_the_still(client):
    r = _animate(client, engine="seedance", duration_sec=12, resolution="1080p", with_audio=False)
    assert r.status_code == 200, r.text
    st = client.state_
    assert st["claims"] == ["seedance"]
    assert st["uploaded"] == ["image/png"]
    [(route, payload, key)] = st["submitted"]
    assert route == "image-to-video"
    assert payload["image_url"] == "https://cdn.example/still.png"
    assert payload["duration"] == 12
    assert payload["resolution"] == "1080p"
    assert payload["generate_audio"] is False
    assert "aspect_ratio" not in payload  # the clip takes the still's shape
    assert key  # every submit carries an Idempotency-Key
    out = r.json()
    assert out["status"] == "animating"
    assert out["engine"] == "seedance"
    assert out["seedance_status_url"].endswith("/req-1/status")
    assert out["veo_operation_name"] is None
    assert out["seedance_cost_usd"] == pytest.approx(12 * 1.1372, abs=0.01)
    assert out["seedance_submitted_at"]
    assert st["ledger"] == [{"id": 1, "gen_id": 5, "cost": pytest.approx(13.6464, abs=0.001),
                             "request_id": "req-1", "written_off": False}]


@pytest.mark.parametrize("body, needle", [
    ({"engine": "seedance", "duration_sec": 31}, "4 to 30"),
    ({"engine": "seedance", "resolution": "4k"}, "resolution"),
    ({"engine": "sora"}, "engine"),
])
def test_bad_choices_are_refused_before_anything_is_claimed(client, body, needle):
    r = _animate(client, **body)
    assert r.status_code == 400
    assert needle in r.json()["detail"]
    st = client.state_
    assert st["claims"] == [] and st["ledger"] == [] and st["submitted"] == []


def test_seedance_without_keys_says_so(client):
    client.state_["connected"] = False
    r = _animate(client, engine="seedance")
    assert r.status_code == 503
    assert "HIGGSFIELD_API_KEY_ID" in r.json()["detail"]
    assert client.state_["claims"] == []


def test_a_still_already_being_animated_gets_a_409_and_nothing_is_paid(client):
    client.state_["rec"]["status"] = "animating"
    r = _animate(client, engine="seedance")
    assert r.status_code == 409
    assert r.json()["detail"] == "This still is already being made into a video."
    assert client.state_["uploaded"] == [] and client.state_["submitted"] == []


def test_losing_the_claim_race_gets_a_409_and_nothing_is_paid(client, monkeypatch):
    # Another request claimed the still between this one's read and its claim.
    monkeypatch.setattr(yp.gv_store, "claim_for_animation", lambda gen_id, engine, **k: _async(None))
    r = _animate(client, engine="seedance")
    assert r.status_code == 409
    st = client.state_
    assert st["ledger"] == [] and st["uploaded"] == [] and st["submitted"] == []


def test_the_budget_cap_refuses_before_upload_and_reopens_the_still(client):
    client.state_["ledger"].append({"id": 1, "gen_id": 4, "cost": 49.0, "request_id": "old", "written_off": False})
    r = _animate(client, engine="seedance", duration_sec=10, resolution="720p")
    assert r.status_code == 402
    assert "SEEDANCE_BUDGET_USD" in r.json()["detail"]
    assert "$49.00 spent" in r.json()["detail"]
    st = client.state_
    assert st["uploaded"] == [] and st["submitted"] == []
    assert st["rec"]["status"] == "frame_ready" and st["rec"]["engine"] is None


def test_written_off_spend_does_not_count_against_the_cap(client):
    client.state_["ledger"].append({"id": 1, "gen_id": 4, "cost": 49.0, "request_id": "old", "written_off": True})
    assert _animate(client, engine="seedance", duration_sec=10, resolution="720p").status_code == 200


def test_a_refused_submit_is_written_off_and_the_still_reopens(client):
    client.state_["submit_script"] = [hf.HiggsfieldError("no credits", code="credits", status=403)]
    r = _animate(client, engine="seedance")
    assert r.status_code == 402
    st = client.state_
    assert len(st["submitted"]) == 1  # not retried
    assert st["ledger"][0]["written_off"] is True
    assert st["rec"]["status"] == "frame_ready"


def test_a_lost_answer_is_repeated_with_the_same_key_and_pays_once(client):
    client.state_["submit_script"] = [hf.HiggsfieldError("slow", code="timeout"), None]
    r = _animate(client, engine="seedance")
    assert r.status_code == 200, r.text
    (_, p1, k1), (_, p2, k2) = client.state_["submitted"]
    assert p1 == p2 and k1 == k2
    assert [row["request_id"] for row in client.state_["ledger"]] == ["req-2"]


def test_an_unanswered_submit_stays_counted_and_is_not_reopened(client):
    err = hf.HiggsfieldError("Higgsfield did not confirm the request in time. Check your Higgsfield dashboard.",
                             code="timeout")
    client.state_["submit_script"] = [err, err, err]
    r = _animate(client, engine="seedance")
    assert r.status_code == 504
    st = client.state_
    assert len(st["submitted"]) == yp._GV_SEEDANCE_SUBMIT_ATTEMPTS
    assert len({k for _, _, k in st["submitted"]}) == 1
    assert st["ledger"][0]["written_off"] is False
    assert st["rec"]["status"] == "failed"
    assert "dashboard" in st["rec"]["error"]


@pytest.mark.parametrize("script", [
    [hf.HiggsfieldError("slow", code="timeout"),
     hf.HiggsfieldError("slow down", code="rate_limited", status=429),
     hf.HiggsfieldError("slow down", code="rate_limited", status=429)],
    [hf.HiggsfieldError("slow", code="timeout"),
     hf.HiggsfieldError("busy", code="concurrency", status=400)],
    [hf.HiggsfieldError("boom", code="upstream", status=500),
     hf.HiggsfieldError("bad keys", code="auth", status=401)],
], ids=["timeout-then-429s", "timeout-then-concurrency", "5xx-then-401"])
def test_a_refused_repeat_does_not_hide_an_earlier_unknown_outcome(client, script):
    # Higgsfield does not consume a key on a request it rejects, so a refused
    # repeat says nothing about whether the first attempt was accepted.
    client.state_["submit_script"] = list(script)
    r = _animate(client, engine="seedance")
    assert r.status_code in (502, 504)
    assert "dashboard" in r.json()["detail"]
    st = client.state_
    assert len({k for _, _, k in st["submitted"]}) == 1
    assert st["ledger"][0]["written_off"] is False
    assert st["rec"]["status"] == "failed" and "dashboard" in st["rec"]["error"]


def test_an_answer_without_a_request_id_is_repeated_with_the_same_key(client):
    client.state_["submit_script"] = [hf.HiggsfieldError("no id", code="invalid_response", status=200), None]
    r = _animate(client, engine="seedance")
    assert r.status_code == 200, r.text
    (_, p1, k1), (_, p2, k2) = client.state_["submitted"]
    assert p1 == p2 and k1 == k2


@pytest.mark.parametrize("code", ["upstream", "network"])
def test_every_unknown_outcome_says_to_check_the_dashboard(client, code):
    client.state_["submit_script"] = [hf.HiggsfieldError("generic text", code=code)] * 3
    r = _animate(client, engine="seedance")
    assert "dashboard" in r.json()["detail"] and "dashboard" in client.state_["rec"]["error"]
    assert client.state_["ledger"][0]["written_off"] is False


def test_a_stale_page_cannot_claim_a_still_that_moved_on(client):
    client.state_["rec"]["status"] = "ready"  # finished in another tab
    r = _animate(client, engine="seedance", expected_status="frame_ready")
    assert r.status_code == 409
    assert r.json()["detail"] == yp._GV_CHANGED
    st = client.state_
    assert st["claims"] == [] and st["ledger"] == [] and st["submitted"] == []


def test_expected_status_must_be_a_claimable_one(client):
    r = _animate(client, engine="seedance", expected_status="animating")
    assert r.status_code == 400
    assert client.state_["claims"] == []


def test_the_page_status_is_what_gets_claimed(client):
    assert _animate(client, engine="seedance", expected_status="frame_ready").status_code == 200
    assert client.state_["claims"] == ["seedance"]


def test_a_brief_database_error_after_acceptance_is_retried_not_resubmitted(client, monkeypatch):
    real_update = yp.gv_store.update
    calls = {"n": 0}

    async def flaky(gen_id, **fields):
        if fields.get("status") == "animating" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("pool timeout")
        return await real_update(gen_id, **fields)

    monkeypatch.setattr(yp.gv_store, "update", flaky)
    r = _animate(client, engine="seedance")
    assert r.status_code == 200, r.text
    assert len(client.state_["submitted"]) == 1
    assert client.state_["rec"]["status"] == "animating"
    assert client.state_["rec"]["seedance_request_id"] == "req-1"


def test_re_animating_with_veo_keeps_the_earlier_seedance_spend(client, monkeypatch):
    client.state_["rec"]["aspect_ratio"] = "9:16"
    assert _animate(client, engine="seedance", duration_sec=30, resolution="1080p").status_code == 200
    client.state_["rec"]["status"] = "ready"

    async def _veo(*a, **k):
        return {"duration_sec": 6, "with_audio": True, "aspect_ratio": "9:16",
                "model": "veo-x", "operation_name": "op-1", "cost_usd": 0.9}

    monkeypatch.setattr(yp.card_animation, "build_card_i2v_coro", _veo)
    r = _animate(client, engine="veo")
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "veo"
    assert r.json()["seedance_status_url"] is None  # the poller follows the Veo job now
    [row] = client.state_["ledger"]
    assert row["written_off"] is False and row["cost"] == pytest.approx(34.116, abs=0.001)


def test_a_failed_veo_submit_reopens_the_still(client, monkeypatch):
    client.state_["rec"]["aspect_ratio"] = "16:9"

    async def _boom(*a, **k):
        raise RuntimeError("Vertex said no")

    monkeypatch.setattr(yp.card_animation, "build_card_i2v_coro", _boom)
    r = _animate(client, engine="veo")
    assert r.status_code == 502
    assert client.state_["claims"] == ["veo"]
    assert client.state_["rec"]["status"] == "frame_ready"


def test_still_accepts_seedance_shapes_with_a_matching_directive():
    assert yp.GENERAL_VIDEO_STILL_ASPECTS >= {"9:16", "16:9", "1:1", "4:3", "3:4", "21:9"}
    assert yp._gv_aspect_note("9:16") == yp.card_animation.aspect_render_note("9:16")
    assert "SQUARE" in yp._gv_aspect_note("1:1")
    assert "21:9" in yp._gv_aspect_note("21:9")


# --------------------------------------------------------------------------- #
# Poller
# --------------------------------------------------------------------------- #

_REAL_CLIENT = httpx.AsyncClient


def _seedance_row(gen_id=5, **over):
    row = {"id": gen_id, "status": "animating", "engine": "seedance",
           "seedance_request_id": f"req-{gen_id}",
           "seedance_status_url": f"https://api.example/requests/req-{gen_id}/status",
           "seedance_submitted_at": "2026-10-07T10:00:00+00:00",
           "veo_operation_name": None}
    row.update(over)
    return row


@pytest.fixture
def tick(monkeypatch):
    """Run one poller tick over `rows`, with Higgsfield's status answer per row
    and the video download faked. Returns what the tick wrote."""
    out = {"updates": [], "write_offs": [], "status_calls": [], "swept": 0}

    async def _update(gen_id, **fields):
        out["updates"].append((gen_id, fields))
        return fields

    async def _write_off(*, spend_id=None, request_id=None):
        out["write_offs"].append(request_id)

    async def _sweep(**k):
        out["swept"] += 1
        return 0

    async def _veo_poll(name):  # must never be reached for a Seedance row
        raise AssertionError("veo_poll called for a Seedance clip")

    def run(rows, answers, *, download=lambda req: httpx.Response(200, content=b"mp4bytes"),
            connected=True, overdue=False):
        async def _status(url):
            out["status_calls"].append(url)
            answer = answers[url]
            if isinstance(answer, Exception):
                raise answer
            return hf.map_status(answer)

        class _Client(_REAL_CLIENT):
            def __init__(self, **kw):
                kw["transport"] = httpx.MockTransport(download)
                super().__init__(**kw)

        monkeypatch.setattr(poller.store, "pending", lambda: _async(rows))
        monkeypatch.setattr(poller.store, "update", _update)
        monkeypatch.setattr(poller.store, "write_off_seedance_spend", _write_off)
        monkeypatch.setattr(poller.store, "fail_stale_claims", _sweep)
        monkeypatch.setattr(poller.higgsfield, "connected", lambda: connected)
        monkeypatch.setattr(poller.higgsfield, "get_status", _status)
        monkeypatch.setattr(poller.httpx, "AsyncClient", _Client)
        monkeypatch.setattr(poller, "veo_poll", _veo_poll)
        monkeypatch.setattr(poller, "_seedance_overdue", lambda r: overdue)
        monkeypatch.setattr(poller.asset_storage, "upload",
                            lambda **k: _async({"storage_bucket": "b", "storage_path": "p", "local_path": None}))
        asyncio.run(poller._poll_once())
        return out

    return run


def _url(gen_id=5):
    return f"https://api.example/requests/req-{gen_id}/status"


def test_poller_stores_a_finished_seedance_clip(tick):
    out = tick([_seedance_row()], {_url(): {"status": "completed", "video": {"url": "https://cdn/v.mp4"}}})
    gen_id, fields = out["updates"][-1]
    assert (gen_id, fields["status"], fields["video_storage_path"], fields["bytes"]) == (5, "ready", "p", 8)
    assert out["swept"] == 1  # the stale-claim sweep runs every tick


def test_poller_leaves_an_open_seedance_job_alone(tick):
    out = tick([_seedance_row()], {_url(): {"status": "in_progress"}})
    assert out["updates"] == [] and out["write_offs"] == []


@pytest.mark.parametrize("status, needle", [
    ("nsfw", "unsafe"), ("failed", "could not make"), ("canceled", "cancelled"),
])
def test_poller_writes_off_a_seedance_clip_that_was_not_charged(tick, status, needle):
    out = tick([_seedance_row()], {_url(): {"status": status}})
    _, fields = out["updates"][-1]
    assert fields["status"] == "failed"
    assert needle in fields["error"] and "Nothing was charged" in fields["error"]
    assert fields["seedance_cost_usd"] == 0
    assert out["write_offs"] == ["req-5"]


def test_poller_retries_a_paid_video_it_could_not_download(tick):
    out = tick([_seedance_row()], {_url(): {"status": "completed", "video": {"url": "https://cdn/v.mp4"}}},
               download=lambda req: httpx.Response(503))
    assert out["updates"] == []  # still animating: tried again next tick


def test_poller_gives_up_on_a_download_only_after_the_deadline(tick):
    out = tick([_seedance_row()], {_url(): {"status": "completed", "video": {"url": "https://cdn/v.mp4"}}},
               download=lambda req: httpx.Response(503), overdue=True)
    _, fields = out["updates"][-1]
    assert fields["status"] == "failed" and "could not be saved" in fields["error"]
    assert "seedance_cost_usd" not in fields  # it was charged; keep counting it


def test_poller_parks_seedance_clips_without_keys_and_says_so(tick, caplog, monkeypatch):
    monkeypatch.setattr(poller, "_missing_keys_warned_at", None)
    with caplog.at_level(logging.WARNING, logger=poller.log.name):
        out = tick([_seedance_row(), _seedance_row(6)], {}, connected=False)
    assert out["status_calls"] == [] and out["updates"] == []
    assert any("HIGGSFIELD_API_KEY_ID" in m and "5, 6" in m for m in caplog.messages)


def test_one_clip_failing_does_not_stop_the_others(tick):
    out = tick(
        [_seedance_row(5), _seedance_row(6)],
        {_url(5): RuntimeError("database hiccup"),
         _url(6): {"status": "completed", "video": {"url": "https://cdn/v.mp4"}}},
    )
    assert out["status_calls"] == [_url(5), _url(6)]
    assert out["updates"][-1][0] == 6 and out["updates"][-1][1]["status"] == "ready"


def test_overdue_uses_the_submit_time():
    old = _seedance_row(seedance_submitted_at="2026-10-01T00:00:00+00:00")
    assert poller._seedance_overdue(old) is True
    fresh = _seedance_row(seedance_submitted_at="2999-01-01T00:00:00+00:00")
    assert poller._seedance_overdue(fresh) is False
