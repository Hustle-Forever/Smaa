"""Tests for ``app/services/news_thumbs.py`` - the picture on a news card.

Every test is offline. ``find_image_url`` is pure and driven with fixture HTML;
everything that would reach the network goes through an ``httpx.MockTransport``,
and the two disk helpers are pointed at ``tmp_path``.

THE ONE THAT MATTERS MOST is ``test_an_error_page_never_becomes_a_cover``. It is
not a hypothetical: on the live feed, rockefeller.org answers a dead article URL
with HTTP 404 and a full page that carries ``og:title = "404 PAGE NOT FOUND"``
and a generic ``og:image``. A fetcher that read the body without checking the
status would have put a stock photo of a nebula on the card and called it the
story's cover. The status check is the whole guard, so it has a test.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import news_logos as nl  # noqa: E402
from app.services import news_thumbs as nt  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# find_image_url - pure, no network
# ---------------------------------------------------------------------------

PAGE = "https://news.example/section/a-story"


def test_an_absolute_og_image_is_taken_as_is():
    html = '<meta property="og:image" content="https://cdn.example/a.jpg">'
    assert nt.find_image_url(html, PAGE) == "https://cdn.example/a.jpg"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/img/a.jpg", "https://news.example/img/a.jpg"),          # root-relative
        ("a.jpg", "https://news.example/section/a.jpg"),           # fully relative
        ("//cdn.example/a.jpg", "https://cdn.example/a.jpg"),      # protocol-relative
    ],
)
def test_a_relative_og_image_is_resolved_against_the_page(raw, expected):
    """All three shapes are in the wild, and a relative URL saved unresolved is
    a download that 404s on our own host."""
    assert nt.find_image_url(f'<meta property="og:image" content="{raw}">', PAGE) == expected


def test_the_attributes_are_read_in_either_order():
    """Some CMSes emit content before property. One regex cannot match both."""
    html = '<meta content="https://cdn.example/b.jpg" property="og:image" />'
    assert nt.find_image_url(html, PAGE) == "https://cdn.example/b.jpg"


def test_og_image_url_and_secure_url_are_the_same_tag():
    for prop in ("og:image:url", "og:image:secure_url"):
        html = f'<meta property="{prop}" content="https://cdn.example/c.jpg">'
        assert nt.find_image_url(html, PAGE) == "https://cdn.example/c.jpg"


def test_twitter_image_is_the_second_choice():
    html = '<meta name="twitter:image" content="https://cdn.example/t.jpg">'
    assert nt.find_image_url(html, PAGE) == "https://cdn.example/t.jpg"


def test_link_rel_image_src_is_the_last_choice():
    html = '<link rel="image_src" href="https://cdn.example/old.jpg">'
    assert nt.find_image_url(html, PAGE) == "https://cdn.example/old.jpg"


def test_og_image_wins_over_twitter_image():
    """Order is a decision: og:image is the one publishers actually maintain."""
    html = (
        '<meta name="twitter:image" content="https://cdn.example/t.jpg">'
        '<meta property="og:image" content="https://cdn.example/og.jpg">'
    )
    assert nt.find_image_url(html, PAGE) == "https://cdn.example/og.jpg"


def test_a_page_with_no_image_returns_none():
    assert nt.find_image_url("<html><head><title>x</title></head></html>", PAGE) is None


@pytest.mark.parametrize(
    "raw",
    [
        "data:image/png;base64,AAAA",   # inlined placeholder, not a photo
        "/brand/logo.svg",              # a logo on a 4/5 tile looks worse than the tint
        "javascript:alert(1)",          # not a scheme we will ever fetch
        "",
    ],
)
def test_an_image_we_will_not_use_returns_none(raw):
    assert nt.find_image_url(f'<meta property="og:image" content="{raw}">', PAGE) is None


def test_a_later_usable_tag_is_taken_when_the_first_is_unusable():
    """A data: og:image must not shadow a real twitter:image below it."""
    html = (
        '<meta property="og:image" content="data:image/gif;base64,AA">'
        '<meta name="twitter:image" content="https://cdn.example/real.jpg">'
    )
    assert nt.find_image_url(html, PAGE) == "https://cdn.example/real.jpg"


# ---------------------------------------------------------------------------
# fetch_cover - the network shapes, all mocked
# ---------------------------------------------------------------------------

def _sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(nt, "asset_local_path",
                        lambda kind, h, ext: tmp_path / f"{h}.{ext}")
    monkeypatch.setattr(nt, "relativise", lambda p: str(p))
    # The logo store too: a cover pass asks for a publisher's logo when the
    # publisher gives no picture, and that must never touch the real asset root.
    monkeypatch.setattr(nl, "asset_local_path",
                        lambda kind, h, ext: tmp_path / f"{kind}-{h}.{ext}")
    monkeypatch.setattr(nl, "relativise", lambda p: str(p))


def _http(monkeypatch, handler):
    """Route every AsyncClient this module builds through `handler`. No network."""
    real = httpx.AsyncClient

    class _Client(real):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    monkeypatch.setattr(nt.httpx, "AsyncClient", _Client)


JPEG = b"\xff\xd8\xff" + b"jpegbytes" * 100  # comfortably past the 512-byte floor


def _page_then_image(html: str, *, page_status: int = 200,
                     image_type: str = "image/jpeg", image: bytes = JPEG):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith((".jpg", ".png")):
            return httpx.Response(200, content=image,
                                  headers={"content-type": image_type})
        return httpx.Response(page_status, text=html,
                              headers={"content-type": "text/html; charset=utf-8"})
    return handler


def test_a_cover_is_downloaded_and_its_path_returned(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _http(monkeypatch, _page_then_image(
        '<meta property="og:image" content="https://cdn.example/a.jpg">'))

    rel = _run(nt.fetch_cover("https://news.example/story"))

    assert rel and Path(rel).read_bytes() == JPEG


def test_an_error_page_never_becomes_a_cover(monkeypatch, tmp_path):
    """A dead link whose 404 page still carries a generic og:image.

    Live example: rockefeller.org answers a removed article with 404 and a page
    advertising og:title "404 PAGE NOT FOUND" over a stock photo.
    """
    _sandbox(monkeypatch, tmp_path)
    _http(monkeypatch, _page_then_image(
        '<meta property="og:image" content="https://cdn.example/nebula.jpg">',
        page_status=404))

    assert _run(nt.fetch_cover("https://news.example/gone")) is None
    assert list(tmp_path.iterdir()) == []


def test_a_bot_wall_is_a_missing_picture_not_an_error(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _http(monkeypatch, lambda r: httpx.Response(403, text="go away"))
    assert _run(nt.fetch_cover("https://news.example/story")) is None


def test_a_timeout_is_a_missing_picture_not_an_error(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    _http(monkeypatch, boom)
    assert _run(nt.fetch_cover("https://news.example/story")) is None


def test_a_consent_wall_serving_html_as_the_image_is_refused(monkeypatch, tmp_path):
    """200 with text/html where a JPEG was promised. Writing that to a .jpg is
    how a card ends up with a broken-image glyph instead of a clean tile."""
    _sandbox(monkeypatch, tmp_path)
    _http(monkeypatch, _page_then_image(
        '<meta property="og:image" content="https://cdn.example/a.jpg">',
        image_type="text/html"))

    assert _run(nt.fetch_cover("https://news.example/story")) is None
    assert list(tmp_path.iterdir()) == []


def test_a_tracking_pixel_is_not_a_cover(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _http(monkeypatch, _page_then_image(
        '<meta property="og:image" content="https://cdn.example/a.jpg">',
        image=b"GIF89a" + b"\x00" * 20))

    assert _run(nt.fetch_cover("https://news.example/story")) is None


def test_a_page_with_no_image_stores_nothing(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _http(monkeypatch, _page_then_image("<html><head></head></html>"))
    assert _run(nt.fetch_cover("https://news.example/story")) is None


def test_an_empty_url_never_opens_a_client(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _http(monkeypatch, lambda r: (_ for _ in ()).throw(AssertionError("fetched")))
    assert _run(nt.fetch_cover("")) is None


def test_the_same_image_is_downloaded_once(monkeypatch, tmp_path):
    """Two stories can share a publisher's default social card, and a story
    re-found after a prune is the same URL again."""
    _sandbox(monkeypatch, tmp_path)
    html = '<meta property="og:image" content="https://cdn.example/shared.jpg">'
    _http(monkeypatch, _page_then_image(html))
    first = _run(nt.fetch_cover("https://news.example/one"))

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith(".jpg"):
            raise AssertionError("re-downloaded a cached image")
        return httpx.Response(200, text=html,
                              headers={"content-type": "text/html"})

    _http(monkeypatch, handler)
    second = _run(nt.fetch_cover("https://news.example/two"))

    assert first == second
    assert all(not c.endswith(".jpg") for c in calls)


# ---------------------------------------------------------------------------
# backfill - the batch, with the store stubbed
# ---------------------------------------------------------------------------

class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.saved: list[tuple[int, str]] = []

    async def news_without_thumb(self, limit=12):
        return self.rows[:limit]

    async def set_local_thumb(self, post_id, rel):
        self.saved.append((post_id, rel))


def _no_pool(monkeypatch, tmp_path):
    """Publisher covers only: an empty pool with no model behind it."""
    empty = tmp_path / "nopool"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(nt, "pool_dir", lambda: empty)

    async def _never(prompt, **kw):
        raise RuntimeError("no model in this test")
    monkeypatch.setattr(nt, "gemini_image", _never)


def test_backfill_records_only_the_covers_that_landed(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _no_pool(monkeypatch, tmp_path)
    store = _Store([
        {"id": 1, "permalink": "https://news.example/ok", "caption": "a"},
        {"id": 2, "permalink": "https://news.example/blocked", "caption": "b"},
        {"id": 3, "permalink": "", "caption": "c"},   # no link: never attempted
    ])
    monkeypatch.setattr(nt, "store", store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=JPEG,
                                  headers={"content-type": "image/jpeg"})
        if request.url.path == "/ok":
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text='<meta property="og:image" content="https://cdn.example/a.jpg">')
        return httpx.Response(403, text="no")

    _http(monkeypatch, handler)

    assert _run(nt.backfill()) == 1
    assert [pid for pid, _ in store.saved] == [1]


def test_backfill_with_nothing_to_do_makes_no_requests(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _no_pool(monkeypatch, tmp_path)
    monkeypatch.setattr(nt, "store", _Store([]))
    _http(monkeypatch, lambda r: (_ for _ in ()).throw(AssertionError("fetched")))
    assert _run(nt.backfill()) == 0


def test_a_store_that_cannot_be_read_is_not_an_error(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    class _Broken:
        async def news_without_thumb(self, limit=12):
            raise RuntimeError("db is down")

    monkeypatch.setattr(nt, "store", _Broken())
    assert _run(nt.backfill()) == 0


def test_one_row_that_cannot_be_recorded_does_not_lose_the_others(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)

    class _HalfBroken(_Store):
        async def set_local_thumb(self, post_id, rel):
            if post_id == 1:
                raise RuntimeError("write failed")
            self.saved.append((post_id, rel))

    store = _HalfBroken([
        {"id": 1, "permalink": "https://news.example/a", "caption": "a"},
        {"id": 2, "permalink": "https://news.example/b", "caption": "b"},
    ])
    monkeypatch.setattr(nt, "store", store)
    _no_pool(monkeypatch, tmp_path)
    _http(monkeypatch, _page_then_image(
        '<meta property="og:image" content="https://cdn.example/a.jpg">'))

    assert _run(nt.backfill()) == 1
    assert [pid for pid, _ in store.saved] == [2]


# ---------------------------------------------------------------------------
# The house-cover pool
# ---------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 200


def _pool_sandbox(monkeypatch, tmp_path):
    """Point both the writer and the reader at one empty directory."""
    root = tmp_path / "pool"
    root.mkdir()
    monkeypatch.setattr(nt, "asset_local_path",
                        lambda kind, h, ext: root / f"{h}.{ext}")
    monkeypatch.setattr(nt, "relativise", lambda p: str(p))
    monkeypatch.setattr(nt, "pool_dir", lambda: root)
    logos = tmp_path / "logos"
    logos.mkdir(exist_ok=True)
    monkeypatch.setattr(nl, "asset_local_path",
                        lambda kind, h, ext: logos / f"{kind}-{h}.{ext}")
    monkeypatch.setattr(nl, "relativise", lambda p: str(p))
    return root


def _draws(monkeypatch, data=PNG, calls=None):
    async def _gen(prompt, *, aspect_ratio="3:4", **kw):
        if calls is not None:
            calls.append((prompt, aspect_ratio))
        if isinstance(data, Exception):
            raise data
        return data
    monkeypatch.setattr(nt, "gemini_image", _gen)


def test_the_prompt_forbids_the_three_things_that_would_mislead():
    """A house card sits beside a real headline and a real link. It must never
    read as a photograph OF the story, must not compete with the headline, and
    must not put words or a face on somebody who never said them."""
    p = nt.cover_prompt("a desk with a laptop").lower()
    assert "never photographic" in p and "flat editorial illustration" in p
    assert "no text" in p and "no letters" in p
    assert "no real people" in p and "no company logos" in p


def test_no_brief_is_about_a_particular_story():
    """Generic is the whole point: a card drawn from one headline would look
    like a picture OF that story, and it is a guess made from a sentence."""
    words = " ".join(nt.COVER_BRIEFS).lower()
    for loaded in ("ai", "chatgpt", "openai", "google", "school district"):
        assert f" {loaded} " not in f" {words} "
    assert len(set(nt.COVER_BRIEFS)) == len(nt.COVER_BRIEFS) >= nt.COVER_POOL_SIZE


def test_filling_the_pool_draws_up_to_the_cap(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    calls = []
    _draws(monkeypatch, calls=calls)

    assert _run(nt.fill_pool(target=10, max_new=4)) == 4
    assert len(nt.pool()) == 4
    assert calls[0][1] == "3:4"          # portrait, because the tile is 4/5


def test_a_full_pool_costs_nothing(monkeypatch, tmp_path):
    """Called on every tick, so a full pool must be one directory listing."""
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch)
    _run(nt.fill_pool(target=3, max_new=3))

    _draws(monkeypatch, data=AssertionError("asked the model"))
    assert _run(nt.fill_pool(target=3, max_new=3)) == 0


def test_filling_resumes_where_it_stopped(monkeypatch, tmp_path):
    """Three ticks fill a pool of ten, and no brief is drawn twice."""
    _pool_sandbox(monkeypatch, tmp_path)
    calls = []
    _draws(monkeypatch, calls=calls)

    total = sum(_run(nt.fill_pool(target=10, max_new=4)) for _ in range(3))

    assert total == 10 and len(nt.pool()) == 10
    assert len({c[0] for c in calls}) == 10


def test_a_model_failure_leaves_a_thinner_pool_not_an_error(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch, data=RuntimeError("no budget left"))
    assert _run(nt.fill_pool()) == 0
    assert nt.pool() == []


def test_the_same_headline_always_gets_the_same_cover(monkeypatch, tmp_path):
    """Or the grid reshuffles on every refresh and a recognised card is lost."""
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch)
    _run(nt.fill_pool(target=6, max_new=6))
    covers = nt.pool()

    first = nt.pick_cover("Schools are banning AI", covers)
    assert first == nt.pick_cover("  Schools   are  banning AI ", covers)
    assert first in covers


def test_different_headlines_spread_across_the_pool(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch)
    _run(nt.fill_pool(target=8, max_new=8))
    covers = nt.pool()

    picked = {nt.pick_cover(f"story number {i}", covers) for i in range(40)}
    assert len(picked) > 1


def test_an_empty_pool_picks_nothing(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    assert nt.pick_cover("anything") is None


def test_a_cover_already_used_in_this_batch_steps_to_the_next(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch)
    _run(nt.fill_pool(target=4, max_new=4))
    covers = nt.pool()

    first = nt.pick_cover("a story", covers)
    second = nt.pick_cover("a story", covers, avoid={first})
    assert second != first and second in covers


def test_a_fully_spoken_for_pool_repeats_rather_than_blanks(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch)
    _run(nt.fill_pool(target=2, max_new=2))
    covers = nt.pool()

    assert nt.pick_cover("x", covers, avoid=set(covers)) in covers


def test_the_pool_is_filed_where_it_can_be_told_apart(monkeypatch, tmp_path):
    """The asset type is the only durable record of which covers are ours."""
    seen = []
    monkeypatch.setattr(nt, "asset_local_path",
                        lambda kind, h, ext: (seen.append(kind),
                                              tmp_path / f"{h}.{ext}")[1])
    monkeypatch.setattr(nt, "relativise", lambda p: str(p))
    monkeypatch.setattr(nt, "pool_dir", lambda: tmp_path)
    _draws(monkeypatch)

    _run(nt.fill_pool(target=1, max_new=1))

    # Every path this touches, whether probing for an existing file or writing a
    # new one, is under the pool's own asset type and never `trend_thumbs`.
    assert seen and set(seen) == {nt.GENERATED_ASSET_TYPE}
    assert nt.GENERATED_ASSET_TYPE != "trend_thumbs"


# ---------------------------------------------------------------------------
# add_covers - publisher first, pool second
# ---------------------------------------------------------------------------

def test_the_publisher_is_always_asked_before_the_pool(monkeypatch, tmp_path):
    """A real photograph of the actual story beats a house card, and costs one
    request. Reaching for the pool first would show a worse picture."""
    _sandbox(monkeypatch, tmp_path)
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch, data=AssertionError("drew for a story that had a photo"))
    store = _Store([{"id": 1, "permalink": "https://news.example/ok",
                     "caption": "a real story"}])
    monkeypatch.setattr(nt, "store", store)
    _http(monkeypatch, _page_then_image(
        '<meta property="og:image" content="https://cdn.example/a.jpg">'))

    assert _run(nt.backfill()) == 1
    assert len(store.saved) == 1
    assert "news_cover_art" not in store.saved[0][1]


def test_only_the_stories_with_no_picture_get_a_house_cover(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch)
    store = _Store([])
    monkeypatch.setattr(nt, "store", store)

    def handler(request):
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=JPEG,
                                  headers={"content-type": "image/jpeg"})
        if request.url.path == "/ok":
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text='<meta property="og:image" content="https://cdn.example/a.jpg">')
        return httpx.Response(403, text="no")

    _http(monkeypatch, handler)

    out = _run(nt.add_covers([
        (1, "https://news.example/ok", "has a photo"),
        (2, "https://news.example/blocked", "blocked"),
    ]))

    assert out == {"logo": 0, "found": 1, "outlet": 0, "house": 1, "none": 0}


def test_two_stories_in_one_batch_never_share_a_picture(monkeypatch, tmp_path):
    """Five stories over ten covers collide about as often as not. Two identical
    illustrations side by side in a grid reads as a bug."""
    _sandbox(monkeypatch, tmp_path)
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch)
    store = _Store([])
    monkeypatch.setattr(nt, "store", store)
    _http(monkeypatch, lambda r: httpx.Response(403, text="no"))

    out = _run(nt.add_covers(
        [(i, f"https://news.example/{i}", f"story {i}") for i in range(1, 6)],
        max_generated=10,
    ))

    chosen = [rel for _pid, rel in store.saved]
    assert out["house"] == 5
    assert len(set(chosen)) == 5


def test_a_batch_with_no_pool_and_no_model_leaves_the_tiles_alone(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch, data=RuntimeError("no budget"))
    monkeypatch.setattr(nt, "store", _Store([]))
    _http(monkeypatch, lambda r: httpx.Response(403, text="no"))

    out = _run(nt.add_covers([(1, "https://news.example/a", "a story")]))

    assert out == {"logo": 0, "found": 0, "outlet": 0, "house": 0, "none": 1}


# ---------------------------------------------------------------------------
# The extension has to match the bytes
# ---------------------------------------------------------------------------

def test_a_file_is_named_for_what_it_actually_is():
    """The route that serves these reads the image type off the FILE NAME, so a
    PNG saved as .jpg is answered as image/jpeg. That is not pedantry: it is the
    bug that made every drawn cover render as a blank grey tile while the real
    photographs beside them rendered fine."""
    assert nt.image_ext(b"\x89PNG\r\n\x1a\n" + b"x" * 40) == "png"
    assert nt.image_ext(b"\xff\xd8\xff" + b"x" * 40) == "jpg"
    assert nt.image_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert nt.image_ext(b"GIF89a" + b"x" * 40) == "gif"
    assert nt.image_ext(b"who knows") == "jpg"       # a safe default, not a guess


def test_a_drawn_png_lands_with_a_png_name(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch, data=PNG)
    _run(nt.fill_pool(target=1, max_new=1))
    assert [Path(f).suffix for f in nt.pool()] == [".png"]


def test_a_drawn_jpeg_lands_with_a_jpg_name(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch, data=b"\xff\xd8\xff" + b"jpegbytes" * 100)
    _run(nt.fill_pool(target=1, max_new=1))
    assert [Path(f).suffix for f in nt.pool()] == [".jpg"]


def test_the_pool_reads_every_image_type_it_can_write(monkeypatch, tmp_path):
    """A pool that wrote .webp and then only listed .png would draw the same
    brief again on every tick, for ever."""
    root = _pool_sandbox(monkeypatch, tmp_path)
    for name in ("a.png", "b.jpg", "c.jpeg", "d.webp", "e.gif"):
        (root / name).write_bytes(b"x" * 900)
    (root / "notes.txt").write_bytes(b"x" * 900)
    assert len(nt.pool()) == 5


def test_a_brief_already_drawn_in_another_format_is_not_redrawn(monkeypatch, tmp_path):
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch, data=b"\xff\xd8\xff" + b"jpegbytes" * 100)
    assert _run(nt.fill_pool(target=1, max_new=1)) == 1

    _draws(monkeypatch, data=AssertionError("redrew a brief it already had"))
    assert _run(nt.fill_pool(target=1, max_new=1)) == 0


# ---------------------------------------------------------------------------
# Logos: the company first, the publisher's mark before the pool
# ---------------------------------------------------------------------------

LOGO_PNG = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + (180).to_bytes(4, "big") + (180).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
            + b"\0" * 4096)


def test_a_story_about_a_company_wears_that_companys_logo(monkeypatch, tmp_path):
    """The judge said OpenAI, so the card gets OpenAI's mark, and the publisher
    is never even asked: a mark that says what the story is about beats a
    stock photograph of it."""
    _sandbox(monkeypatch, tmp_path)
    _no_pool(monkeypatch, tmp_path)
    store = _Store([{
        "id": 1, "permalink": "https://news.example/story", "caption": "a",
        "metadata": {"company": "OpenAI", "company_site": "openai.com"},
    }])
    monkeypatch.setattr(nt, "store", store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "news.example":
            raise AssertionError("the publisher was asked although the company answered")
        if request.url.path == "/apple.png":
            return httpx.Response(200, content=LOGO_PNG,
                                  headers={"content-type": "image/png"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text='<link rel="apple-touch-icon" href="/apple.png">')

    _http(monkeypatch, handler)

    assert _run(nt.backfill()) == 1
    assert len(store.saved) == 1
    assert nl.LOGO_ASSET_TYPE in store.saved[0][1]


def test_a_company_with_no_reachable_logo_falls_through_to_the_publisher(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    _no_pool(monkeypatch, tmp_path)
    store = _Store([{
        "id": 1, "permalink": "https://news.example/story", "caption": "a",
        "metadata": {"company_site": "blocked.example"},
    }])
    monkeypatch.setattr(nt, "store", store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in ("blocked.example", "www.google.com"):
            return httpx.Response(403, text="no")
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=JPEG,
                                  headers={"content-type": "image/jpeg"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text='<meta property="og:image" content="https://cdn.example/a.jpg">')

    _http(monkeypatch, handler)

    out = _run(nt.add_covers([(1, "https://news.example/story", "a", "blocked.example")]))
    assert out == {"logo": 0, "found": 1, "outlet": 0, "house": 0, "none": 0}


def test_a_publisher_with_no_picture_lends_its_own_logo_before_the_pool(monkeypatch, tmp_path):
    """The article page is a bot wall, so there is no photograph. The
    publisher's homepage still declares its mark, and that mark beats a drawn
    desk: it at least says where the story is from."""
    _sandbox(monkeypatch, tmp_path)
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch, data=AssertionError("drew a house cover although the publisher had a mark"))
    store = _Store([{"id": 1, "permalink": "https://news.example/story", "caption": "a"}])
    monkeypatch.setattr(nt, "store", store)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.google.com":
            return httpx.Response(404)
        if request.url.path == "/story":
            return httpx.Response(403, text="no bots")
        if request.url.path == "/apple.png":
            return httpx.Response(200, content=LOGO_PNG,
                                  headers={"content-type": "image/png"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text='<link rel="apple-touch-icon" href="/apple.png">')

    _http(monkeypatch, handler)

    out = _run(nt.add_covers([(1, "https://news.example/story", "a")]))
    assert out == {"logo": 0, "found": 0, "outlet": 1, "house": 0, "none": 0}
    assert nl.LOGO_ASSET_TYPE in store.saved[0][1]


def test_the_house_cover_is_the_last_resort(monkeypatch, tmp_path):
    """No company, a bot wall on the article, no icon on the homepage, and the
    fallback lookup knows nothing: only THEN is a card drawn from the pool."""
    _sandbox(monkeypatch, tmp_path)
    _pool_sandbox(monkeypatch, tmp_path)
    _draws(monkeypatch)
    store = _Store([{"id": 1, "permalink": "https://news.example/story", "caption": "a"}])
    monkeypatch.setattr(nt, "store", store)
    _http(monkeypatch, lambda r: httpx.Response(403, text="no"))

    out = _run(nt.add_covers([(1, "https://news.example/story", "a")]))
    assert out == {"logo": 0, "found": 0, "outlet": 0, "house": 1, "none": 0}
    assert "news_cover_art" in store.saved[0][1] or "pool" in store.saved[0][1]
