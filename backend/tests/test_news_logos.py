"""Tests for ``app/services/news_logos.py`` - the logo on a card about a company.

Every test is offline. ``find_icon_url``, ``host_of``, ``png_width`` and
``looks_like_a_logo`` are pure and driven with fixtures; everything that would
reach the network goes through an ``httpx.MockTransport``, and the store is
pointed at ``tmp_path``.

THE ONE THAT MATTERS MOST is ``test_the_default_globe_is_never_a_logo``. The
public favicon lookup answers an unknown domain with a tiny generic globe, and
a fetcher that saved it would put a grey circle on a card and call it the
company's mark, which is the exact "generic logo everywhere" this module
exists to end.
"""
from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import news_logos as nl  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def png(width: int, *, size: int = 4096) -> bytes:
    """A byte string that starts like a PNG of this width and is `size` long."""
    head = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, width) + b"\x08\x06\x00\x00\x00" + b"\0\0\0\0")
    return head + b"\0" * max(0, size - len(head))


PAGE = "https://openai.com/"


# ---------------------------------------------------------------------------
# find_icon_url - pure
# ---------------------------------------------------------------------------

def test_an_apple_touch_icon_beats_an_unsized_favicon():
    html = ('<link rel="icon" href="/favicon.png">'
            '<link rel="apple-touch-icon" href="/apple.png">')
    assert nl.find_icon_url(html, PAGE) == "https://openai.com/apple.png"


def test_the_largest_declared_icon_wins_whatever_its_rel():
    html = ('<link rel="apple-touch-icon" sizes="180x180" href="/apple.png">'
            '<link rel="icon" type="image/png" sizes="512x512" href="/big.png">')
    assert nl.find_icon_url(html, PAGE) == "https://openai.com/big.png"


def test_the_attributes_are_read_in_either_order():
    html = '<link href="/a.png" sizes="192x192" rel="icon">'
    assert nl.find_icon_url(html, PAGE) == "https://openai.com/a.png"


@pytest.mark.parametrize("href", ["/mark.svg", "/favicon.ico", "data:image/png;base64,AAAA"])
def test_an_icon_we_cannot_show_as_a_picture_is_skipped(href):
    """An SVG cannot be size-checked or served as a plain picture, an ICO is
    the 16px favicon, and a data URI is not an address."""
    assert nl.find_icon_url(f'<link rel="icon" href="{href}">', PAGE) is None


def test_a_relative_href_is_resolved_against_the_page():
    html = '<link rel="apple-touch-icon" href="static/icons/apple.png">'
    assert nl.find_icon_url(html, "https://www.example.com/home/") == \
        "https://www.example.com/home/static/icons/apple.png"


def test_a_page_with_no_icon_returns_none():
    assert nl.find_icon_url('<link rel="stylesheet" href="/a.css">', PAGE) is None
    assert nl.find_icon_url("", PAGE) is None


def test_a_mask_icon_is_not_an_icon():
    """`rel="mask-icon"` is Safari's monochrome SVG; the token is not "icon"."""
    assert nl.find_icon_url('<link rel="mask-icon" href="/m.png">', PAGE) is None


# ---------------------------------------------------------------------------
# host_of - pure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("openai.com", "openai.com"),
    ("https://www.OpenAI.com/blog/x", "openai.com"),
    ("Anthropic.com", "anthropic.com"),
    ("http://news.mit.edu:443/a", "news.mit.edu"),
    ("I am not sure", ""),
    ("", ""),
    (None, ""),
    ("nodots", ""),
])
def test_the_judges_company_site_becomes_a_bare_host_or_nothing(raw, expected):
    assert nl.host_of(raw) == expected


# ---------------------------------------------------------------------------
# The size floors - pure
# ---------------------------------------------------------------------------

def test_a_pngs_width_is_read_off_its_header():
    assert nl.png_width(png(256)) == 256
    assert nl.png_width(png(16)) == 16


def test_bytes_that_are_not_a_png_have_no_width():
    assert nl.png_width(b"\xff\xd8\xff" + b"x" * 40) is None
    assert nl.png_width(b"") is None


def test_the_default_globe_is_never_a_logo():
    """Tiny in bytes AND tiny in pixels: both floors reject it."""
    assert not nl.looks_like_a_logo(png(16, size=600))
    assert not nl.looks_like_a_logo(png(16, size=4096))


def test_a_real_mark_is_a_logo():
    assert nl.looks_like_a_logo(png(180))
    assert nl.looks_like_a_logo(png(256))


def test_a_flat_mark_that_weighs_almost_nothing_is_still_a_logo():
    """Microsoft's four squares are 426 bytes at 128 pixels. Measured on the
    live fetch, and a byte floor threw it away. A PNG is judged on its width."""
    assert nl.looks_like_a_logo(png(128, size=426))
    assert not nl.looks_like_a_logo(png(16, size=426))


