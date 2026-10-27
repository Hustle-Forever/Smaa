"""Offline tests for the Higgsfield Seedance 2.5 client.

Every network shape is mocked through httpx.MockTransport (same trick as
test_news_thumbs.py), so these cover the parts that would otherwise only be
found out with a paid request: payloads that respect each route's closed
schema, the error mapping, "never retry a submit", "retry a status GET", and
the two-step upload.
"""
from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

from app.services import higgsfield_client as hf


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "kid")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "sec")
    monkeypatch.setattr(hf, "_RETRY_BACKOFF_SEC", 0.0)


@pytest.fixture
def disconnected(monkeypatch):
    monkeypatch.delenv("HIGGSFIELD_API_KEY_ID", raising=False)
    monkeypatch.delenv("HIGGSFIELD_API_KEY_SECRET", raising=False)
    monkeypatch.setattr(hf, "_config_creds", lambda: (None, None))


_REAL_CLIENT = httpx.AsyncClient


def _http(monkeypatch, handler):
    """Route every AsyncClient the module builds through `handler`. No network.
    Subclasses the ORIGINAL client (captured at import), so calling this twice
    in one test swaps the handler instead of stacking two mocks."""
    real = _REAL_CLIENT

    class _Client(real):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    monkeypatch.setattr(hf.httpx, "AsyncClient", _Client)


# ---------------------------------------------------------------------------
# build_payload: one closed schema per route
# ---------------------------------------------------------------------------

def test_reference_to_video_keeps_images_and_drops_output_format():
    p = hf.build_payload(
        hf.ROUTE_REFERENCE, prompt="hi", duration=8, resolution="720p", aspect_ratio="16:9",
        generate_audio=True, image_urls=["https://x/a.png"], output_format="mp4", video_urls=[],
    )
    assert p["image_urls"] == ["https://x/a.png"]
    assert "output_format" not in p
    assert "video_urls" not in p
    assert p["duration"] == 8


def test_text_to_video_drops_image_urls_and_keeps_output_format():
    p = hf.build_payload(hf.ROUTE_TEXT, prompt="hi", image_urls=["https://x/a.png"], output_format="mov")
    assert "image_urls" not in p
    assert p["output_format"] == "mov"


def test_image_to_video_drops_aspect_ratio():
    p = hf.build_payload(hf.ROUTE_IMAGE, image_url="https://x/a.png", aspect_ratio="9:16", duration=5)
    assert "aspect_ratio" not in p
    assert p["image_url"] == "https://x/a.png"


def test_none_and_empty_values_are_dropped():
    p = hf.build_payload(hf.ROUTE_TEXT, prompt="hi", duration=None, generate_audio=None, image_urls=[])
    assert p == {"prompt": "hi"}


@pytest.mark.parametrize("field,value,needle", [
    ("duration", 3, "duration"),
    ("duration", 31, "duration"),
    ("duration", 5.5, "duration"),
    ("duration", True, "duration"),
    ("resolution", "4k", "resolution"),
    ("aspect_ratio", "2:1", "aspect_ratio"),
    ("bitrate_mode", "ultra", "bitrate_mode"),
    ("output_format", "avi", "output_format"),
    ("generate_audio", "yes", "generate_audio"),
])
def test_bad_values_raise_naming_the_field(field, value, needle):
    with pytest.raises(ValueError) as e:
        hf.build_payload(hf.ROUTE_TEXT, prompt="hi", **{field: value})
    assert needle in str(e.value)


def test_prompt_required_for_text_route_but_not_reference_route():
    with pytest.raises(ValueError):
        hf.build_payload(hf.ROUTE_TEXT)
    p = hf.build_payload(hf.ROUTE_REFERENCE, image_urls=["https://x/a.png"])
    assert "prompt" not in p


def test_reference_route_needs_at_least_one_url_list():
    with pytest.raises(ValueError) as e:
        hf.build_payload(hf.ROUTE_REFERENCE, prompt="hi")
    assert "image_urls" in str(e.value)


