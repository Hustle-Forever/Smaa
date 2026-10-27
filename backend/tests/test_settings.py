"""Tests for `GET /api/settings/connections`: state, never secrets.

WHAT USED TO BE HERE AND IS DELIBERATELY NOT ANY MORE. This file also asserted
that `build_hints` ignored the whole `brand_profile_settings` row and that
`SettingsPatch` refused the eight weights, the mode, the include flags and the
three model-tuning fields. That change has been reverted at the owner's request:
the weights, the auto/manual mode and the model settings are Agegnew's work,
they are restored exactly as he wrote them, and they are reachable again from
Settings under the Advanced tab.

Nothing here replaces those assertions with assertions about the restored
behaviour. That is on purpose. Tests are a statement about what is correct, and
what `build_hints` ought to do with a lopsided manual row belongs to whoever owns
that feature, not to the person who happened to revert it.

`/api/settings/connections` is unrelated to any of that: it reports whether
posting and reminder emails are wired up, and the Settings screen still uses it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Connections: state, never secrets
# ---------------------------------------------------------------------------

def _connections(monkeypatch, *, publish, email, handle="42abudhabi", to="a@b.c",
                 hosting=(True, "")):
    """Run the endpoint against a swapped-in config.

    `Config` is a frozen dataclass read once at import, which is exactly why
    none of this is editable from the Settings screen, so the test replaces the
    whole object rather than a field. The route re-imports `config` inside the
    function body, so patching the module attribute is what reaches it.

    `hosting` stands in for the media-storage check (Instagram fetches every
    post from a public URL); None leaves the real check in place.
    """
    import dataclasses

    from app import config as config_mod
    from app.routers import settings as route
    import app.services.asset_storage as storage
    import app.services.email_service as es
    import app.services.instagram_publish as ig

    monkeypatch.setattr(ig, "publish_status", lambda: publish)
    monkeypatch.setattr(es, "email_configured", lambda: email)
    if hosting is not None:
        monkeypatch.setattr(storage, "public_hosting_configured", lambda: hosting)
    monkeypatch.setattr(config_mod, "config", dataclasses.replace(
        config_mod.config, instagram_target_username=handle, reminder_email=to,
    ))
    return asyncio.run(route.get_connections())


def test_connections_report_a_working_setup(monkeypatch):
    out = _connections(monkeypatch, publish=(True, ""), email=True)
    assert out["instagram"] == {
        "ready": True, "handle": "42abudhabi", "reason": None, "detail": None,
    }
    assert out["email"] == {"ready": True, "to": "a@b.c"}


def test_the_screen_never_shows_the_servers_own_wording(monkeypatch):
    """Two sentences, for two readers.

    `publish_status` writes for whoever runs the server and names the exact
    environment variable to set, which is right for a log and wrong for a
    Settings screen: "set INSTAGRAM_ACCESS_TOKEN (or INSTAGRAM_PUBLISH_TOKEN)
    with the instagram_content_publish permission" means nothing to somebody who
    has never seen a shell. `reason` is the plain one the UI prints; `detail`
    carries the server's, for anyone reading the API directly.
    """
    server_wording = (
        "Instagram publishing not configured: set INSTAGRAM_ACCESS_TOKEN (or "
        "INSTAGRAM_PUBLISH_TOKEN) with the instagram_content_publish permission."
    )
    out = _connections(monkeypatch, publish=(False, server_wording), email=False,
                       to="")
    ig = out["instagram"]
    assert ig["ready"] is False
    assert ig["detail"] == server_wording
    # The part a person reads names no variable, no permission and no command.
    assert ig["reason"] and server_wording not in ig["reason"]
    for jargon in ("INSTAGRAM", "TOKEN", "permission", "configured", "_"):
        assert jargon not in ig["reason"], f"{jargon!r} leaked into the UI copy"
    assert out["email"] == {"ready": False, "to": None}


def test_a_check_that_throws_is_reported_not_raised(monkeypatch):
    """A status panel that 500s is worse than one saying it does not know."""
    def _boom():
        raise RuntimeError("credentials are malformed")

    from app.routers import settings as route
    import app.services.asset_storage as storage
    import app.services.email_service as es
    import app.services.instagram_publish as ig

    monkeypatch.setattr(ig, "publish_status", _boom)
    monkeypatch.setattr(storage, "public_hosting_configured", lambda: (True, ""))
    monkeypatch.setattr(es, "email_configured", lambda: True)

    out = asyncio.run(route.get_connections())
    assert out["instagram"]["ready"] is False
    # The cause is kept where an operator will find it, not put on the screen.
    assert "malformed" in out["instagram"]["detail"]
    assert "malformed" not in (out["instagram"]["reason"] or "")


# ---------------------------------------------------------------------------
# Media hosting: a connected account still cannot post without a public URL
# ---------------------------------------------------------------------------

_JARGON = ("INSTAGRAM", "TOKEN", "permission", "configured", "_")
_SECRETS = ("token", "password", "secret", "smtp", "account_id")


def _assert_plain(reason: str) -> None:
    for jargon in _JARGON:
        assert jargon not in reason, f"{jargon!r} leaked into the UI copy"


def test_a_connected_account_without_media_hosting_is_not_ready(monkeypatch):
    """Instagram fetches every post from a public URL, so with nowhere to host
    the media each Post is skipped. Saying "ready" there was the old bug."""
    storage_wording = "the media storage public URL is not set (S3_PUBLIC_BASE_URL)"
    out = _connections(monkeypatch, publish=(True, ""), email=True,
                       hosting=(False, storage_wording))
    ig = out["instagram"]
    assert ig["ready"] is False
    assert ig["handle"] == "42abudhabi"
    # A different plain sentence from the no-account one: the account is fine.
    no_account = _connections(monkeypatch, publish=(False, "x"), email=True)["instagram"]
    assert ig["reason"] and ig["reason"] != no_account["reason"]
    _assert_plain(ig["reason"])
    assert storage_wording not in ig["reason"]
    # The server's wording goes to the operator field, as for publishing.
    assert storage_wording in ig["detail"]
    flat = repr(out).lower()
    for leaked in _SECRETS:
        assert leaked not in flat
    assert set(ig) == {"ready", "handle", "reason", "detail"}


def test_a_missing_account_is_reported_before_missing_hosting(monkeypatch):
    """Connecting the account is the first thing to fix, so it is what the
    screen says when both are missing."""
    server_wording = "Instagram publishing not configured: set INSTAGRAM_ACCOUNT_ID."
    out = _connections(monkeypatch, publish=(False, server_wording), email=True,
                       hosting=(False, "the media storage credentials are not set"))
    assert out["instagram"]["ready"] is False
    assert out["instagram"]["detail"] == server_wording
    assert "connected yet" in out["instagram"]["reason"]


def test_a_hosting_check_that_throws_is_reported_not_raised(monkeypatch):
    def _boom():
        raise RuntimeError("storage config is malformed")

    from app.routers import settings as route
    import app.services.asset_storage as storage
    import app.services.email_service as es
    import app.services.instagram_publish as ig_mod

    monkeypatch.setattr(ig_mod, "publish_status", lambda: (True, ""))
    monkeypatch.setattr(storage, "public_hosting_configured", _boom)
    monkeypatch.setattr(es, "email_configured", lambda: True)

    ig = asyncio.run(route.get_connections())["instagram"]
    assert ig["ready"] is False
    assert "malformed" in ig["detail"]
    assert "malformed" not in ig["reason"]
    # The account check passed, so the screen does not blame the account.
    assert "connected yet" not in ig["reason"]
    _assert_plain(ig["reason"])


def test_an_operator_detail_that_reads_like_a_credential_is_replaced(monkeypatch):
    """The detail is for operators, but the payload still never carries a word
    like "secret", whatever the storage layer's reason says."""
    out = _connections(monkeypatch, publish=(True, ""), email=True,
                       hosting=(False, "S3_SECRET_KEY is empty"))
    ig = out["instagram"]
    assert ig["ready"] is False
    assert "secret" not in ig["detail"].lower()
    assert "media storage endpoint, credentials and public URL" in ig["detail"]