def test_a_jpeg_is_judged_on_bytes_alone():
    assert nl.looks_like_a_logo(b"\xff\xd8\xff" + b"j" * 2000)
    assert not nl.looks_like_a_logo(b"\xff\xd8\xff" + b"j" * 200)


# ---------------------------------------------------------------------------
# fetch_logo - the network shapes, all mocked
# ---------------------------------------------------------------------------

def _sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(nl, "asset_local_path",
                        lambda kind, h, ext: tmp_path / f"{kind}-{h}.{ext}")
    monkeypatch.setattr(nl, "relativise", lambda p: str(p))


def _http(monkeypatch, handler):
    real = httpx.AsyncClient

    class _Client(real):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    monkeypatch.setattr(nl.httpx, "AsyncClient", _Client)


def _site_with_icon(calls: list):
    """openai.com declares an apple-touch-icon; the fallback must not be asked."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "www.google.com":
            raise AssertionError("the fallback was asked although the site answered")
        if request.url.path == "/apple.png":
            return httpx.Response(200, content=png(180),
                                  headers={"content-type": "image/png"})
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            text='<link rel="apple-touch-icon" sizes="180x180" href="/apple.png">')
    return handler


def test_the_sites_own_icon_is_fetched_and_filed_under_the_logo_type(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    calls: list = []
    _http(monkeypatch, _site_with_icon(calls))

    rel = _run(nl.fetch_logo("https://www.OpenAI.com/"))

    assert rel and rel.endswith(".png")
    assert nl.LOGO_ASSET_TYPE in rel, "the path is the record of what kind of cover this is"
    assert Path(rel).read_bytes() == png(180)
    assert calls == ["https://openai.com/", "https://openai.com/apple.png"]


def test_the_second_story_about_the_same_company_costs_no_request(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    calls: list = []
    _http(monkeypatch, _site_with_icon(calls))

    first = _run(nl.fetch_logo("openai.com"))
    second = _run(nl.fetch_logo("OpenAI.com"))

    assert first == second
    assert len(calls) == 2, "one page and one image, once"


def test_a_site_that_blocks_bots_is_answered_by_the_fallback(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.google.com":
            assert request.url.params["domain"] == "microsoft.com"
            return httpx.Response(200, content=png(256),
                                  headers={"content-type": "image/png"})
        return httpx.Response(403, text="no")

    _http(monkeypatch, handler)
    rel = _run(nl.fetch_logo("microsoft.com"))
    assert rel and Path(rel).read_bytes() == png(256)


def test_a_fallback_that_answers_with_the_default_globe_is_nothing(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.google.com":
            return httpx.Response(200, content=png(16, size=700),
                                  headers={"content-type": "image/png"})
        return httpx.Response(404, text="gone")

    _http(monkeypatch, handler)
    assert _run(nl.fetch_logo("nobody-here.example")) is None
    assert not list(tmp_path.iterdir()), "nothing was written"


def test_a_site_whose_icon_is_too_small_falls_through_to_the_fallback(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.google.com":
            return httpx.Response(200, content=png(256),
                                  headers={"content-type": "image/png"})
        if request.url.path == "/tiny.png":
            return httpx.Response(200, content=png(32),
                                  headers={"content-type": "image/png"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text='<link rel="icon" sizes="32x32" href="/tiny.png">')

    _http(monkeypatch, handler)
    rel = _run(nl.fetch_logo("example.com"))
    assert rel and Path(rel).read_bytes() == png(256)


def test_a_consent_wall_serving_html_as_the_icon_is_refused(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.google.com":
            return httpx.Response(404)
        if request.url.path == "/apple.png":
            return httpx.Response(200, text="<html>consent</html>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text='<link rel="apple-touch-icon" href="/apple.png">')

    _http(monkeypatch, handler)
    assert _run(nl.fetch_logo("example.com")) is None


def test_a_timeout_is_a_missing_logo_not_an_error(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _http(monkeypatch, handler)
    assert _run(nl.fetch_logo("example.com")) is None


@pytest.mark.parametrize("raw", ["", None, "not a website", "I am not sure"])
def test_something_that_is_not_a_host_never_opens_a_client(monkeypatch, tmp_path, raw):
    _sandbox(monkeypatch, tmp_path)
    _http(monkeypatch, lambda r: (_ for _ in ()).throw(AssertionError("fetched")))
    assert _run(nl.fetch_logo(raw)) is None


def test_the_saved_file_is_named_for_what_it_actually_is(monkeypatch, tmp_path):
    """A JPEG mark lands as .jpg, because the route reads the type off the name."""
    _sandbox(monkeypatch, tmp_path)
    jpeg = b"\xff\xd8\xff" + b"j" * 3000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/apple.png":
            return httpx.Response(200, content=jpeg,
                                  headers={"content-type": "image/jpeg"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text='<link rel="apple-touch-icon" href="/apple.png">')

    _http(monkeypatch, handler)
    rel = _run(nl.fetch_logo("example.com"))
    assert rel and rel.endswith(".jpg")