def test_more_than_thirty_images_is_refused():
    with pytest.raises(ValueError):
        hf.build_payload(hf.ROUTE_REFERENCE, image_urls=[f"https://x/{i}.png" for i in range(31)])


def test_edit_and_extend_need_video_url():
    with pytest.raises(ValueError):
        hf.build_payload(hf.ROUTE_EDIT, prompt="hi")
    p = hf.build_payload(hf.ROUTE_EXTEND, prompt="hi", video_url="https://x/v.mp4", duration=10)
    assert p["video_url"] == "https://x/v.mp4"


def test_unknown_route_is_refused():
    with pytest.raises(ValueError):
        hf.build_payload("image-to-gif", prompt="hi")


def test_route_for():
    assert hf.route_for([]) == hf.ROUTE_TEXT
    assert hf.route_for(["https://x/a.png"]) == hf.ROUTE_REFERENCE


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

def test_local_estimate_list_price_and_promo():
    on = hf.estimate_local_usd(10, "720p", on=date(2026, 9, 30))
    assert on["usd_list"] == pytest.approx(4.622)
    assert on["promo_active"] is True
    assert on["usd_promo"] == pytest.approx(4.622 * 0.7)
    off = hf.estimate_local_usd(10, "720p", on=date(2026, 10, 1))
    assert off["promo_active"] is False and off["usd_promo"] is None


def test_local_estimate_rejects_bad_inputs():
    with pytest.raises(ValueError):
        hf.estimate_local_usd(10, "4k")
    with pytest.raises(ValueError):
        hf.estimate_local_usd(0, "720p")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_connected_false_without_keys(disconnected):
    assert hf.connected() is False


def test_connected_true_with_env_keys(connected):
    assert hf.connected() is True


def test_combined_id_colon_secret_is_accepted_in_either_variable(monkeypatch):
    """Higgsfield's dashboard shows the credential as one "id:secret" string,
    which is what gets pasted. The header must still read "Key id:secret"."""
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "kid")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "kid:sec")
    assert hf._creds() == ("kid", "sec")
    # An empty variable falls back to the config (which may hold real keys on
    # a dev machine), so the one-variable forms are checked on the splitter.
    assert hf.split_combined_key("", "kid:sec") == ("kid", "sec")
    assert hf.split_combined_key("kid:sec", "") == ("kid", "sec")
    assert hf.split_combined_key("kid", "sec") == ("kid", "sec")
    assert hf.split_combined_key("kid", "other:sec") == ("kid", "other:sec")   # a colon inside a real secret stays
    assert "backend/.env" not in hf.NOT_CONNECTED_MESSAGE


def test_submit_without_keys_raises_not_connected(disconnected):
    with pytest.raises(hf.HiggsfieldNotConnected) as e:
        _run(hf.submit(hf.ROUTE_TEXT, {"prompt": "hi"}))
    assert e.value.code == "not_connected"
    assert "HIGGSFIELD_API_KEY_ID" in str(e.value)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def test_submit_sends_key_header_and_returns_ids(connected, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "status": "queued", "request_id": "abc-123",
            "status_url": "https://api.higgsfield.ai/requests/abc-123/status",
            "cancel_url": "https://api.higgsfield.ai/requests/abc-123/cancel",
        }, headers={"x-correlation-id": "cid-1"})

    _http(monkeypatch, handler)
    out = _run(hf.submit(hf.ROUTE_REFERENCE, {"prompt": "hi", "image_urls": ["https://x/a.png"]}))
    assert seen["auth"] == "Key kid:sec"
    assert seen["path"] == "/bytedance/seedance-2.5/reference-to-video"
    assert b'"image_urls"' in seen["body"]
    assert out["request_id"] == "abc-123"
    assert out["status_url"].endswith("/requests/abc-123/status")
    assert out["cancel_url"].endswith("/requests/abc-123/cancel")
    assert out["correlation_id"] == "cid-1"


