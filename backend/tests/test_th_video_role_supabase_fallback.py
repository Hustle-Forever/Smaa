"""Regression test for the Talking Head video routes' Supabase fallback.

Reproduces the deployed-server bug where a generated Talking Head clip shows
"Couldn't load this video (the file may be unavailable)" in the Generated Videos
tab while loading fine in the editor.

Root cause: ``asset_storage.url_for`` declares ``local_path`` as a *required*
keyword-only argument, but the TH v1 (``_th_video_role``) and TH v2
(``_th_v2_video_role``) route handlers called it WITHOUT ``local_path`` in their
tier-2 (Supabase recover) branch. On the deployed VPS the local-disk cache is
empty (the durable copy lives in the Supabase bucket; the Postgres row's
``local_path`` is shared via the shared DB but points at a file that only exists
on the machine that generated it), so tier-1 misses and tier-2 runs — raising
``TypeError: url_for() missing 1 required keyword-only argument: 'local_path'``
=> HTTP 500 => the <video> onError => the error banner. Locally tier-1 hits and
the bug is invisible.

These tests force the tier-2 path and assert the handler 307-redirects to the
signed URL instead of raising.
"""
import asyncio

import app.routers.yas_producer as r
from starlette.responses import RedirectResponse


def _run(coro):
    return asyncio.run(coro)


class _AnimRow(dict):
    """An animation row whose local file is GONE but lives in a bucket."""


def _patch_common(monkeypatch, service):
    # Run row: a single "main" animation linked, no scenes.
    run_row = {"id": 1, "metadata": {"main_animation_id": 99}}

    async def _fetch_run_row(run_id):
        return run_row

    async def _fetch_animation_row(anim_id):
        # Has a (stale) local_path AND a bucket copy — the deployed case.
        return _AnimRow(
            local_path="data/producer/animations/gone.mp4",
            storage_bucket="producer-media",
            storage_path="animations/abc.mp4",
            status="ready",
        )

    # The local path does NOT exist on disk (simulate the wiped/foreign cache).
    class _MissingPath:
        def exists(self):
            return False

    def _abs_from_rel(rel):
        return _MissingPath()

    monkeypatch.setattr(service, "_fetch_run_row", _fetch_run_row)
    monkeypatch.setattr(service, "_fetch_animation_row", _fetch_animation_row)
    monkeypatch.setattr(service, "_abs_from_rel", _abs_from_rel)

    # asset_storage.url_for is called with keyword args; a strict signature
    # (required local_path) means the buggy call raises TypeError BEFORE this
    # body runs. We assert it is invoked WITH local_path supplied. The optional
    # kwargs url_for gained (download_name, internal) are accepted and recorded.
    calls = {}

    async def _url_for(*, storage_bucket, storage_path, local_path, expires=3600, **_kw):
        calls["local_path_passed"] = True
        calls["kwargs"] = _kw
        return "https://signed.example/clip.mp4?token=xyz"

    monkeypatch.setattr(r.asset_storage, "url_for", _url_for)
    return calls


def test_th_v2_video_role_falls_back_to_supabase(monkeypatch):
    calls = _patch_common(monkeypatch, r.th_v2_service)
    resp = _run(r._th_v2_video_role(1, "main"))
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 307
    assert calls.get("local_path_passed") is True
    # A browser redirect: the internal (S3_ENDPOINT) origin must never leak here.
    assert not calls["kwargs"].get("internal")


def test_th_v1_video_role_falls_back_to_supabase(monkeypatch):
    calls = _patch_common(monkeypatch, r.th_service)
    resp = _run(r._th_video_role(1, "main"))
    assert isinstance(resp, RedirectResponse)
    assert resp.status_code == 307
    assert calls.get("local_path_passed") is True
    assert not calls["kwargs"].get("internal")
