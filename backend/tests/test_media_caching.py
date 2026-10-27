"""Tests for the media caching / fast-load work.

Covers the behaviour-bearing pieces WITHOUT network or DB:

  1. ``asset_storage`` signed-URL TTL cache — hit, expiry, and evict-on-delete,
     so we stop re-signing the same object on every request.
  2. ``asset_storage.public_url`` / ``public_render_url`` shape — permanent CDN
     URLs + the on-the-fly transform (thumbnail) endpoint.
  3. Router URL builders — ``_public_media_url`` prefers a public CDN URL (transform
     for thumbnails) when the row is in Supabase, and falls back to the backend
     proxy route for local-disk rows; ``_cache_bust`` is separator-aware.
  4. ``_MEDIA_CACHE_HEADERS`` is the immutable long-cache directive.

Runs standalone (``cd backend && python tests/test_media_caching.py``) and pytest.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))


# --- signed-URL cache --------------------------------------------------------

def test_signed_cache_hit_and_evict():
    from app.services import asset_storage as a

    a._SIGNED_URL_CACHE.clear()
    key = ("producer-assets", "x/y.png", 3600, "")
    assert a._signed_cache_get(key) is None
    a._signed_cache_put(key, "https://signed/url?token=abc", ttl=3600)
    assert a._signed_cache_get(key) == "https://signed/url?token=abc"
    # Evict drops every (expires, transform) tier for the (bucket, path).
    a._signed_cache_put(("producer-assets", "x/y.png", 600, ""), "https://other", ttl=600)
    a._signed_cache_evict("producer-assets", "x/y.png")
    assert a._signed_cache_get(key) is None
    assert a._signed_cache_get(("producer-assets", "x/y.png", 600, "")) is None


def test_signed_cache_expiry():
    from app.services import asset_storage as a

    a._SIGNED_URL_CACHE.clear()
    key = ("b", "p", 1, "")
    a._signed_cache_put(key, "https://u", ttl=-1)  # already expired
    assert a._signed_cache_get(key) is None  # expired entries are dropped on read


def test_signed_cache_is_bounded():
    from app.services import asset_storage as a

    a._SIGNED_URL_CACHE.clear()
    for i in range(a._SIGNED_URL_CACHE_MAX + 50):
        a._signed_cache_put(("b", f"p{i}", 3600, ""), f"https://u/{i}", ttl=3600)
    assert len(a._SIGNED_URL_CACHE) <= a._SIGNED_URL_CACHE_MAX


def test_transform_key_is_canonical_and_distinct():
    """The cache key MUST carry a transform dimension. Supabase silently ignores
    ?width= on a plain token and returns the full-resolution original, so a shared
    (bucket, path, expires) key would hand a thumbnail request a full-res URL."""
    from app.services import asset_storage as a

    assert a.transform_key(None) == ""
    assert a.transform_key({}) == ""
    # Key order must not fork the cache entry.
    assert a.transform_key({"width": 480, "quality": 70}) == \
           a.transform_key({"quality": 70, "width": 480})
    # Different tiers must NOT collide.
    assert a.transform_key(a.THUMB_TRANSFORM) != a.transform_key(a.PREVIEW_TRANSFORM)
    assert a.transform_key(a.THUMB_TRANSFORM) != ""


def test_can_transform_skips_untransformable_types():
    """Supabase's transformer is raster-only: signing an SVG or a video with a
    transform yields a tile that 400s."""
    from app.services import asset_storage as a

    assert a.can_transform("image/png")
    assert a.can_transform("image/jpeg")
    assert not a.can_transform("image/svg+xml")
    assert not a.can_transform("video/mp4")
    assert not a.can_transform(None)


# --- public URL helpers ------------------------------------------------------

def _set_supabase_url(url: str):
    """`config` is a frozen dataclass — bypass the freeze for a test, return the
    previous values so the caller can restore them. Also turns the public-media
    flag ON (the public-URL path is gated on it)."""
    from app.config import config

    old = (
        config.supabase_url,
        config.producer_media_public,
        config.supabase_image_transform,
    )
    object.__setattr__(config, "supabase_url", url)
    object.__setattr__(config, "producer_media_public", True)
    # These tests assert the SHAPE of an image-transform URL, so they need the
    # paid add-on treated as present. Whether it IS present is a separate
    # question with its own tests below — conflating the two is what let the
    # Library ship 403-ing URLs with a green suite (#37).
    object.__setattr__(config, "supabase_image_transform", "on")
    return old


def _restore_supabase_url(old) -> None:
    from app.config import config

    object.__setattr__(config, "supabase_url", old[0])
    object.__setattr__(config, "producer_media_public", old[1])
    object.__setattr__(config, "supabase_image_transform", old[2])


def _stub_publicness(mapping: dict) -> dict:
    """Force the per-bucket publicness map (normally fetched from Supabase).
    Returns the previous contents so the caller can restore it."""
    from app.services import asset_storage as a

    prev = dict(a._BUCKET_PUBLIC)
    a._BUCKET_PUBLIC.clear()
    a._BUCKET_PUBLIC.update(mapping)
    return prev


def _restore_publicness(prev: dict) -> None:
    from app.services import asset_storage as a

    a._BUCKET_PUBLIC.clear()
    a._BUCKET_PUBLIC.update(prev)


def test_public_url_and_transform_shape():
    from app.services import asset_storage as a

    old = _set_supabase_url("https://proj.supabase.co")
    try:
        pub = a.public_url("producer-media", "poster/12.png")
        assert pub == "https://proj.supabase.co/storage/v1/object/public/producer-media/poster/12.png"
        thumb = a.public_render_url("producer-media", "poster/12.png", width=480, quality=70)
        assert "/render/image/public/producer-media/poster/12.png" in thumb
        assert "width=480" in thumb and "quality=70" in thumb
    finally:
        _restore_supabase_url(old)


def test_bucket_is_public_defaults_to_private():
    """Unknown / unfetched bucket ⇒ private ⇒ backend proxy. The safe direction:
    slow, never a 400."""
    from app.services import asset_storage as a

    prev = _stub_publicness({"producer-media": True, "producer-assets": False})
    try:
        assert a.bucket_is_public("producer-media") is True
        assert a.bucket_is_public("producer-assets") is False
        assert a.bucket_is_public("never-heard-of-it") is False
        assert a.bucket_is_public(None) is False
    finally:
        _restore_publicness(prev)


# --- router URL builders -----------------------------------------------------

def test_cache_bust_is_separator_aware():
    from app.routers import yas_producer as yp

    rec = {"created_at": "2026-06-27T06:37:03+00:00"}
    # No existing query → '?'
    assert yp._cache_bust("/api/x/file", rec).count("?") == 1
    # Existing query (transform URL) → '&', exactly one '?'
    busted = yp._cache_bust("https://h/render?width=480&quality=70", rec)
    assert busted.count("?") == 1 and "&v=" in busted


def test_public_media_url_prefers_cdn_then_proxy():
    from app.routers import yas_producer as yp

    old = _set_supabase_url("https://proj.supabase.co")
    prev = _stub_publicness({"producer-media": True, "producer-assets": False})
    rec = {"created_at": "2026-06-27T06:37:03+00:00"}
    try:
        # Supabase row + thumb → transform CDN URL, cache-busted.
        url = yp._public_media_url(
            rec, bucket="producer-media", path="poster/12.png",
            proxy_url="/api/yas-producer/poster/12/file", thumb=True,
        )
        assert "/render/image/public/producer-media/poster/12.png" in url
        assert "width=480" in url and "v=" in url
        # HEIGHT + RESIZE ARE MANDATORY. width alone makes Supabase default to
        # resizing_type=fill, which keeps the SOURCE height and crops the width —
        # a 1024x1024 poster came back 480x1024 with the headline sliced off.
        assert "height=480" in url
        assert "resize=contain" in url

        # Supabase row, full (not thumb) → object/public URL.
        full = yp._public_media_url(
            rec, bucket="producer-media", path="poster/12.png",
            proxy_url="/api/yas-producer/poster/12/file",
        )
        assert "/object/public/producer-media/poster/12.png" in full

        # Local-disk row (no bucket/path) → backend proxy fallback, cache-busted.
        local = yp._public_media_url(
            rec, bucket=None, path=None,
            proxy_url="/api/yas-producer/poster/12/file", thumb=True,
        )
        assert local.startswith("/api/yas-producer/poster/12/file?v=")
    finally:
        _restore_publicness(prev)
        _restore_supabase_url(old)


def test_private_bucket_never_gets_a_public_url():
    """REGRESSION GUARD for the "no preview" bug.

    Every yas_generations row names the PRIVATE producer-assets bucket. The old
    gate checked only the process-wide producer_media_public flag, so it emitted
    /render/image/public/producer-assets/... which Supabase answers
    400 {"error":"Bucket not found"} — rendered by <img onError> as "no preview"
    on 38 of 48 Library tiles and 12 of 12 Dashboard tiles.

    With the flag ON and the bucket PRIVATE, the URL must stay on the proxy.
    """
    from app.routers import yas_producer as yp

    old = _set_supabase_url("https://proj.supabase.co")  # also turns the flag ON
    prev = _stub_publicness({"producer-media": True, "producer-assets": False})
    rec = {"created_at": "2026-07-26T17:03:25+00:00"}
    try:
        for thumb in (True, False):
            url = yp._public_media_url(
                rec, bucket="producer-assets", path="yas/generations/746.png",
                proxy_url="/api/yas-producer/poster-human/746/file", thumb=thumb,
            )
            assert url.startswith("/api/yas-producer/poster-human/746/file?v="), url
            assert "/public/" not in url
            assert "producer-assets" not in url
    finally:
        _restore_publicness(prev)
        _restore_supabase_url(old)


def test_public_media_url_gated_off_uses_proxy():
    """With PRODUCER_MEDIA_PUBLIC off (default), even a Supabase row stays on the
    backend proxy route — so flipping the flag is the ONLY thing that changes URL
    semantics, and a private bucket can never serve a 400-ing public URL."""
    from app.routers import yas_producer as yp
    from app.config import config

    prev = config.producer_media_public
    object.__setattr__(config, "producer_media_public", False)
    try:
        url = yp._public_media_url(
            {"created_at": "2026-06-27T06:37:03+00:00"},
            bucket="producer-media", path="poster/12.png",
            proxy_url="/api/yas-producer/poster/12/file", thumb=True,
        )
        assert url.startswith("/api/yas-producer/poster/12/file?v=")
    finally:
        object.__setattr__(config, "producer_media_public", prev)


def test_media_cache_headers_are_immutable():
    from app.routers import yas_producer as yp

    cc = yp._MEDIA_CACHE_HEADERS["Cache-Control"]
    assert "immutable" in cc and "max-age=31536000" in cc


def test_etag_response_200_then_304():
    from app.routers import yas_producer as yp
    from starlette.requests import Request

    def make_req(headers: dict) -> Request:
        return Request({
            "type": "http",
            "method": "GET",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        })

    payload = {"items": [{"id": 1, "thumbnail_url": "https://cdn/x.png"}]}
    first = yp._etag_response(make_req({}), payload)
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert etag
    # Same content + matching If-None-Match → 304, no body.
    second = yp._etag_response(make_req({"if-none-match": etag}), payload)
    assert second.status_code == 304
    # Changed content → new etag, 200.
    changed = yp._etag_response(make_req({"if-none-match": etag}), {"items": []})
    assert changed.status_code == 200 and changed.headers["etag"] != etag


# --- image-transform capability (#37) ----------------------------------------
#
# `/storage/v1/render/image/**` is a paid Supabase add-on. Without it every such
# URL answers 403 {"code":"FeatureNotEnabled"} — in the BROWSER, inside list JSON,
# where no backend log can see it — and the whole Library rendered "Preview
# unavailable" while the objects themselves fetched fine over /object/.
# These pin the rule: never emit a URL shape the tenant cannot serve.

def _set_transform_mode(mode: str):
    from app.config import config

    old = config.supabase_image_transform
    object.__setattr__(config, "supabase_image_transform", mode)
    return old


def _restore_transform_mode(old) -> None:
    from app.config import config

    object.__setattr__(config, "supabase_image_transform", old)


def test_image_transform_modes_override_the_probe():
    from app.services import asset_storage as a

    old = _set_transform_mode("on")
    try:
        assert a.image_transform_enabled() is True
    finally:
        _restore_transform_mode(old)

    old = _set_transform_mode("off")
    try:
        assert a.image_transform_enabled() is False
    finally:
        _restore_transform_mode(old)


def test_auto_mode_is_unavailable_until_proven_available():
    """Unknown ⇒ no transform. The safe direction: bigger bytes, never blank."""
    from app.services import asset_storage as a

    old = _set_transform_mode("auto")
    prev = a._IMAGE_TRANSFORM_OK
    try:
        a._IMAGE_TRANSFORM_OK = None
        assert a.image_transform_enabled() is False
        a._IMAGE_TRANSFORM_OK = False
        assert a.image_transform_enabled() is False
        a._IMAGE_TRANSFORM_OK = True
        assert a.image_transform_enabled() is True
    finally:
        a._IMAGE_TRANSFORM_OK = prev
        _restore_transform_mode(old)


def test_to_render_path_only_rewrites_when_the_feature_exists():
    from app.services import asset_storage as a

    signed = "https://proj.supabase.co/storage/v1/object/sign/producer-assets/a.png?token=x"

    old = _set_transform_mode("on")
    try:
        assert "/render/image/sign/" in a._to_render_path(signed)
    finally:
        _restore_transform_mode(old)

    old = _set_transform_mode("off")
    try:
        # The exact regression: this used to rewrite unconditionally, handing the
        # browser a URL that 403s on a tenant without the add-on.
        assert a._to_render_path(signed) == signed
    finally:
        _restore_transform_mode(old)


def test_thumb_cache_tier_tracks_the_capability():
    """Otherwise a capability flip keeps serving whichever shape was cached
    first, for as long as the signed token lives (~24h)."""
    from app.services import asset_storage as a

    old = _set_transform_mode("on")
    try:
        on_tier = a._thumb_tier()
    finally:
        _restore_transform_mode(old)

    old = _set_transform_mode("off")
    try:
        off_tier = a._thumb_tier()
    finally:
        _restore_transform_mode(old)

    assert on_tier != off_tier


def test_public_thumb_falls_back_to_plain_object_without_the_addon():
    from app.config import config
    from app.routers import yas_producer as yp

    old = _set_supabase_url("https://proj.supabase.co")
    prev = _stub_publicness({"producer-media": True})
    rec = {"created_at": "2026-06-27T06:37:03+00:00"}
    try:
        object.__setattr__(config, "supabase_image_transform", "off")
        url = yp._public_media_url(
            rec, bucket="producer-media", path="poster/12.png",
            proxy_url="/api/yas-producer/poster/12/file", thumb=True,
        )
        assert "/render/image/" not in url, "a dead URL shape must never be emitted"
        assert "/object/public/producer-media/poster/12.png" in url
        assert "v=" in url  # still cache-busted
    finally:
        _restore_publicness(prev)
        _restore_supabase_url(old)


# --- standalone monkeypatch shim --------------------------------------------

class _MP:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value, raising=True):
        had = hasattr(obj, name)
        old = getattr(obj, name, None)
        self._undo.append((obj, name, old, had))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old, had in reversed(self._undo):
            if had:
                setattr(obj, name, old)
        self._undo.clear()


if __name__ == "__main__":
    import inspect

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        if "monkeypatch" in inspect.signature(fn).parameters:
            mp = _MP()
            try:
                fn(mp)
            finally:
                mp.undo()
        else:
            fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} media-caching tests passed.")
