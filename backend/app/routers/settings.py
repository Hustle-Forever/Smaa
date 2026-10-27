"""The settings surface: GET / PUT /api/settings, plus a read-only connections probe.

`PUT /api/settings` is back to the whitelist it has always had: the mode, the
eight weights, the four include flags, the three model-tuning fields and the
owner's sentence (see `models/settings.SettingsPatch`). That write path and the
`build_hints` logic it feeds are Agegnew's and are deliberately left exactly as
he wrote them, unpatched, so the feature can be reviewed as one piece.

`GET /api/settings/connections` answers a question the app could not answer
before: does posting actually work, and will the reminder emails actually send?
Both checks already existed and were simply never exposed. It reports state,
never secrets: the token and the SMTP password live in the environment and stay
there.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from ..db import get_conn
from ..models.settings import SettingsPatch, SettingsResponse

router = APIRouter(prefix="/api", tags=["settings"])

# The plain sentences the Settings screen prints after "Not set up, so nothing
# can be posted." They name no variable, permission or command.
_NO_ACCOUNT = "No Instagram account is connected yet, so nothing can be posted."
_NO_HOSTING = (
    "The account is connected, but there is no public place yet to put the "
    "photos and videos for Instagram to fetch."
)
# Words that must never reach this payload, even inside an operator detail.
_SECRETISH = ("token", "password", "secret", "smtp", "account_id")


def _hosting_detail(why: str) -> str:
    """The operator sentence for missing media hosting. The storage layer's
    reason names settings, never values, but it is filtered anyway: a status
    panel is no place for anything that reads like a credential."""
    if not why or any(w in why.lower() for w in _SECRETISH):
        why = "set the media storage endpoint, credentials and public URL"
    return f"Instagram media hosting is not set up: {why}"


def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


@router.get("/settings", response_model=SettingsResponse)
async def get_settings():
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM brand_profile_settings ORDER BY id ASC LIMIT 1"
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=500, detail="brand_profile_settings row missing")
    return _row(row)


@router.put("/settings", response_model=SettingsResponse)
async def update_settings(patch: SettingsPatch):
    body = patch.model_dump(exclude_none=True)
    if not body:
        return await get_settings()

    # Round temperature
    if "temperature" in body:
        body["temperature"] = round(float(body["temperature"]), 2)

    set_parts = []
    params: list = []
    for k, v in body.items():
        set_parts.append(f"{k} = %s")
        params.append(v)
    sql = (
        f"UPDATE brand_profile_settings SET {', '.join(set_parts)} "
        f"WHERE id = (SELECT MIN(id) FROM brand_profile_settings) "
        f"RETURNING *"
    )

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=500, detail="UPDATE returned no row")
    return _row(row)


@router.get("/settings/connections")
async def get_connections() -> dict:
    """Is posting wired up, and will reminder emails send?

    Read-only and secret-free. The Instagram token, the account id and the SMTP
    password are environment values frozen at import; this says whether they add
    up to something that works, which is the only part a person on the Settings
    screen can act on.

    TWO SENTENCES, FOR TWO READERS. `publish_status` writes for whoever runs the
    server, and it is right to: its reason names the exact environment variable
    to set. That sentence must not reach a Settings screen, where it reads as
    "set INSTAGRAM_ACCESS_TOKEN (or INSTAGRAM_PUBLISH_TOKEN) with the
    instagram_content_publish permission" to somebody who has never seen a shell.
    So `reason` is the plain one the UI prints and `detail` is the server's own,
    carried for anyone reading the API directly and never rendered.

    A connected account is not enough on its own: Instagram fetches every post
    from a public URL, so without public media hosting each Post is skipped.
    That case gets its own plain reason.
    """
    from ..config import config
    from ..services import asset_storage
    from ..services.email_service import email_configured
    from ..services.instagram_publish import publish_status

    detail: str | None = None
    reason = _NO_ACCOUNT
    try:
        can_publish, raw = publish_status()
        detail = raw or None
        if can_publish:
            reason = _NO_HOSTING
            hosted, why = asset_storage.public_hosting_configured()
            if not hosted:
                can_publish, detail = False, _hosting_detail(why)
    except Exception as exc:  # noqa: BLE001 - a status check must not 500
        can_publish, detail = False, f"the check itself failed: {str(exc)[:160]}"

    return {
        "instagram": {
            "ready": bool(can_publish),
            "handle": (config.instagram_target_username or "").lstrip("@") or None,
            # Plain, and it never quotes the server's wording.
            "reason": None if can_publish else reason,
            "detail": None if can_publish else detail,
        },
        "email": {
            "ready": bool(email_configured()),
            "to": config.reminder_email or None,
        },
    }
