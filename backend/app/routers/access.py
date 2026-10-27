"""
Sign-in endpoints (the account gate).

  GET  /api/auth/status  → {required, authenticated, user}  (public, drives the gate UI)
  POST /api/auth/login   → checks email + password, sets the access cookie
  POST /api/auth/logout  → clears the cookie

The actual enforcement is the middleware in main.py, which rejects any other
/api/* request without a valid cookie. These endpoints are exempt so the sign-in
screen can check the session and sign people in.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .. import access

router = APIRouter(prefix="/api/auth", tags=["access"])

# Keep people signed in for a week (re-prompt after that).
_COOKIE_MAX_AGE = 60 * 60 * 24 * 7


class LoginBody(BaseModel):
    email: str
    password: str


def _profile(account: access.Account) -> dict:
    """What the UI shows for the signed-in person."""
    return {"email": account.email, "name": account.name, "initials": account.initials}


@router.get("/status")
async def status(request: Request) -> dict:
    """Whether the gate is armed, whether THIS browser is through it, and who as."""
    token = request.cookies.get(access.COOKIE_NAME)
    account = access.account_for_token(token)
    return {
        "required": access.enabled(),
        "authenticated": access.token_ok(token),
        "user": _profile(account) if account else None,
    }


@router.post("/login")
async def login(body: LoginBody, response: Response) -> dict:
    match = access.token_for_login(body.email, body.password)
    if match is None:
        # One message for both failures: saying which half was wrong would tell
        # a stranger which emails exist.
        raise HTTPException(401, "That email and password don't match.")
    account, token = match
    # httponly: JS can't read it (XSS-safe); samesite=lax: sent on top-level
    # same-origin requests incl. <img>/<video>; no Secure flag so it also works
    # over plain http in local testing (the public tunnel is https regardless).
    response.set_cookie(
        key=access.COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "user": _profile(account)}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(access.COOKIE_NAME, path="/")
    return {"ok": True}
