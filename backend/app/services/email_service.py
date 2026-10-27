"""
Reminder email sender for the Producer Roadmap (Step 4 Calendar).

SMTP via aiosmtplib, FULLY GATED: if SMTP isn't configured (no host) or there's
no recipient, ``send_email`` logs and returns False — scheduling still works, the
email is just skipped. No exceptions escape to the caller.
"""
from __future__ import annotations

import logging
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python <3.9 fallback (not expected here)
    ZoneInfo = None  # type: ignore

from ..config import config

log = logging.getLogger("archivist.email")


def _coerce_dt(when) -> Optional[datetime]:
    """Best-effort parse of a scheduled_at value into an aware datetime.

    Postgres timestamptz comes back as an aware ``datetime``; ISO strings are
    parsed. A naive datetime is assumed UTC. Returns None if unparseable."""
    dt: Optional[datetime] = None
    if isinstance(when, datetime):
        dt = when
    elif isinstance(when, str) and when.strip():
        try:
            dt = datetime.fromisoformat(when.strip())
        except Exception:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_when(when, tzname: str) -> str:
    """Human-friendly stamp in the post's own timezone, e.g.
    'Wednesday, 2 July 2026 · 4:30 PM (Asia/Dubai)'. Falls back to the raw value
    if it can't be parsed/converted."""
    dt = _coerce_dt(when)
    if dt is None:
        return str(when)
    label = (tzname or "Asia/Dubai").strip()
    local = dt
    if ZoneInfo is not None:
        try:
            local = dt.astimezone(ZoneInfo(label))
        except Exception:
            label = ""
    else:
        label = ""
    try:
        stamp = local.strftime("%A, %-d %B %Y · %-I:%M %p")
    except ValueError:  # Windows lacks the %-d / %-I no-pad flags
        stamp = local.strftime("%A, %d %B %Y · %I:%M %p")
    return f"{stamp} ({label})" if label else stamp


def relative_phrase(when) -> str:
    """A rough 'in about N minutes/hours/days' relative to now (UTC), or 'now'
    when the scheduled time has effectively arrived/passed."""
    dt = _coerce_dt(when)
    if dt is None:
        return "shortly"
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 60:
        return "now"
    mins = round(secs / 60)
    if mins < 60:
        return f"in about {mins} minute{'s' if mins != 1 else ''}"
    hours = secs / 3600
    if hours < 24:
        h = round(hours)
        return f"in about {h} hour{'s' if h != 1 else ''}"
    days = round(hours / 24)
    return f"in about {days} day{'s' if days != 1 else ''}"

_tls_ctx: ssl.SSLContext | None = None


def _tls_context() -> ssl.SSLContext:
    """An SSL context backed by certifi's CA bundle.

    Fixes the common macOS Python issue where the interpreter has no access to
    the system root certificates, so verifying Gmail's TLS cert fails with
    ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`` before
    auth is even attempted. Falls back to the default context if certifi is
    somehow unavailable.
    """
    global _tls_ctx
    if _tls_ctx is None:
        try:
            import certifi

            _tls_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _tls_ctx = ssl.create_default_context()
    return _tls_ctx


def email_configured() -> bool:
    """True when we can actually send a reminder (SMTP host + recipient set)."""
    return bool(config.smtp_host and config.reminder_email)


async def send_email(*, to: str, subject: str, text: str, html: Optional[str] = None) -> bool:
    """Send one email. Returns True on success, False if unconfigured/failed."""
    if not config.smtp_host:
        log.info("[email] SMTP not configured — skipping %r to %s", subject, to)
        return False
    if not to:
        log.info("[email] no recipient — skipping %r", subject)
        return False
    try:
        import aiosmtplib
    except Exception:
        log.warning("[email] aiosmtplib not installed — skipping email (pip install aiosmtplib)")
        return False

    msg = EmailMessage()
    msg["From"] = config.smtp_from or config.smtp_user or "42studio@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    # Port 465 = implicit TLS (SSL on connect); 587/25 = plain connect then
    # STARTTLS upgrade. Pick the right one so either Gmail config works.
    use_implicit_tls = config.smtp_port == 465
    try:
        await aiosmtplib.send(
            msg,
            hostname=config.smtp_host,
            port=config.smtp_port,
            username=config.smtp_user or None,
            password=config.smtp_password or None,
            use_tls=use_implicit_tls,
            start_tls=(config.smtp_use_tls and not use_implicit_tls),
            tls_context=_tls_context(),
        )
        log.info("[email] sent %r to %s", subject, to)
        return True
    except Exception:
        log.exception("[email] send failed (%r to %s)", subject, to)
        return False


def build_reminder(post: dict) -> tuple[str, str, str]:
    """Compose (subject, text, html) for a scheduled-post reminder email.

    Wording adapts to how many reminders we've already sent: the first is a
    heads-up ("coming up"); any later one is a follow-up ("still not posted").
    """
    tool = (post.get("tool") or "post").replace("_", " ")
    when = post.get("scheduled_at")
    tzname = post.get("timezone") or "Asia/Dubai"
    when_str = format_when(when, tzname)
    rel = relative_phrase(when)
    caption = (post.get("caption") or "").strip()
    link = f"{config.app_base_url}/#library/scheduled"
    is_followup = int(post.get("reminder_count") or 0) >= 1

    if is_followup:
        subject = f"⏰ Reminder: your {tool} still needs to be posted"
        lead_text = (
            f"You haven't posted yet — your scheduled {tool} was due to publish "
            f"({when_str}). Approve & post it before it slips."
        )
        lead_html = (
            f"<p><b>You haven't posted yet.</b> Your scheduled <b>{tool}</b> was due "
            f"to publish (<b>{when_str}</b>) — approve &amp; post it before it slips.</p>"
        )
    else:
        # "in about 20 minutes" for the subject; "now" reads oddly there.
        subject_when = "soon" if rel == "now" else rel
        subject = f"⏰ Your {tool} posts {subject_when} — review & approve"
        lead_text = (
            f"Heads up — your scheduled {tool} is set to publish at {when_str} "
            f"({rel})."
        )
        lead_html = (
            f"<p>Heads up — your scheduled <b>{tool}</b> is set to publish at "
            f"<b>{when_str}</b> ({rel}).</p>"
        )

    text = (
        f"{lead_text}\n\n"
        f"Caption:\n{caption}\n\n"
        f"Open 42 Studio to review and approve & post:\n{link}\n"
    )
    html = (
        f"{lead_html}"
        f"<p><b>Caption</b><br><pre style='white-space:pre-wrap;font-family:inherit'>{caption}</pre></p>"
        f"<p><a href='{link}' style='background:#06B6A2;color:#fff;padding:10px 16px;"
        f"border-radius:8px;text-decoration:none;display:inline-block'>"
        f"Open 42 Studio → Approve &amp; Post</a></p>"
    )
    return subject, text, html