def test_submit_sends_the_idempotency_key_only_when_given(connected, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("idempotency-key"))
        return httpx.Response(200, json={"status": "queued", "request_id": "abc-123"})

    _http(monkeypatch, handler)
    _run(hf.submit(hf.ROUTE_TEXT, {"prompt": "hi"}, idempotency_key="k-1"))
    _run(hf.submit(hf.ROUTE_TEXT, {"prompt": "hi"}))
    assert seen == ["k-1", None]


def test_submit_maps_concurrency_400(connected, monkeypatch):
    _http(monkeypatch, lambda r: httpx.Response(
        400, json={"detail": "Maximum number of concurrent requests (4) has been reached"}))
    with pytest.raises(hf.HiggsfieldError) as e:
        _run(hf.submit(hf.ROUTE_TEXT, {"prompt": "hi"}))
    assert e.value.code == "concurrency"
    assert e.value.retryable is False


@pytest.mark.parametrize("status,code,retryable", [
    (401, "auth", False), (403, "credits", False), (404, "not_found", False),
    (422, "invalid", False), (423, "blocked", False), (500, "upstream", True), (503, "upstream", True),
])
def test_submit_error_mapping(connected, monkeypatch, status, code, retryable):
    _http(monkeypatch, lambda r: httpx.Response(status, json={"detail": "nope"}))
    with pytest.raises(hf.HiggsfieldError) as e:
        _run(hf.submit(hf.ROUTE_TEXT, {"prompt": "hi"}))
    assert e.value.code == code
    assert e.value.retryable is retryable
    assert e.value.status == status


def test_submit_is_never_retried(connected, monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"detail": "boom"})

    _http(monkeypatch, handler)
    with pytest.raises(hf.HiggsfieldError):
        _run(hf.submit(hf.ROUTE_TEXT, {"prompt": "hi"}))
    assert calls["n"] == 1


def test_submit_timeout_is_reported_as_unknown_outcome(connected, monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    _http(monkeypatch, handler)
    with pytest.raises(hf.HiggsfieldError) as e:
        _run(hf.submit(hf.ROUTE_TEXT, {"prompt": "hi"}))
    assert e.value.code == "timeout"
    assert "dashboard" in str(e.value)


def test_submit_without_request_id_is_an_invalid_response(connected, monkeypatch):
    _http(monkeypatch, lambda r: httpx.Response(200, json={"status": "queued"}))
    with pytest.raises(hf.HiggsfieldError) as e:
        _run(hf.submit(hf.ROUTE_TEXT, {"prompt": "hi"}))
    assert e.value.code == "invalid_response"


# ---------------------------------------------------------------------------
# status / cancel / estimate
# ---------------------------------------------------------------------------

def test_get_status_retries_a_503_then_succeeds(connected, monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"detail": "warming up"})
        return httpx.Response(200, json={
            "status": "completed", "request_id": "abc", "video": {"url": "https://cdn/x.mp4"},
        })

    _http(monkeypatch, handler)
    out = _run(hf.get_status("https://api.higgsfield.ai/requests/abc/status"))
    assert calls["n"] == 2
    assert out["status"] == "completed" and out["terminal"] is True
    assert out["video_url"] == "https://cdn/x.mp4"


def test_get_status_stops_at_once_on_404(connected, monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"detail": "unknown"})

    _http(monkeypatch, handler)
    with pytest.raises(hf.HiggsfieldError) as e:
        _run(hf.get_status("https://api.higgsfield.ai/requests/abc/status"))
    assert e.value.code == "not_found"
    assert calls["n"] == 1


def test_get_status_gives_up_after_repeated_server_errors(connected, monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"detail": "boom"})

    _http(monkeypatch, handler)
    with pytest.raises(hf.HiggsfieldError) as e:
        _run(hf.get_status("https://api.higgsfield.ai/requests/abc/status"))
    assert e.value.retryable is True
    assert calls["n"] == hf._STATUS_ATTEMPTS


