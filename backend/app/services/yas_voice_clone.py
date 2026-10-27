"""Yas Producer — Video Editor: the single app-wide CLONED VOICE.

"Clone my voice" keeps exactly ONE ElevenLabs instant-voice-clone slot busy.
The clone is stored in a one-row table (``ve_voice_clone``, id always 1);
replacing it deletes the previous ElevenLabs voice first so slots never
accumulate, and clearing it frees the remote slot too.

The table is created lazily once per process (CREATE TABLE IF NOT EXISTS),
mirroring the ``_ensure_*`` DDL pattern in yas_talking_head_v2.py — no
migration machinery exists in this project.

Used by:
  * routers/yas_producer.py        — POST/GET/DELETE /video-editor/ai/voice-clone
  * services/yas_video_edit_ai.py  — start_job resolves use_cloned → voice_id;
    _voice_op clears a stale clone when ElevenLabs reports it gone mid-STS.
"""
from __future__ import annotations

import logging

from ..db import get_conn
from .media_providers import ProviderError, get_voice_provider

log = logging.getLogger("archivist.yas_voice_clone")

# Lazy one-shot DDL flag (per process).
_TABLE_READY = False


async def _ensure_table() -> None:
    """Idempotently create ve_voice_clone. Runs the DDL once per process."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ve_voice_clone (
                    id         int PRIMARY KEY DEFAULT 1,
                    voice_id   text,
                    name       text,
                    created_at timestamptz DEFAULT now()
                )
                """
            )
    _TABLE_READY = True


async def get_cloned_voice() -> dict | None:
    """The stored clone as {"voice_id", "name", "created_at"} — or None."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT voice_id, name, created_at FROM ve_voice_clone WHERE id = 1"
            )
            row = await cur.fetchone()
    if not row or not row.get("voice_id"):
        return None
    return {
        "voice_id": str(row["voice_id"]),
        "name": row.get("name") or "My voice",
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


async def set_cloned_voice(voice_id: str, name: str) -> dict:
    """Store the (already-created) clone as THE app-wide voice.

    Replace-on-set: if a different clone is already stored, its ElevenLabs
    voice is deleted first so only one slot is ever used. The remote cleanup
    is best-effort — the new clone already exists and must never be lost over
    a failed delete of the old one.
    """
    await _ensure_table()
    old = await get_cloned_voice()
    if old and old["voice_id"] != voice_id:
        try:
            await get_voice_provider().delete_voice(old["voice_id"])
        except ProviderError:
            log.exception(
                "[voice-clone] could not delete old voice %s (storing new one anyway)",
                old["voice_id"],
            )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO ve_voice_clone (id, voice_id, name, created_at)
                VALUES (1, %s, %s, now())
                ON CONFLICT (id) DO UPDATE
                   SET voice_id = EXCLUDED.voice_id,
                       name     = EXCLUDED.name,
                       created_at = now()
                """,
                (voice_id, name),
            )
    log.info("[voice-clone] stored clone voice_id=%s name=%r", voice_id, name)
    return await get_cloned_voice() or {
        "voice_id": voice_id, "name": name, "created_at": None,
    }


async def clear_cloned_voice() -> None:
    """Forget the clone and free its ElevenLabs slot. The remote delete is
    best-effort (an already-gone voice is fine; a missing key just logs)."""
    await _ensure_table()
    old = await get_cloned_voice()
    if old:
        try:
            await get_voice_provider().delete_voice(old["voice_id"])
        except ProviderError:
            log.exception(
                "[voice-clone] remote delete failed for %s (clearing locally anyway)",
                old["voice_id"],
            )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM ve_voice_clone WHERE id = 1")
    log.info("[voice-clone] cleared (was %s)", old["voice_id"] if old else "empty")