@pytest.mark.parametrize("settings", [
    # local disk only (the conftest default)
    {},
    # S3 with each piece missing in turn
    {"storage_backend": "s3"},
    {"storage_backend": "s3", "s3_endpoint": "http://minio:9000"},
    {"storage_backend": "s3", "s3_endpoint": "http://minio:9000",
     "s3_access_key": "k", "s3_secret_key": "s"},
    {"storage_backend": "s3", "s3_endpoint": "http://minio:9000",
     "s3_access_key": "k", "s3_secret_key": "s",
     "s3_public_base_url": "https://x.test/media", "approved_media_bucket": ""},
    # Supabase with each piece missing in turn
    {"storage_backend": "supabase"},
    {"storage_backend": "supabase", "supabase_url": "https://p.supabase.co"},
])
def test_every_real_hosting_reason_is_safe_to_report(monkeypatch, settings):
    """The real storage check, every way it can say no: the screen stays
    plain and the payload never names a credential."""
    from app.config import config

    saved = config.approved_media_bucket
    try:
        for name, value in settings.items():
            object.__setattr__(config, name, value)
        out = _connections(monkeypatch, publish=(True, ""), email=True, hosting=None)
    finally:
        object.__setattr__(config, "approved_media_bucket", saved)
    ig = out["instagram"]
    assert ig["ready"] is False
    _assert_plain(ig["reason"])
    assert ig["detail"].startswith("Instagram media hosting is not set up: ")
    flat = repr(out).lower()
    for leaked in _SECRETS:
        assert leaked not in flat


def test_a_fully_set_up_object_store_counts_as_hosting(monkeypatch):
    from app.config import config

    for name, value in {
        "storage_backend": "s3", "s3_endpoint": "http://minio:9000",
        "s3_access_key": "k", "s3_secret_key": "s",
        "s3_public_base_url": "https://x.test/media",
    }.items():
        object.__setattr__(config, name, value)
    out = _connections(monkeypatch, publish=(True, ""), email=True, hosting=None)
    assert out["instagram"] == {
        "ready": True, "handle": "42abudhabi", "reason": None, "detail": None,
    }


def test_no_secret_is_ever_in_the_payload(monkeypatch):
    """The token and the SMTP password stay in the environment. This endpoint
    answers "does it work", and a status panel is not a place to leak one."""
    out = _connections(monkeypatch, publish=(True, ""), email=True)
    flat = repr(out).lower()
    for leaked in ("token", "password", "secret", "smtp", "account_id"):
        assert leaked not in flat
    # And the payload is exactly these keys: a field added later without a
    # thought about what it carries fails here rather than in production.
    assert set(out) == {"instagram", "email"}
    assert set(out["instagram"]) == {"ready", "handle", "reason", "detail"}
    assert set(out["email"]) == {"ready", "to"}
