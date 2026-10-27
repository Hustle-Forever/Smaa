"""
Account gate for the whole app (public preview window).

Fixed accounts sharing one password (ACCOUNTS below). Signing in sets an
access cookie; while the gate is armed (env APP_ACCESS_PASSWORD is set), every
/api/* route except health + the auth endpoints requires that cookie, so nobody
can burn OpenAI/Vertex spend without an account, even by calling the API
directly with curl. With the env var empty the gate is disarmed and the API is
open (local dev is unaffected) - but /api/auth/login still checks credentials
either way, so a wrong email or password never reaches the app.

Stateless: the cookie is `email|HMAC(secret, email)`, so there is no session
store and every request carries who is signed in. The secret is
APP_ACCESS_PASSWORD when set, otherwise a key that lives and dies with this
process (a dev restart signs you out, which costs nothing where the gate is
disarmed anyway).

Note on APP_ACCESS_PASSWORD: nobody types it any more. It is now the switch
that arms the gate plus the key the cookie is signed with. The password people
type is PASSWORD below.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from .config import config

# Cookie name for the signed session.
COOKIE_NAME = "studio_access"
# Domain-separation tag so the token can't be reused for anything else.
_TAG = b"42studio-access-v2"

# One password for all the accounts. Preview credentials, not secrets: the
# accounts exist so each person signs in as themselves instead of one anonymous
# password being passed around in chat. A deployment sets its own through
# APP_LOGIN_PASSWORD; without it the local-dev default applies.
PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "").strip() or "1234"


@dataclass(frozen=True)
class Account:
    email: str
    name: str
    initials: str


ACCOUNTS: tuple[Account, ...] = (
    Account("yasin@smaa.com", "Yasin", "Y"),
    Account("agegnew@smaa.com", "Agegnew", "A"),
    Account("mayada@smaa.com", "Mayada", "M"),
)

_BY_EMAIL = {a.email: a for a in ACCOUNTS}

# Signing key used when no password is configured. Regenerated per process.
_FALLBACK_SECRET = secrets.token_hex(32)


def _secret() -> bytes:
    return (config.access_password or _FALLBACK_SECRET).encode("utf-8")


def enabled() -> bool:
    """True when the middleware should reject un-signed-in /api calls."""
    return bool(config.access_password)


def find_account(email: str) -> Account | None:
    """The account for an email, matched case-insensitively."""
    return _BY_EMAIL.get((email or "").strip().lower())


def _sign(email: str) -> str:
    return hmac.new(_secret(), _TAG + email.encode("utf-8"), hashlib.sha256).hexdigest()


def token_for_login(email: str, password: str) -> tuple[Account, str] | None:
    """The account and its cookie token when the credentials match, else None.

    The password comparison is constant-time and runs even for an unknown email,
    so a missing account and a wrong password take the same time to reject.
    """
    account = find_account(email)
    password_ok = secrets.compare_digest(
        (password or "").encode("utf-8"), PASSWORD.encode("utf-8")
    )
    if account is None or not password_ok:
        return None
    return account, f"{account.email}|{_sign(account.email)}"


def account_for_token(token: str | None) -> Account | None:
    """Who this cookie belongs to, or None if it doesn't verify."""
    if not token or "|" not in token:
        return None
    email, _, mac = token.partition("|")
    account = find_account(email)
    if account is None:
        return None
    return account if secrets.compare_digest(mac, _sign(account.email)) else None


def token_ok(token: str | None) -> bool:
    """True if the cookie is valid - or the gate is disarmed."""
    if not enabled():
        return True
    return account_for_token(token) is not None