@pytest.mark.parametrize("body,status,terminal,url,error", [
    ({"status": "queued", "request_id": "a"}, "queued", False, None, None),
    ({"status": "in_progress", "request_id": "a"}, "in_progress", False, None, None),
    ({"status": "completed", "video": {"url": "https://cdn/v.mp4"}}, "completed", True, "https://cdn/v.mp4", None),
    ({"status": "failed", "error": "Generation failed"}, "failed", True, None, "Generation failed"),
    ({"status": "nsfw"}, "nsfw", True, None, None),
    ({"status": "canceled"}, "canceled", True, None, None),
    # webhook envelope
    ({"status": "completed", "error": None, "payload": {"video": {"url": "https://cdn/w.mp4", "content_type": "video/mp4"}}},
     "completed", True, "https://cdn/w.mp4", None),
])
def test_map_status_shapes(body, status, terminal, url, error):
    out = hf.map_status(body)
    assert out["status"] == status
    assert out["terminal"] is terminal
    assert out["video_url"] == url
    assert out["error"] == error


def test_cancel_true_on_202_false_on_400(connected, monkeypatch):
    _http(monkeypatch, lambda r: httpx.Response(202))
    assert _run(hf.cancel("https://api.higgsfield.ai/requests/a/cancel")) is True
    _http(monkeypatch, lambda r: httpx.Response(400, json={"detail": "already started"}))
    assert _run(hf.cancel("https://api.higgsfield.ai/requests/a/cancel")) is False


def test_estimate_parses_usd_string(connected, monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={"credits": "1.500", "usd": "0.094"})

    _http(monkeypatch, handler)
    out = _run(hf.estimate(hf.ROUTE_REFERENCE, {"prompt": "hi", "image_urls": ["https://x/a.png"]}))
    assert seen["path"] == "/estimate/bytedance/seedance-2.5/reference-to-video"
    assert out["usd"] == pytest.approx(0.094)
    assert out["credits"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------

def test_upload_file_two_steps(connected, monkeypatch):
    seen = {}

    def handler(request):
        if request.method == "POST":
            seen["post_body"] = request.read()
            seen["post_auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={
                "public_url": "https://cdn/input/x.png",
                "upload_url": "https://storage/presigned",
                "content_type": "image/png",
                "upload_headers": {"Content-Type": "image/png", "x-amz-tagging": "retention=temporary"},
            })
        seen["put_url"] = str(request.url)
        seen["put_headers"] = dict(request.headers)
        seen["put_body"] = request.read()
        return httpx.Response(200)

    _http(monkeypatch, handler)
    url = _run(hf.upload_file(b"\x89PNGdata", "image/png"))
    assert url == "https://cdn/input/x.png"
    assert b'"image/png"' in seen["post_body"]
    assert seen["post_auth"] == "Key kid:sec"
    assert seen["put_url"] == "https://storage/presigned"
    assert seen["put_headers"]["x-amz-tagging"] == "retention=temporary"
    assert seen["put_headers"]["content-type"] == "image/png"
    assert "authorization" not in seen["put_headers"]
    assert seen["put_body"] == b"\x89PNGdata"


def test_upload_file_rejects_unsupported_type(connected):
    with pytest.raises(ValueError):
        _run(hf.upload_file(b"x", "image/bmp"))


def test_upload_file_reports_a_failed_put(connected, monkeypatch):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"public_url": "https://cdn/p", "upload_url": "https://s/u", "upload_headers": {}})
        return httpx.Response(403, text="denied")

    _http(monkeypatch, handler)
    with pytest.raises(hf.HiggsfieldError) as e:
        _run(hf.upload_file(b"x", "image/jpeg"))
    assert e.value.code == "upload_failed"
