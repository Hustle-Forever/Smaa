"""Yas Producer — Video Editor category orchestration.

A multi-track NLE-style editor. Unlike the generative categories, this one does
**pure assembly/render** — it never calls Veo, Gemini, or any RAI-gated model.
The whole editable document (a timeline of tracks + clips + keyframes +
transitions) lives in `yas_generations.metadata.video_edit` as JSONB, keyed by
`mode = 'video_edit_preview'`. The frontend owns the document and autosaves it;
the backend validates + persists it and drives the export (Remotion render).

Lifecycle:
  1. `create_project(title, aspect)`  — INSERT an empty timeline row.
  2. `fetch_project(id)`              — return the doc, sources resolved to URLs.
  3. `save_timeline(id, timeline)`    — autosave: replace project/tracks/clips,
                                        bump history.rev, preserve render state.
  4. `list_sources(...)`             — pickable generated clips + uploads. (Phase B)
  5. `import_upload(file)`           — store + probe an uploaded video. (Phase B)
  6. `export_project(id)`            — flag for the export poller. (Phase B)

The router (`routers/yas_producer.py`) wraps these under
`/api/yas-producer/video-editor/*`. The export poller
(`workers/yas_video_edit_composition_poller.py`) watches the `render` sub-state.

Talking Head v1/v2 are intentionally untouched — new mode, new service, new
poller, new Remotion composition id (`video-edit`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..config import config
from .storage import first_existing
from ..db import get_conn
from ..services import asset_storage

log = logging.getLogger("archivist.yas_video_editor")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_YAS_ROOT = (config.asset_root.parent / "yas_producer").resolve()
VE_ROOT = _YAS_ROOT / "video-editor"

# Identifies THIS backend instance. The Supabase Postgres is shared across
# deployments (local Docker, the production VPS, teammates), but the rendered
# media files + the Remotion sidecar are LOCAL to each instance. So every export
# is stamped with INSTANCE_ID and the composition poller only processes renders
# it owns — otherwise a remote instance grabs an in-flight render it can't reach
# and fatally fails it ("no clip sources" / "render job lost"). Override with
# STUDIO_INSTANCE_ID; defaults to the host/container name (unique per instance).
import os as _os
import socket as _socket
INSTANCE_ID = (_os.getenv("STUDIO_INSTANCE_ID") or _socket.gethostname() or "default")[:64]

# Root mode → surfaces in the Library automatically (no child modes).
MODE_PREVIEW = "video_edit_preview"

# Mode prefixes/values of the OTHER categories, used to tag picker sources with
# a user-facing `category`. Duplicated as frozen literals (they're persisted in
# yas_generations.mode and can't change) to avoid importing those services here.
# See services/yas_talking_head{,_v2}.py MODE_* and routers/yas_producer.py.
_TH_V2_MODE_PREFIX = "talking_head_v2"
_TH_MODE_PREFIX = "talking_head"
_FINAL_MODES = ("talking_head_preview", "talking_head_v2_preview")

# yas_generations.status values (reused from the existing flows).
STATUS_READY = "ready"

# Export / composition status (lives on metadata.video_edit.render.composition_status).
COMP_DRAFT = "draft"
COMP_RENDERING = "rendering"
COMP_READY = "ready"
COMP_FAILED = "failed"

# Single source of truth for the timeline clock + canvas. FPS matches every
# existing composition; the aspect→dimensions map is mirrored in the frontend
# schema.ts so Player and the server agree exactly.
FPS = 30
SCHEMA_VERSION = 1

_ASPECT_DIMS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}
DEFAULT_ASPECT = "9:16"

# Cap the cross-session undo ring so metadata can't grow unbounded.
_MAX_CHECKPOINTS = 20

# Saved versions kept per project (yas_video_edit_revisions), which the editor
# loads under Undo when a project opens. The checkpoints above only ever held a
# rev number and a time, so closing the tab used to throw the undo history away.
_MAX_REVISIONS = 100


# ---------------------------------------------------------------------------
# Path helpers (mirror the talking-head-v2 conventions)
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    VE_ROOT.mkdir(parents=True, exist_ok=True)
    (VE_ROOT / "uploads").mkdir(parents=True, exist_ok=True)


def _backend_root() -> Path:
    return config.asset_root.parent


def _to_rel(p: Path) -> str:
    try:
        return str(p.relative_to(_backend_root()))
    except ValueError:
        return str(p)


def _abs_from_rel(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    # Local-mode uploads store backend/-relative paths; see first_existing.
    return first_existing(_backend_root() / p, config.producer_assets_root.parent.parent / p)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The Remotion service mounts backend/data at /data; headless Chrome fetches
# clip sources from there during export (the 307→Supabase redirect used for the
# browser Player does NOT work for the renderer).
# Configurable so headless Chrome (inside the Remotion container) fetches /data
# from the right host in Docker; defaults to local dev (127.0.0.1:3001).
REMOTION_DATA_PREFIX = f"{config.remotion_base_url.rstrip('/')}/data"


def _to_remotion_url(p: Path | None) -> str | None:
    if not p:
        return None
    try:
        rel = p.relative_to(_backend_root())
    except ValueError:
        log.warning("[yas-ve] export source %s is outside backend/data — Remotion can't fetch it", p)
        return None
    url_path = "/".join(quote(part) for part in rel.parts)
    return f"{REMOTION_DATA_PREFIX}/{url_path}"


# Browser-side proxy URLs the <Player> fetches (same-origin via Vite; Range-served).
def _source_play_url(kind: str, source_id: int) -> str:
    return f"/api/yas-producer/video-editor/sources/{kind}/{source_id}/video"


def _source_thumb_url(kind: str, source_id: int) -> str:
    return f"/api/yas-producer/video-editor/sources/{kind}/{source_id}/thumb"


def _still_file_url(gen_id: int) -> str:
    """Same-origin proxy for a still-image source (keyframe / card / poster)."""
    return f"/api/yas-producer/video-editor/sources/still/{gen_id}/file"


def _animation_category(mode: str | None) -> str:
    """Picker category for a yas_animations row, from its generation's mode."""
    m = mode or ""
    if m.startswith(_TH_V2_MODE_PREFIX):
        return "talking_head_v2"
    if m.startswith(_TH_MODE_PREFIX):
        return "talking_head"
    if m == MODE_PREVIEW:
        return "video_edit"
    return "poster_animation"


def _frames(duration_sec: float | int | None) -> int:
    try:
        return max(1, round(float(duration_sec or 0) * FPS))
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# ffmpeg / OpenCV media probing (imageio-ffmpeg binary is bundled — see CLAUDE.md)
# ---------------------------------------------------------------------------

def _probe_video(path: Path) -> tuple[float | None, int | None, int | None]:
    """(duration_sec, width, height) via OpenCV. Best-effort."""
    try:
        import cv2

        cap = cv2.VideoCapture(str(path))
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) or None
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or None
            dur = (float(frames) / float(fps)) if (fps > 0 and frames > 0) else None
            return dur, w, h
        finally:
            cap.release()
    except Exception as e:  # noqa: BLE001
        log.debug("[yas-ve] OpenCV probe failed: %s", e)
        return None, None, None


# Finals are probed at list time for their duration; cache per (path, mtime) so
# repeated picker opens don't re-decode unchanged MP4s.
_probe_cache: dict[str, tuple[float | None, int | None, int | None]] = {}


def _probe_video_cached(path: Path) -> tuple[float | None, int | None, int | None]:
    try:
        key = f"{path}:{path.stat().st_mtime_ns}"
    except OSError:
        return None, None, None
    hit = _probe_cache.get(key)
    if hit is None:
        hit = _probe_video(path)
        _probe_cache[key] = hit
    return hit


def _save_thumb(src: Path, dest: Path, t_sec: float = 0.5) -> bool:
    """SYNCHRONOUS and BLOCKING — up to 60s of ffmpeg. Never call this directly
    from async code; use `save_thumb_async` so the event loop stays free."""
    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [ff, "-y", "-ss", f"{t_sec:.3f}", "-i", str(src),
             "-frames:v", "1", "-q:v", "4", "-vf", "scale=320:-2", str(dest)],
            capture_output=True, timeout=60,
        )
        return proc.returncode == 0 and dest.exists()
    except Exception as e:  # noqa: BLE001
        log.debug("[yas-ve] thumb extract failed: %s", e)
        return False


# Bounded thread offload for the blocking media tools (ffmpeg subprocess, sync
# cv2). Without the offload, ONE cold video-editor thumbnail froze the single
# worker's event loop for the whole ffmpeg run — taking Dashboard, Library and
# Assets down with it. The semaphore is REQUIRED, not decorative: asyncio's
# default executor is min(32, cpu+4) threads, so ~23 cold tiles each holding a
# thread for up to 60s would starve every other to_thread user in the process.
_MEDIA_TOOL_SEM = asyncio.Semaphore(2)


async def save_thumb_async(src: Path, dest: Path, t_sec: float = 0.5) -> bool:
    """Non-blocking `_save_thumb`."""
    async with _MEDIA_TOOL_SEM:
        return await asyncio.to_thread(_save_thumb, src, dest, t_sec)


async def probe_video_async(path: Path) -> tuple[float | None, int | None, int | None]:
    """Non-blocking `_probe_video_cached` (sync cv2 open + property reads)."""
    async with _MEDIA_TOOL_SEM:
        return await asyncio.to_thread(_probe_video_cached, path)


# ---------------------------------------------------------------------------
# Timeline document
# ---------------------------------------------------------------------------

def _normalize_aspect(aspect: str | None) -> str:
    a = (aspect or DEFAULT_ASPECT).strip()
    return a if a in _ASPECT_DIMS else DEFAULT_ASPECT


def _empty_render() -> dict:
    return {
        "composition_status": COMP_DRAFT,
        "render_job_id": None,
        "composition_output_path": None,
        "composition_path": None,
        "composition_error": None,
        "rendered_rev": None,
    }


def _empty_timeline(title: str, aspect: str) -> dict:
    aspect = _normalize_aspect(aspect)
    w, h = _ASPECT_DIMS[aspect]
    return {
        "schema_version": SCHEMA_VERSION,
        "project": {
            "title": title or "Untitled project",
            "aspect": aspect,
            "width": w,
            "height": h,
            "fps": FPS,
            "background": "#000000",
            "durationInFrames": 0,
        },
        # Seed one video + one audio lane so the editor always has a drop target.
        "tracks": [
            {"id": "trk_v0", "kind": "video", "index": 0, "muted": False, "locked": False},
            {"id": "trk_a0", "kind": "audio", "index": 100, "muted": False, "locked": False},
        ],
        "clips": [],
        "history": {"rev": 0, "updated_at": _now_iso(), "checkpoints": []},
        "render": _empty_render(),
    }


def _derive_duration(timeline: dict) -> int:
    """Max (from + durationInFrames) across all clips. Transition-overlap
    subtraction is layered in at Phase E; for now this is the flat sum end."""
    end = 0
    for clip in timeline.get("clips") or []:
        try:
            e = int(clip.get("from", 0)) + int(clip.get("durationInFrames", 0))
        except (TypeError, ValueError):
            e = 0
        if e > end:
            end = e
    return max(0, end)


def _video_edit(md: dict) -> dict:
    ve = md.get("video_edit")
    if isinstance(ve, str):
        try:
            ve = json.loads(ve)
        except Exception:
            ve = None
    return ve if isinstance(ve, dict) else {}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _fetch_row(project_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, topic, mode, status, cost_usd, error,
                       metadata, created_at, updated_at
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                """,
                (project_id, MODE_PREVIEW),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"video-editor project #{project_id} not found")
    md = row.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    row["metadata"] = md
    return dict(row)


async def delete_project(project_id: int) -> dict:
    """Hard-delete a Video-Editor project and the render output it OWNS — used by
    the Library "Delete" button.

    A project is one ``yas_generations`` row (``mode=MODE_PREVIEW``) whose timeline
    lives in ``metadata.video_edit``; the exported MP4 is rendered to local disk at
    ``VE_ROOT/{project_id}/final.mp4`` and backed up to the bucket by the export
    poller (``render.composition_storage_*``). Removes the row, that backup + the
    id-scoped render dir. PRESERVED — and never touched: the bin's source clips
    (animations / uploads / assets / stills / compositions), which are independent
    records shared across projects and live under SOURCE-keyed cache dirs, not
    ``VE_ROOT/{project_id}/``. Returns ``{deleted, id}``.
    """
    import shutil

    row = await _fetch_row(project_id)  # raises 404 if not a MODE_PREVIEW row
    md = row.get("metadata") or {}
    render = (md.get("video_edit") or {}).get("render") or {}

    # Best-effort fs cleanup: unlink the recorded composition file, then drop the
    # whole id-scoped project dir (holds only this project's render output).
    for rel in (render.get("composition_path"), render.get("composition_output_path")):
        if not rel:
            continue
        try:
            p = _abs_from_rel(rel)
            if p.exists():
                p.unlink()
        except Exception:
            log.exception("[ve] composition unlink failed for #%s (continuing)", project_id)
    try:
        shutil.rmtree(VE_ROOT / str(project_id), ignore_errors=True)
    except Exception:
        log.exception("[ve] project dir cleanup failed for #%s (continuing)", project_id)

    # The bucket backup of the export (yas/compositions/video-editor/{id}.mp4)
    # would otherwise outlive the project as an orphan.
    if render.get("composition_storage_bucket") and render.get("composition_storage_path"):
        try:
            await asset_storage.delete(
                storage_bucket=render["composition_storage_bucket"],
                storage_path=render["composition_storage_path"],
                local_path=None,
            )
        except Exception:
            log.exception("[ve] composition backup delete failed for #%s (continuing)", project_id)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM yas_generations WHERE id = %s AND mode = %s RETURNING id",
                (project_id, MODE_PREVIEW),
            )
            deleted = await cur.fetchone()

    # Its saved versions go with it. A database that has never recorded one has
    # no table yet, which is not a failure.
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM yas_video_edit_revisions WHERE project_id = %s",
                    (project_id,),
                )
    except Exception:
        log.debug("[yas-ve] revision cleanup skipped for #%s", project_id, exc_info=True)

    return {"deleted": 1 if deleted else 0, "id": project_id}


async def _write_video_edit(project_id: int, timeline: dict, *, topic: str | None = None) -> None:
    """Replace the whole `video_edit` sub-object via the top-level merge.

    The frontend owns project/tracks/clips/history; `render` is read-modify-write
    by the caller before this so the poller's state is never clobbered.
    """
    patch: dict[str, Any] = {"video_edit": timeline}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            if topic is not None:
                await cur.execute(
                    """
                    UPDATE yas_generations
                       SET metadata = metadata || %s::jsonb,
                           topic = %s,
                           updated_at = NOW()
                     WHERE id = %s
                    """,
                    (Jsonb(patch), topic, project_id),
                )
            else:
                await cur.execute(
                    """
                    UPDATE yas_generations
                       SET metadata = metadata || %s::jsonb,
                           updated_at = NOW()
                     WHERE id = %s
                    """,
                    (Jsonb(patch), project_id),
                )


# ---------------------------------------------------------------------------
# Saved versions: the undo history that survives a reload
#
# One row per save in its own table, NOT inside metadata. The document is
# rewritten on every autosave and read by the export poller and the AI, so a
# hundred copies riding along in the same JSONB would be paid for on every one
# of those reads and writes.
# ---------------------------------------------------------------------------

_REVISIONS_READY = False

_REVISIONS_DDL = """
CREATE TABLE IF NOT EXISTS yas_video_edit_revisions (
    id          bigserial   PRIMARY KEY,
    project_id  bigint      NOT NULL,
    rev         integer     NOT NULL,
    actor       text,
    summary     text,
    doc_hash    text        NOT NULL,
    doc         jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, rev)
)
"""


async def _ensure_revisions_table() -> bool:
    """Create the versions table once per process (same lazy pattern as
    edit_recipes). False, logged, on failure: missing history must never fail
    an autosave."""
    global _REVISIONS_READY
    if _REVISIONS_READY:
        return True
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_REVISIONS_DDL)
        _REVISIONS_READY = True
        return True
    except Exception:
        log.exception("[yas-ve] could not ensure yas_video_edit_revisions (non-fatal)")
        return False


def _revision_doc(project: dict | None, tracks: list | None, clips: list | None) -> dict:
    """The part of a timeline a saved version restores: {project, tracks, clips}.

    `sourceUrl` is dropped from every clip. It is derived, and re-attached on
    read by _resolve_clip_sources, so storing it would only let two identical
    documents hash differently. Copies; never mutates the caller's clips."""
    return {
        "project": dict(project or {}),
        "tracks": list(tracks or []),
        "clips": [
            {k: v for k, v in c.items() if k != "sourceUrl"} if isinstance(c, dict) else c
            for c in (clips or [])
        ],
    }


def _doc_hash(doc: dict) -> str:
    """Fingerprint of a saved version that does not depend on key order."""
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def _record_revision(
    project_id: int,
    *,
    rev: int,
    doc: dict,
    actor: str,
    summary: str | None,
    previous_rev: int,
    previous_doc: dict,
) -> None:
    """Store one saved version and trim the project to its newest _MAX_REVISIONS.

    The first time a project is recorded, the document as it stood BEFORE this
    save is stored too, or the state somebody opened the project in would be
    the one place Undo could never reach after a reload. A save that changed
    nothing is skipped, so idle autosaves do not fill the history with copies.
    Best-effort: logs and returns on any failure."""
    if not await _ensure_revisions_table():
        return
    new_hash = _doc_hash(doc)
    insert = """
        INSERT INTO yas_video_edit_revisions
          (project_id, rev, actor, summary, doc_hash, doc)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (project_id, rev) DO NOTHING
    """
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT doc_hash FROM yas_video_edit_revisions "
                    "WHERE project_id = %s ORDER BY rev DESC LIMIT 1",
                    (project_id,),
                )
                latest = await cur.fetchone()
                if latest is None:
                    prev_hash = _doc_hash(previous_doc)
                    if previous_rev < rev and prev_hash != new_hash:
                        await cur.execute(insert, (
                            project_id, previous_rev, "baseline",
                            "As the project was before history was kept",
                            prev_hash, Jsonb(previous_doc),
                        ))
                elif latest["doc_hash"] == new_hash:
                    return
                await cur.execute(insert, (
                    project_id, rev, actor, (summary or "")[:400] or None,
                    new_hash, Jsonb(doc),
                ))
                await cur.execute(
                    """
                    DELETE FROM yas_video_edit_revisions
                     WHERE project_id = %s
                       AND rev < (SELECT rev FROM yas_video_edit_revisions
                                   WHERE project_id = %s
                                   ORDER BY rev DESC OFFSET %s LIMIT 1)
                    """,
                    (project_id, project_id, _MAX_REVISIONS - 1),
                )
    except Exception:
        log.warning("[yas-ve] revision capture failed for #%d", project_id, exc_info=True)


async def list_revisions(project_id: int, *, limit: int = _MAX_REVISIONS) -> dict:
    """A project's saved versions, oldest first, each ready for the editor to
    apply: {rev, at, actor, summary, project, tracks, clips} with clip sources
    resolved. `items` is empty when nothing was recorded yet."""
    await _fetch_row(project_id)  # 404 when it is not a Video Editor project
    limit = max(1, min(int(limit or _MAX_REVISIONS), _MAX_REVISIONS))
    if not await _ensure_revisions_table():
        return {"items": []}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT rev, actor, summary, created_at, doc
                  FROM yas_video_edit_revisions
                 WHERE project_id = %s
                 ORDER BY rev DESC
                 LIMIT %s
                """,
                (project_id, limit),
            )
            rows = await cur.fetchall()
    items: list[dict] = []
    for r in reversed(rows):
        doc = r["doc"]
        if isinstance(doc, str):
            doc = json.loads(doc)
        resolved = await _resolve_clip_sources({"clips": doc.get("clips") or []})
        items.append({
            "rev": int(r["rev"]),
            "at": r["created_at"].isoformat() if r.get("created_at") else None,
            "actor": r.get("actor"),
            "summary": r.get("summary"),
            "project": doc.get("project") or {},
            "tracks": doc.get("tracks") or [],
            "clips": resolved["clips"],
        })
    return {"items": items}


# ---------------------------------------------------------------------------
# Per-project memory: clip provenance + append-only edit log
#
# Stored at the SIBLING key metadata.video_edit_memory so it never clobbers the
# timeline (video_edit) or the analysis cache (video_edit_ai) — the shallow `||`
# merge replaces only the key we patch. Silent / AI-only: never shown in the UI,
# fed to the editor AI (via build_project_digest) so edits stay accurate and
# continuous. Provenance = "what each clip IS"; edit_log = "what's been done".
# ---------------------------------------------------------------------------

_MAX_EDIT_LOG = 500


def _video_edit_memory(md: dict) -> dict:
    mem = md.get("video_edit_memory")
    if isinstance(mem, str):
        try:
            mem = json.loads(mem)
        except Exception:
            mem = None
    return mem if isinstance(mem, dict) else {}


async def _resolve_provenance(kind: str | None, sid: int | None) -> dict:
    """Best-effort 'birth certificate' for a source clip — the creative intent
    that produced it: TH script hook/dialogue, animation/poster prompt, topic,
    bookend role. Never raises; returns {source_mode, creative:{}} (possibly
    empty) so a memory miss never blocks a create/save."""
    creative: dict[str, Any] = {}
    source_mode: str | None = None
    if not kind or sid is None:
        return {"source_mode": None, "creative": {}}
    try:
        if kind == "animation":
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT a.prompt_used, a.metadata->>'role' AS role,
                               g.topic, g.mode, g.metadata AS g_meta
                          FROM yas_animations a
                          JOIN yas_generations g ON g.id = a.yas_generation_id
                         WHERE a.id = %s
                        """,
                        (sid,),
                    )
                    r = await cur.fetchone()
            if r:
                source_mode = r.get("mode")
                if r.get("topic"):
                    creative["topic"] = r["topic"]
                if r.get("prompt_used"):
                    creative["prompt"] = str(r["prompt_used"])[:600]
                if r.get("role"):
                    creative["role"] = r["role"]
                g_meta = r.get("g_meta") or {}
                if isinstance(g_meta, str):
                    try:
                        g_meta = json.loads(g_meta)
                    except Exception:
                        g_meta = {}
                script = g_meta.get("script") if isinstance(g_meta, dict) else None
                if isinstance(script, dict):
                    if script.get("hook"):
                        creative["script_hook"] = str(script["hook"])[:300]
                    if script.get("dialogue"):
                        creative["script_dialogue"] = str(script["dialogue"])[:1200]
        elif kind in ("still", "composition"):
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT topic, prompt_used, mode FROM yas_generations WHERE id = %s",
                        (sid,),
                    )
                    r = await cur.fetchone()
            if r:
                source_mode = r.get("mode")
                if r.get("topic"):
                    creative["topic"] = r["topic"]
                if r.get("prompt_used"):
                    creative["prompt"] = str(r["prompt_used"])[:600]
        # upload: no creative text — provenance is just the source pointer.
    except Exception:
        log.debug("[yas-ve] provenance resolve failed for %s:%s", kind, sid, exc_info=True)
    return {"source_mode": source_mode, "creative": creative}


async def _provenance_entry(clip: dict, *, rev: int) -> dict:
    """Build one provenance row for a clip (resolving its creative metadata)."""
    kind, sid = _clip_source_kind_id(clip)
    prov = await _resolve_provenance(kind, sid)
    return {
        "clip_id": clip.get("id"),
        "source_kind": kind,
        "source_id": sid,
        "source_mode": prov["source_mode"],
        "entered_at_rev": rev,
        "entered_at": _now_iso(),
        "creative": prov["creative"],
    }


_TRACKED_CLIP_FIELDS = (
    "from", "durationInFrames", "trimBefore", "trimAfter", "muted",
    "speed", "opacity", "volume", "fit", "trackId",
)


def _summarize_clip_change(old: dict, new: dict) -> str:
    """Short human-readable diff of one clip across a save — what the user/AI
    actually changed (for the edit log)."""
    bits: list[str] = []
    for f in _TRACKED_CLIP_FIELDS:
        if old.get(f) != new.get(f):
            bits.append(f"{f} {old.get(f)}->{new.get(f)}")
    # Nested: text font, scale keyframes, filters.
    of = (old.get("text") or {}).get("fontFamily")
    nf = (new.get("text") or {}).get("fontFamily")
    if of != nf:
        bits.append(f"font {of!r}->{nf!r}")
    if (old.get("keyframes") or {}).get("scale") != (new.get("keyframes") or {}).get("scale"):
        bits.append("scale")
    if (old.get("filters") or {}) != (new.get("filters") or {}):
        bits.append("filters")
    return ", ".join(bits) if bits else "edited"


async def _diff_edit_log(
    existing_clips: list, new_clips: list, *, rev: int,
    actor: str, change_summary: str | None,
) -> tuple[list[dict], list[dict]]:
    """Diff old vs new clips → (edit_log_entries, provenance_for_new_clips).
    Returns ([], []) when nothing meaningful changed (skips no-op autosaves)."""
    old = {c.get("id"): c for c in (existing_clips or []) if c.get("id")}
    new = {c.get("id"): c for c in (new_clips or []) if c.get("id")}
    added = [cid for cid in new if cid not in old]
    deleted = [cid for cid in old if cid not in new]
    mutated = [cid for cid in (set(new) & set(old)) if old[cid] != new[cid]]

    if not (added or deleted or mutated or change_summary):
        return [], []

    changes: list[dict] = []
    for cid in added:
        changes.append({"op": "add_clip", "clip_id": cid,
                        "kind": new[cid].get("kind")})
    for cid in deleted:
        changes.append({"op": "delete_clip", "clip_id": cid})
    for cid in mutated:
        changes.append({"op": "edit_clip", "clip_id": cid,
                        "change": _summarize_clip_change(old[cid], new[cid])})

    if change_summary:
        summary = change_summary
    else:
        parts = []
        if added:
            parts.append(f"added {len(added)}")
        if deleted:
            parts.append(f"deleted {len(deleted)}")
        if mutated:
            parts.append("; ".join(
                f"{cid}: {_summarize_clip_change(old[cid], new[cid])}" for cid in mutated[:4]
            ))
        summary = " · ".join(p for p in parts if p) or "edited"

    entry = {"rev": rev, "at": _now_iso(), "actor": actor,
             "summary": summary[:400], "ops": changes[:40]}

    # New clips get provenance rows too (manual drag-add path).
    provenance = [await _provenance_entry(new[cid], rev=rev) for cid in added]
    return [entry], provenance


async def append_memory(
    project_id: int,
    *,
    provenance: list[dict] | None = None,
    edit_log_entries: list[dict] | None = None,
) -> None:
    """Append to metadata.video_edit_memory (sibling key). Read-modify-write;
    single-user editor → last-write-wins (same stance as save_timeline). Never
    clobbers video_edit / video_edit_ai (shallow `||` patches only this key).
    Best-effort — a memory failure must never break the edit."""
    if not provenance and not edit_log_entries:
        return
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT metadata->'video_edit_memory' AS mem "
                    "FROM yas_generations WHERE id = %s AND mode = %s",
                    (project_id, MODE_PREVIEW),
                )
                row = await cur.fetchone()
                mem = (row or {}).get("mem") or {}
                if isinstance(mem, str):
                    try:
                        mem = json.loads(mem)
                    except Exception:
                        mem = {}
                if not isinstance(mem, dict):
                    mem = {}
                prov = list(mem.get("provenance") or [])
                elog = list(mem.get("edit_log") or [])
                if provenance:
                    prov.extend(provenance)
                if edit_log_entries:
                    elog.extend(edit_log_entries)
                new_mem = {"provenance": prov, "edit_log": elog[-_MAX_EDIT_LOG:]}
                await cur.execute(
                    """
                    UPDATE yas_generations
                       SET metadata = metadata || %s::jsonb, updated_at = NOW()
                     WHERE id = %s
                    """,
                    (Jsonb({"video_edit_memory": new_mem}), project_id),
                )
    except Exception:
        log.warning("[yas-ve] append_memory failed for #%d", project_id, exc_info=True)


# ---------------------------------------------------------------------------
# Source resolution (fleshed out in Phase B)
# ---------------------------------------------------------------------------

def _clip_source_kind_id(clip: dict) -> tuple[str | None, int | None]:
    src = clip.get("source") or {}
    kind = src.get("kind")
    if kind == "animation":
        return "animation", src.get("animation_id")
    if kind == "upload":
        return "upload", src.get("upload_id")
    if kind == "composition":
        return "composition", src.get("composition_id")
    if kind == "asset":
        return "asset", src.get("asset_id")
    if kind == "still":
        return "still", src.get("still_id")
    return None, None


# Visual layer kinds that have NO media source (rendered from props alone). The
# export poller's "0 resolvable sources" abort must NOT count these — a text- or
# shape-only timeline is perfectly renderable.
_SOURCELESS_KINDS = {"text", "shape"}


def _clip_needs_source(clip: dict) -> bool:
    """True if the clip renders from a fetched media file (video/audio/image)."""
    kind = (clip.get("kind") or "video")
    return kind not in _SOURCELESS_KINDS


def clip_is_renderable(clip: dict) -> bool:
    """A clip contributes pixels/audio to the export if it's sourceless (text/
    shape) OR its media source resolved to a fetchable URL. Used by the poller
    to decide whether an export has anything to render."""
    if not _clip_needs_source(clip):
        return True
    return bool(clip.get("sourceUrl"))


async def _resolve_clip_sources(timeline: dict) -> dict:
    """Attach a browser-playable `sourceUrl` to every clip for the <Player>.

    Points at the same-origin proxy route (Range-served), which the browser
    fetches directly. The EXPORT path (build_render_props) re-resolves to a
    `/data` filesystem URL for headless Chrome instead.
    """
    for clip in timeline.get("clips") or []:
        kind, sid = _clip_source_kind_id(clip)
        if not kind or sid is None:
            continue
        src = clip.get("source") or {}
        if kind == "asset":
            # Image/sticker assets stream from the producer-assets file route.
            clip["sourceUrl"] = f"/api/agents/producer/assets/{int(sid)}/file"
        elif kind == "still":
            # Keyframe/card/poster images stream from the still-image proxy.
            clip["sourceUrl"] = _still_file_url(int(sid))
        elif src.get("audioFile"):
            # Music/audio upload → mime-correct audio route (for <Audio>).
            clip["sourceUrl"] = _audio_play_url(int(sid))
        else:
            clip["sourceUrl"] = _source_play_url(kind, int(sid))
    return timeline


def library_thumb_source(md: dict) -> str | None:
    """Best-available IMAGE thumbnail for a NOT-yet-exported project's Library
    tile: the first renderable VISUAL clip's still/asset image, or a JPG
    first-frame thumbnail for a video source. Returns None when the timeline has
    no resolvable visual source (text/shape/audio-only). Lightweight — derives a
    URL from the stored timeline, no DB hydration; the thumb JPG (for video
    sources) is generated lazily by the /thumb route on first GET.
    """
    timeline = _video_edit(md)
    clips = timeline.get("clips") or []
    # Visually-first clip = lowest `from` among the video/image layers.
    visual = [c for c in clips if (c.get("kind") or "video") in ("video", "image")]
    for clip in sorted(visual, key=lambda c: int(c.get("from") or 0)):
        kind, sid = _clip_source_kind_id(clip)
        if not kind or sid is None:
            continue
        if kind == "still":
            return _still_file_url(int(sid))
        if kind == "asset":
            return f"/api/agents/producer/assets/{int(sid)}/file"
        if kind in ("animation", "upload", "composition"):
            return _source_thumb_url(kind, int(sid))
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _project_payload(row: dict, timeline: dict) -> dict:
    """The shape the frontend store hydrates from."""
    proj = timeline.get("project") or {}
    render = timeline.get("render") or _empty_render()
    history = timeline.get("history") or {"rev": 0}
    return {
        "id": row["id"],
        "title": proj.get("title") or row.get("topic") or "Untitled project",
        "status": row.get("status"),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        "schema_version": timeline.get("schema_version", SCHEMA_VERSION),
        "project": proj,
        "tracks": timeline.get("tracks") or [],
        "clips": timeline.get("clips") or [],
        "history": {"rev": history.get("rev", 0), "updated_at": history.get("updated_at")},
        "render": render,
    }


async def create_project(*, title: str, aspect: str, preset: Optional[str] = None) -> dict:
    if preset not in (None, "b_roll"):
        raise HTTPException(400, f"unknown project preset {preset!r} (expected 'b_roll')")
    _ensure_dirs()
    title = (title or "Untitled project").strip()[:200] or "Untitled project"
    aspect = _normalize_aspect(aspect)
    timeline = _empty_timeline(title, aspect)
    if preset == "b_roll":
        # Opt-in B-roll auto-edit preset — a flag INSIDE project so
        # save_timeline's `dict(project or {})` rebuild preserves it.
        timeline["project"]["preset"] = "b_roll"
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_generations
                  (topic, mode, prompt_used, status, metadata)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, created_at, updated_at
                """,
                (
                    title, MODE_PREVIEW, title, STATUS_READY,
                    Jsonb({"video_edit": timeline}),
                ),
            )
            new = await cur.fetchone()
            project_id = int(new["id"])
    log.info("[yas-ve] created project #%d (title=%r, aspect=%s)", project_id, title[:60], aspect)
    return await fetch_project(project_id)


async def create_project_from_sources(
    *, title: str, aspect: str, sources: list[dict], preset: str | None = None,
) -> dict:
    """Create a NEW editor project pre-seeded with the given video sources, both
    in the Media bin AND laid end-to-end on the first video track, in order.

    `sources` is an ordered list of:
        {"kind": "animation"|"upload", "id": int,
         "duration_frames": int, "title": str}

    Used by the "Open in editor" bridge (e.g. Talking Head v2 → editor). The clip
    shape mirrors store.ts::addClipFromSource so the frontend hydrates cleanly;
    `sourceUrl` is attached by fetch_project's _resolve_clip_sources.

    `preset` tags the timeline's project (e.g. "general_video") so the Library
    surfaces it as the originating Ideation tool's tile and resume routes back to
    the right creator. Leave None for a plain Video Editor project.
    """
    _ensure_dirs()
    title = (title or "Untitled project").strip()[:200] or "Untitled project"
    aspect = _normalize_aspect(aspect)
    timeline = _empty_timeline(title, aspect)
    if preset:
        timeline["project"]["preset"] = preset

    media_bin: list[dict] = []
    clips: list[dict] = []
    cursor = 0
    for s in sources:
        kind = "upload" if s.get("kind") == "upload" else "animation"
        sid = int(s["id"])
        durf = max(1, int(s.get("duration_frames") or 1))
        clip_title = (s.get("title") or f"{kind} #{sid}")
        media_bin.append({
            "kind": kind, "id": sid,
            "title": clip_title, "durationInFrames": durf,
        })
        source: dict[str, Any] = {
            "kind": kind,
            "naturalDurationInFrames": durf,
        }
        source["animation_id" if kind == "animation" else "upload_id"] = sid
        clips.append({
            "id": f"clip_{secrets.token_hex(5)}",
            "trackId": "trk_v0",
            "kind": "video",
            "source": source,
            "trimBefore": 0,
            "trimAfter": durf,
            "from": cursor,
            "durationInFrames": durf,
            "keyframes": {},
            "muted": False,
            "fit": "cover",
        })
        cursor += durf

    timeline["project"]["media_bin"] = media_bin
    timeline["project"]["durationInFrames"] = cursor
    timeline["clips"] = clips

    # Capture provenance for every seeded clip — the "birth certificate" (TH
    # script, animation prompt, topic, role). Best-effort; a resolve miss just
    # yields an empty creative block.
    provenance = [await _provenance_entry(c, rev=0) for c in clips]

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_generations
                  (topic, mode, prompt_used, status, metadata)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    title, MODE_PREVIEW, title, STATUS_READY,
                    Jsonb({
                        "video_edit": timeline,
                        "video_edit_memory": {"provenance": provenance, "edit_log": []},
                    }),
                ),
            )
            new = await cur.fetchone()
            project_id = int(new["id"])
    log.info(
        "[yas-ve] created project #%d from %d source(s) (title=%r, aspect=%s)",
        project_id, len(sources), title[:60], aspect,
    )
    return await fetch_project(project_id)


async def fetch_project(project_id: int) -> dict:
    row = await _fetch_row(project_id)
    timeline = _video_edit(row["metadata"])
    if not timeline:
        # Legacy/empty row — synthesize a fresh doc so the editor still opens.
        timeline = _empty_timeline(row.get("topic") or "Untitled project", DEFAULT_ASPECT)
    # Self-heal: a competing backend on the shared Supabase DB can mark a render
    # 'failed' even though THIS instance's Remotion actually produced the file.
    # If the composition file is on disk, it's really ready → fix it.
    render = timeline.get("render") or {}
    if render.get("composition_status") == COMP_FAILED:
        out = VE_ROOT / str(project_id) / "final.mp4"
        if out.exists() and out.stat().st_size > 0:
            rel = _to_rel(out)
            render.update({
                "composition_status": COMP_READY,
                "composition_path": rel,
                "composition_error": None,
            })
            timeline["render"] = render
            await _patch_render(project_id, {
                "composition_status": COMP_READY,
                "composition_path": rel,
                "composition_error": None,
            })
    timeline = await _resolve_clip_sources(timeline)
    return _project_payload(row, timeline)


async def save_timeline(
    project_id: int,
    *,
    project: dict,
    tracks: list,
    clips: list,
    base_rev: Optional[int] = None,
    actor: str = "manual",
    change_summary: Optional[str] = None,
) -> dict:
    """Autosave the editable document. Validates aspect/dimensions, bumps
    history.rev, appends a capped checkpoint, and PRESERVES the backend-owned
    `render` sub-state. This is the leave-&-resume / edit-history mechanism.

    Also appends to the per-project memory edit log (diffing clips vs. the prior
    save). `actor` distinguishes manual edits from AI ones; AI callers (apply_plan)
    pass `actor='ai'` + a `change_summary` describing the applied ops.
    """
    row = await _fetch_row(project_id)
    existing = _video_edit(row["metadata"]) or _empty_timeline(
        row.get("topic") or "Untitled project", DEFAULT_ASPECT
    )
    stored_rev = int((existing.get("history") or {}).get("rev", 0))

    # Single-user editor → last-write-wins. `base_rev` is accepted for API
    # compatibility but NOT enforced: rapid autosaves can legitimately lag the
    # stored rev (a save in flight while the next edit fires), and 409-ing those
    # would spuriously fail the autosave. (Multi-tab conflict is a non-goal.)
    _ = base_rev

    # Normalize the project block: trust aspect, recompute canonical dimensions.
    aspect = _normalize_aspect((project or {}).get("aspect"))
    w, h = _ASPECT_DIMS[aspect]
    title = ((project or {}).get("title") or row.get("topic") or "Untitled project").strip()[:200]
    # Preserve any extra project keys the frontend stores (e.g. media_bin) — start
    # from the incoming project, then normalize the canonical fields.
    new_project = dict(project or {})
    new_project.update({
        "title": title or "Untitled project",
        "aspect": aspect,
        "width": w,
        "height": h,
        "fps": FPS,
        "background": (project or {}).get("background") or "#000000",
        "durationInFrames": 0,  # set below
    })

    new_rev = stored_rev + 1
    checkpoints = list((existing.get("history") or {}).get("checkpoints") or [])
    checkpoints.append({"rev": new_rev, "at": _now_iso()})
    checkpoints = checkpoints[-_MAX_CHECKPOINTS:]

    timeline = {
        "schema_version": SCHEMA_VERSION,
        "project": new_project,
        "tracks": tracks or [],
        "clips": clips or [],
        "history": {"rev": new_rev, "updated_at": _now_iso(), "checkpoints": checkpoints},
        # PRESERVE render — owned by export_project + the poller.
        "render": existing.get("render") or _empty_render(),
    }
    timeline["project"]["durationInFrames"] = _derive_duration(timeline)

    # Keep the row's topic in sync with the project title so the Library tile
    # + resume label stay correct.
    new_topic = title if title and title != row.get("topic") else None
    await _write_video_edit(project_id, timeline, topic=new_topic)

    # Per-project memory: diff against the prior save → edit-log entry + provenance
    # for any newly-added clips. Best-effort; never blocks the autosave.
    log_entries: list[dict] = []
    try:
        log_entries, prov = await _diff_edit_log(
            existing.get("clips") or [], clips or [],
            rev=new_rev, actor=actor, change_summary=change_summary,
        )
        if log_entries or prov:
            await append_memory(project_id, provenance=prov or None,
                                edit_log_entries=log_entries or None)
    except Exception:
        log.warning("[yas-ve] edit-log capture failed for #%d", project_id, exc_info=True)

    # The saved version Undo walks back through after a reload. Best-effort too.
    await _record_revision(
        project_id,
        rev=new_rev,
        doc=_revision_doc(timeline["project"], timeline["tracks"], timeline["clips"]),
        actor=actor,
        summary=change_summary or (log_entries[0].get("summary") if log_entries else None),
        previous_rev=stored_rev,
        previous_doc=_revision_doc(
            existing.get("project"), existing.get("tracks"), existing.get("clips"),
        ),
    )

    fresh = await _fetch_row(project_id)
    return _project_payload(fresh, await _resolve_clip_sources(timeline))


# ---------------------------------------------------------------------------
# Phase B — source library, upload, serving, export
# ---------------------------------------------------------------------------

async def _fetch_animation_row(anim_id: int) -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status, local_path, storage_bucket, storage_path,
                       duration_sec, with_audio, width, height, mime_type
                  FROM yas_animations WHERE id = %s
                """,
                (anim_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def _fetch_upload_row(upload_id: int) -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, token, local_path, mime_type, bytes, width, height,
                       duration_sec, thumb_path, created_at
                  FROM yas_uploads WHERE id = %s
                """,
                (upload_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_sources(
    *,
    q: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Pickable sources for the media bin, each tagged with a `category`:

      animations    → kind='animation', category in {'talking_head_v2',
                      'talking_head', 'poster_animation', 'video_edit'}
                      (derived from the joined yas_generations.mode); intro/
                      outro bookends (metadata.role) override to
                      'bookend_intro' / 'bookend_outro' with a role-suffixed
                      title — covers TH v2 AND editor-generated bookends
      finals        → kind='composition', category='final' — stitched MP4s of
                      composition-ready th / th_v2 / video-edit runs
      stills        → kind='still', category in {'still', 'poster',
                      'bookend_intro', 'bookend_outro'} — keyframe / card
                      images (cards from *_intro / *_outro modes get the
                      bookend categories) and poster image generations
      uploads       → kind='upload', category='upload'

    `kind` in {None,'all','animation','upload','composition','still'}; `q`
    filters by title/topic. Best-effort: a final with no MP4 on disk is simply
    not listed.
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    like = f"%{q.strip()}%" if (q and q.strip()) else None
    want_anim = kind in (None, "all", "animation")
    want_upload = kind in (None, "all", "upload")
    want_final = kind in (None, "all", "composition", "final")
    want_still = kind in (None, "all", "still")
    items: list[dict] = []

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            if want_anim:
                await cur.execute(
                    """
                    SELECT a.id, a.duration_sec, a.with_audio, a.width, a.height,
                           a.created_at, a.metadata->>'role' AS role, g.topic, g.mode
                      FROM yas_animations a
                      JOIN yas_generations g ON g.id = a.yas_generation_id
                     WHERE a.status = 'ready'
                       AND (%s::text IS NULL OR g.topic ILIKE %s::text)
                     ORDER BY a.created_at DESC
                     LIMIT 500
                    """,
                    (like, like),
                )
                for r in await cur.fetchall():
                    aid = int(r["id"])
                    title = r.get("topic") or f"Clip #{aid}"
                    category = _animation_category(r.get("mode"))
                    # Intro/outro bookends (TH v2 runs AND editor-generated ones
                    # whose parent mode is video_edit_preview) get their own
                    # picker category + a role suffix on the title.
                    role = (r.get("role") or "").strip().lower()
                    if role in ("intro", "outro"):
                        category = f"bookend_{role}"
                        title = f"{title} — {role}"
                    items.append({
                        "kind": "animation",
                        "id": aid,
                        "category": category,
                        "title": title,
                        "durationInFrames": _frames(r.get("duration_sec")),
                        "naturalDurationInFrames": _frames(r.get("duration_sec")),
                        "with_audio": bool(r.get("with_audio")),
                        "width": r.get("width"),
                        "height": r.get("height"),
                        "url": _source_play_url("animation", aid),
                        "thumbnail_url": _source_thumb_url("animation", aid),
                        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    })

            if want_final:
                # Stitched compositions: th / th_v2 keep the render state flat on
                # metadata; video-edit nests it under metadata.video_edit.render.
                await cur.execute(
                    """
                    SELECT id, topic, created_at,
                           COALESCE(metadata->>'composition_path',
                                    metadata->'video_edit'->'render'->>'composition_path')
                               AS comp_rel
                      FROM yas_generations
                     WHERE ((mode IN (%s, %s) AND metadata->>'composition_status' = 'ready')
                         OR (mode = %s
                             AND metadata->'video_edit'->'render'->>'composition_status' = 'ready'))
                       AND (%s::text IS NULL OR topic ILIKE %s::text)
                     ORDER BY created_at DESC
                     LIMIT 500
                    """,
                    (*_FINAL_MODES, MODE_PREVIEW, like, like),
                )
                for r in await cur.fetchall():
                    gid = int(r["id"])
                    comp_rel = r.get("comp_rel")
                    if not comp_rel:
                        continue
                    p = _abs_from_rel(comp_rel)
                    if not p.exists():
                        continue  # best-effort: no MP4 on disk → not pickable
                    dur, w, h = await probe_video_async(p)
                    items.append({
                        "kind": "composition",
                        "id": gid,
                        "category": "final",
                        "title": r.get("topic") or f"Final #{gid}",
                        "durationInFrames": _frames(dur),
                        "naturalDurationInFrames": _frames(dur),
                        "with_audio": True,
                        "width": w,
                        "height": h,
                        "url": _source_play_url("composition", gid),
                        "thumbnail_url": _source_thumb_url("composition", gid),
                        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    })

            if want_still:
                # Image generations: talking-head keyframes / cards ('still')
                # plus poster image runs ('poster'). All live on yas_generations
                # with the image at local_path / the bucket.
                await cur.execute(
                    """
                    SELECT id, topic, mode, created_at
                      FROM yas_generations
                     WHERE status IN ('ready', 'saved_to_assets')
                       AND (local_path IS NOT NULL OR storage_path IS NOT NULL)
                       AND (mode IS NULL OR mode <> %s)
                       AND (%s::text IS NULL OR topic ILIKE %s::text)
                     ORDER BY created_at DESC
                     LIMIT 500
                    """,
                    (MODE_PREVIEW, like, like),
                )
                for r in await cur.fetchall():
                    gid = int(r["id"])
                    mode = r.get("mode") or ""
                    topic = r.get("topic") or f"Image #{gid}"
                    if mode.startswith(_TH_MODE_PREFIX):
                        # Intro/outro cards (TH v2 + editor bookends share the
                        # *_intro / *_outro modes) get their own category so the
                        # picker can group them.
                        if mode.endswith("_intro"):
                            category = "bookend_intro"
                            role = "intro card"
                        elif mode.endswith("_outro"):
                            category = "bookend_outro"
                            role = "outro card"
                        else:
                            category = "still"
                            role = (
                                "scene keyframe" if mode.endswith("_scene")
                                else "keyframe"
                            )
                        title = f"{topic} — {role}"
                    else:
                        category = "poster"
                        title = topic
                    items.append({
                        "kind": "still",
                        "id": gid,
                        "category": category,
                        "title": title,
                        # Images default to 3s overlays (mirrors store.addImageClip).
                        "durationInFrames": 90,
                        "naturalDurationInFrames": 90,
                        "with_audio": False,
                        "width": None,
                        "height": None,
                        "url": _still_file_url(gid),
                        "thumbnail_url": _still_file_url(gid),
                        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    })

            if want_upload:
                await cur.execute(
                    """
                    SELECT id, mime_type, duration_sec, width, height, created_at
                      FROM yas_uploads
                     WHERE mime_type LIKE 'video/%%'
                     ORDER BY created_at DESC
                     LIMIT 500
                    """
                )
                for r in await cur.fetchall():
                    uid = int(r["id"])
                    items.append({
                        "kind": "upload",
                        "id": uid,
                        "category": "upload",
                        "title": f"Upload #{uid}",
                        "durationInFrames": _frames(r.get("duration_sec")),
                        "naturalDurationInFrames": _frames(r.get("duration_sec")),
                        "with_audio": True,
                        "width": r.get("width"),
                        "height": r.get("height"),
                        "url": _source_play_url("upload", uid),
                        "thumbnail_url": _source_thumb_url("upload", uid),
                        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    })

    items.sort(key=lambda it: it.get("created_at") or "", reverse=True)
    total = len(items)
    page = items[offset:offset + limit]
    return {"items": page, "total": total, "limit": limit, "offset": offset}


async def import_upload(*, raw: bytes, filename: Optional[str], mime: Optional[str]) -> dict:
    """Store an uploaded video under VE_ROOT/uploads, probe + thumbnail it, and
    record a yas_uploads row. Returns a source descriptor for the picker."""
    _ensure_dirs()
    if not raw:
        raise HTTPException(400, "empty file")
    mime = (mime or mimetypes.guess_type(filename or "")[0] or "").lower()
    if not mime.startswith("video/"):
        suffix = Path(filename or "").suffix.lower()
        if suffix in {".mp4", ".mov", ".webm", ".m4v", ".mkv"}:
            mime = "video/mp4" if suffix in {".mp4", ".m4v"} else f"video/{suffix.lstrip('.')}"
        else:
            raise HTTPException(400, f"unsupported file type {mime!r}; upload an MP4/MOV/WebM")

    token = secrets.token_urlsafe(12)
    ext = Path(filename or "").suffix.lstrip(".").lower() or "mp4"
    dest = VE_ROOT / "uploads" / f"{token}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)

    dur, w, h = _probe_video(dest)
    thumb_rel: Optional[str] = None
    thumb = VE_ROOT / "uploads" / f"{token}.jpg"
    if await save_thumb_async(dest, thumb):
        thumb_rel = _to_rel(thumb)

    rel = _to_rel(dest)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_uploads
                  (token, local_path, mime_type, bytes, width, height, duration_sec, thumb_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (token, rel, mime, len(raw), w, h, dur, thumb_rel),
            )
            new = await cur.fetchone()
            upload_id = int(new["id"])

    log.info("[yas-ve] imported upload #%d (%s, %.1fs)", upload_id, filename, dur or 0)
    return {
        "kind": "upload",
        "id": upload_id,
        "category": "upload",
        "title": filename or f"Upload #{upload_id}",
        "durationInFrames": _frames(dur),
        "naturalDurationInFrames": _frames(dur),
        "with_audio": True,
        "width": w,
        "height": h,
        "url": _source_play_url("upload", upload_id),
        "thumbnail_url": _source_thumb_url("upload", upload_id),
        "created_at": new["created_at"].isoformat() if new.get("created_at") else None,
    }


# ---------------------------------------------------------------------------
# Audio / music uploads (separate from the video source bin)
# ---------------------------------------------------------------------------

_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".flac", ".opus", ".weba"}


def _probe_audio_duration(path: Path) -> float | None:
    """Audio duration (sec) by parsing ffmpeg's stderr 'Duration:' line."""
    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        proc = subprocess.run([ff, "-i", str(path)], capture_output=True, timeout=60)
        err = (proc.stderr or b"").decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception as e:  # noqa: BLE001
        log.debug("[yas-ve] audio probe failed: %s", e)
    return None


def _audio_play_url(upload_id: int) -> str:
    return f"/api/yas-producer/video-editor/assets/audio/{upload_id}/file"


async def import_audio(*, raw: bytes, filename: Optional[str], mime: Optional[str]) -> dict:
    """Store an uploaded audio/music file under VE_ROOT/uploads, probe duration,
    and record a yas_uploads row. Returns a descriptor for the Audio tab."""
    _ensure_dirs()
    if not raw:
        raise HTTPException(400, "empty file")
    mime = (mime or mimetypes.guess_type(filename or "")[0] or "").lower()
    suffix = Path(filename or "").suffix.lower()
    if not mime.startswith("audio/"):
        if suffix in _AUDIO_EXTS:
            mime = f"audio/{suffix.lstrip('.')}"
        else:
            raise HTTPException(400, f"unsupported audio type {mime!r}; upload MP3/WAV/M4A/OGG")

    token = secrets.token_urlsafe(12)
    ext = suffix.lstrip(".") or "mp3"
    dest = VE_ROOT / "uploads" / f"{token}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)

    dur = _probe_audio_duration(dest)
    rel = _to_rel(dest)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_uploads
                  (token, local_path, mime_type, bytes, width, height, duration_sec, thumb_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (token, rel, mime, len(raw), None, None, dur, None),
            )
            new = await cur.fetchone()
            upload_id = int(new["id"])

    log.info("[yas-ve] imported audio #%d (%s, %.1fs)", upload_id, filename, dur or 0)
    return {
        "id": upload_id,
        "title": filename or f"Audio #{upload_id}",
        "durationInFrames": _frames(dur),
        "url": _audio_play_url(upload_id),
        "created_at": new["created_at"].isoformat() if new.get("created_at") else None,
    }


async def list_audio(*, limit: int = 200) -> dict:
    """Uploaded audio/music files for the Audio tab."""
    limit = max(1, min(int(limit), 500))
    items: list[dict] = []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, mime_type, duration_sec, created_at
                  FROM yas_uploads
                 WHERE mime_type LIKE 'audio/%%'
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            for r in await cur.fetchall():
                uid = int(r["id"])
                items.append({
                    "id": uid,
                    "title": f"Audio #{uid}",
                    "durationInFrames": _frames(r.get("duration_sec")),
                    "url": _audio_play_url(uid),
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                })
    return {"items": items}


async def resolve_audio_serving(upload_id: int) -> Optional[dict]:
    """{'path': Path, 'mime': str} for the browser <Audio> proxy."""
    row = await _fetch_upload_row(int(upload_id))
    if not row or not row.get("local_path"):
        return None
    p = _abs_from_rel(row["local_path"])
    if not p.exists():
        return None
    return {"path": p, "mime": row.get("mime_type") or "audio/mpeg"}


async def _download_to(url: str, dest: Path) -> bool:
    # Runs server side, so callers sign with url_for(..., internal=True): the
    # public /media origin would hairpin through the public IP (or, in a local
    # stack, not resolve from inside the container at all).
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                if r.status_code >= 300:
                    return False
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes(65536):
                        f.write(chunk)
        return dest.exists() and dest.stat().st_size > 0
    except Exception:  # noqa: BLE001
        log.exception("[yas-ve] download failed: %s", url)
        return False


async def ensure_animation_local(anim_id: int) -> Optional[Path]:
    """Local file for a Veo animation, downloading from the bucket + caching
    under VE_ROOT/sources when only the bucket copy survives."""
    row = await _fetch_animation_row(anim_id)
    if not row:
        return None
    if row.get("local_path"):
        p = _abs_from_rel(row["local_path"])
        if p.exists():
            return p
    if row.get("storage_bucket") and row.get("storage_path"):
        cache = VE_ROOT / "sources" / f"anim-{anim_id}.mp4"
        if cache.exists() and cache.stat().st_size > 0:
            return cache
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=row["storage_path"],
            local_path=None,
            expires=3600,
            internal=True,
        )
        if signed and await _download_to(signed, cache):
            return cache
    return None


async def ensure_upload_local(upload_id: int) -> Optional[Path]:
    row = await _fetch_upload_row(upload_id)
    if not row or not row.get("local_path"):
        return None
    p = _abs_from_rel(row["local_path"])
    return p if p.exists() else None


async def ensure_asset_local(asset_id: int) -> Optional[Path]:
    """Local copy of an Assets-Library image/sticker, downloading from the
    bucket + caching under VE_ROOT/assets for headless export."""
    row = await _fetch_asset_row(asset_id)
    if not row:
        return None
    if row.get("local_path"):
        p = asset_storage.local_to_abs(row["local_path"])
        if p.exists():
            return p
    if row.get("storage_bucket") and row.get("storage_path"):
        ext = Path(row["storage_path"]).suffix or ".png"
        cache = VE_ROOT / "assets" / f"asset-{asset_id}{ext}"
        if cache.exists() and cache.stat().st_size > 0:
            return cache
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=row["storage_path"],
            local_path=None,
            expires=3600,
            internal=True,
        )
        if signed and await _download_to(signed, cache):
            return cache
    return None


async def ensure_composition_local(generation_id: int) -> Optional[Path]:
    """Local stitched-composition MP4 for a 'final' source (a composition-ready
    th / th_v2 / video-edit run). Compositions are rendered locally by the
    pollers and never uploaded to the bucket, so this is a metadata-path
    resolve only (best-effort: missing file → None)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COALESCE(metadata->>'composition_path',
                                metadata->'video_edit'->'render'->>'composition_path')
                           AS comp_rel
                  FROM yas_generations WHERE id = %s
                """,
                (generation_id,),
            )
            row = await cur.fetchone()
    if not row or not row.get("comp_rel"):
        return None
    p = _abs_from_rel(row["comp_rel"])
    return p if p.exists() else None


async def _fetch_still_row(gen_id: int) -> dict | None:
    # autocommit: read-only — saves the BEGIN + COMMIT round trips.
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, local_path, storage_bucket, storage_path, mime_type
                  FROM yas_generations WHERE id = %s
                """,
                (gen_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def ensure_still_local(
    gen_id: int, *, row: dict | None = None,
) -> Optional[Path]:
    """Local keyframe/card/poster image for a 'still' source, downloading from
    the bucket + caching under VE_ROOT/sources when only the bucket copy
    survives (mirrors ensure_animation_local).

    `row` lets a caller that ALREADY fetched the row pass it in rather than
    forcing a second identical SELECT — resolve_still_serving did exactly that,
    paying two remote round trips to serve one image. Must carry local_path,
    storage_bucket, storage_path.
    """
    if row is None:
        row = await _fetch_still_row(gen_id)
    if not row:
        return None
    if row.get("local_path"):
        p = _abs_from_rel(row["local_path"])
        if p.exists():
            return p
    if row.get("storage_bucket") and row.get("storage_path"):
        ext = Path(row["storage_path"]).suffix or ".png"
        cache = VE_ROOT / "sources" / f"still-{gen_id}{ext}"
        if cache.exists() and cache.stat().st_size > 0:
            return cache
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=row["storage_path"],
            local_path=None,
            expires=3600,
            internal=True,
        )
        if signed and await _download_to(signed, cache):
            return cache
    return None


async def resolve_still_serving(gen_id: int) -> Optional[dict]:
    """{'path': Path, 'mime': str} for the still-image proxy route."""
    row = await _fetch_still_row(int(gen_id))
    if not row:
        return None
    # Pass the row we already have: ensure_still_local would otherwise re-SELECT
    # the same columns, doubling this route's DB round trips.
    p = await ensure_still_local(int(gen_id), row=row)
    if not p:
        return None
    mime = row.get("mime_type") or mimetypes.guess_type(p.name)[0] or "image/png"
    return {"path": p, "mime": mime}


async def resolve_source_local(kind: str, source_id: int) -> Optional[Path]:
    if kind == "animation":
        return await ensure_animation_local(int(source_id))
    if kind == "upload":
        return await ensure_upload_local(int(source_id))
    if kind == "asset":
        return await ensure_asset_local(int(source_id))
    if kind == "composition":
        return await ensure_composition_local(int(source_id))
    if kind == "still":
        return await ensure_still_local(int(source_id))
    return None


async def resolve_source_serving(kind: str, source_id: int) -> Optional[dict]:
    """For the browser Player proxy. {'path': Path} for a local file, or
    {'redirect': signed_url} for a bucket-only animation (Range-friendly)."""
    if kind == "animation":
        row = await _fetch_animation_row(int(source_id))
        if not row:
            return None
        if row.get("local_path"):
            p = _abs_from_rel(row["local_path"])
            if p.exists():
                return {"path": p}
        if row.get("storage_bucket") and row.get("storage_path"):
            signed = await asset_storage.url_for(
                storage_bucket=row["storage_bucket"],
                storage_path=row["storage_path"],
                local_path=None,
                expires=3600,
            )
            if signed:
                return {"redirect": signed}
        return None
    if kind == "upload":
        p = await ensure_upload_local(int(source_id))
        return {"path": p} if p else None
    if kind == "composition":
        # Stitched MP4s live on local disk only (no bucket copy).
        p = await ensure_composition_local(int(source_id))
        return {"path": p} if p else None
    return None


async def resolve_source_thumb(kind: str, source_id: int) -> Optional[Path]:
    """A cached JPG thumbnail for the picker tile (first ~0.5s frame)."""
    _ensure_dirs()
    thumbs = VE_ROOT / "thumbs"
    if kind == "upload":
        row = await _fetch_upload_row(int(source_id))
        if row and row.get("thumb_path"):
            p = _abs_from_rel(row["thumb_path"])
            if p.exists():
                return p
        local = await ensure_upload_local(int(source_id))
        if not local:
            return None
        dest = thumbs / f"upload-{source_id}.jpg"
        if dest.exists():
            return dest
        return dest if await save_thumb_async(local, dest) else None
    if kind == "animation":
        dest = thumbs / f"anim-{source_id}.jpg"
        if dest.exists():
            return dest
        local = await ensure_animation_local(int(source_id))
        if not local:
            return None
        return dest if await save_thumb_async(local, dest) else None
    if kind == "composition":
        dest = thumbs / f"comp-{source_id}.jpg"
        if dest.exists():
            return dest
        local = await ensure_composition_local(int(source_id))
        if not local:
            return None
        return dest if await save_thumb_async(local, dest) else None
    return None


# ---------------------------------------------------------------------------
# Brand fonts (Assets Library, asset_kind='font') — for text-layer rendering
# ---------------------------------------------------------------------------

# A text clip's brand font is stored as fontUrl = the producer-assets file route.
# For headless export we must re-resolve that to a /data URL (Chrome can't follow
# the signed-redirect). This regex pulls the asset id back out of the URL.
_FONT_ASSET_URL_RE = re.compile(r"/agents/producer/assets/(\d+)/file")


async def _fetch_asset_row(asset_id: int) -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, asset_kind, storage_bucket, storage_path, local_path,
                       mime_type, slug
                  FROM producer_assets WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_fonts() -> dict:
    """Brand fonts from the Assets Library (asset_kind='font'). The Text tab
    merges these with the curated Google fonts baked into the composition."""
    items: list[dict] = []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, slug, display_name
                  FROM producer_assets
                 WHERE asset_kind = 'font' AND enabled = TRUE
                 ORDER BY COALESCE(display_name, slug) ASC
                """
            )
            for r in await cur.fetchall():
                aid = int(r["id"])
                items.append({
                    "id": aid,
                    "family": r.get("display_name") or r.get("slug") or f"Font {aid}",
                    "file_url": f"/api/agents/producer/assets/{aid}/file",
                    "preview_url": f"/api/agents/producer/assets/{aid}/preview",
                })
    return {"items": items}


def _font_ext(row: dict) -> str:
    mime = (row.get("mime_type") or "").lower()
    if "woff2" in mime:
        return ".woff2"
    if "woff" in mime:
        return ".woff"
    if "otf" in mime or "opentype" in mime:
        return ".otf"
    for key in ("storage_path", "slug"):
        suf = Path(row.get(key) or "").suffix.lower()
        if suf in {".ttf", ".otf", ".woff", ".woff2"}:
            return suf
    return ".ttf"


async def ensure_font_local(asset_id: int) -> Optional[Path]:
    """Local copy of a brand font file, downloading from the bucket + caching
    under VE_ROOT/fonts so headless Chrome can fetch it during export."""
    row = await _fetch_asset_row(asset_id)
    if not row or row.get("asset_kind") != "font":
        return None
    if row.get("local_path"):
        p = asset_storage.local_to_abs(row["local_path"])
        if p.exists():
            return p
    if row.get("storage_bucket") and row.get("storage_path"):
        cache = VE_ROOT / "fonts" / f"font-{asset_id}{_font_ext(row)}"
        if cache.exists() and cache.stat().st_size > 0:
            return cache
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=row["storage_path"],
            local_path=None,
            expires=3600,
            internal=True,
        )
        if signed and await _download_to(signed, cache):
            return cache
    return None


async def _resolve_text_font(clip: dict) -> None:
    """Rewrite a text clip's brand-font URL to a /data URL for the export (no-op
    for curated Google fonts, which carry no fontUrl)."""
    text = clip.get("text") or {}
    url = text.get("fontUrl")
    if not url:
        return
    m = _FONT_ASSET_URL_RE.search(str(url))
    if not m:
        return
    local = await ensure_font_local(int(m.group(1)))
    if local:
        text = dict(text)
        text["fontUrl"] = _to_remotion_url(local)
        clip["text"] = text


async def _patch_render(project_id: int, patch: dict) -> None:
    """Read-modify-write the nested render sub-object via jsonb_set so the
    poller's state never clobbers project/tracks/clips/history (the shallow
    `||` merge would)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT metadata FROM yas_generations WHERE id = %s AND mode = %s",
                (project_id, MODE_PREVIEW),
            )
            row = await cur.fetchone()
            if not row:
                return
            md = row["metadata"] or {}
            ve = md.get("video_edit") or {}
            render = ve.get("render") or _empty_render()
            render.update(patch)
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = jsonb_set(metadata, '{video_edit,render}', %s::jsonb, true),
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (Jsonb(render), project_id),
            )


async def export_project(project_id: int) -> dict:
    """Flag the project for export. The poller picks up rendering rows, builds
    the props, and submits a Remotion render."""
    row = await _fetch_row(project_id)
    timeline = _video_edit(row["metadata"]) or {}
    if not (timeline.get("clips") or []):
        raise HTTPException(400, "nothing to export — add a clip to the timeline first")
    rev = int((timeline.get("history") or {}).get("rev", 0))
    await _patch_render(project_id, {
        "composition_status": COMP_RENDERING,
        "render_job_id": None,
        "composition_error": None,
        "rendered_rev": rev,
        "render_owner": INSTANCE_ID,  # only THIS instance's poller renders it
    })
    fresh = await _fetch_row(project_id)
    return _project_payload(fresh, await _resolve_clip_sources(_video_edit(fresh["metadata"])))


async def build_render_props(project_id: int) -> tuple[dict, Path]:
    """Build the `video-edit` composition props (sources resolved to /data URLs
    for headless Chrome) + the output MP4 path. Used by the export poller."""
    row = await _fetch_row(project_id)
    timeline = _video_edit(row["metadata"]) or {}
    clips = [dict(c) for c in (timeline.get("clips") or [])]
    for clip in clips:
        # Sourceless layers (text/shape) render from props alone — no fetch.
        if not _clip_needs_source(clip):
            if (clip.get("kind") or "video") == "text":
                await _resolve_text_font(clip)
            continue
        kind, sid = _clip_source_kind_id(clip)
        local = await resolve_source_local(kind, int(sid)) if (kind and sid is not None) else None
        clip["sourceUrl"] = _to_remotion_url(local) if local else None
    props = {
        "project": timeline.get("project") or {},
        "tracks": timeline.get("tracks") or [],
        "clips": clips,
    }
    out_path = VE_ROOT / str(project_id) / "final.mp4"
    return props, out_path


async def composition_locator(project_id: int) -> dict:
    """Resolve a READY export to its servable sources for the file route.

    Returns ``{"local": Path|None, "storage_bucket": str|None,
    "storage_path": str|None}``. Local disk is only a transient cache — the
    durable copy lives in the Supabase bucket (uploaded by the composition
    poller when the render completes). The route streams local when present,
    else redirects to a signed bucket URL, else 410 — mirroring the Veo route
    so a wiped disk (redeploy) can't 410 a finished video. Raises 409 when the
    export isn't ready yet."""
    row = await _fetch_row(project_id)
    ve = _video_edit(row["metadata"]) or {}
    render = ve.get("render") or {}
    if render.get("composition_status") != COMP_READY:
        raise HTTPException(409, "export not ready")
    rel = render.get("composition_path")
    local: Path | None = None
    if rel:
        p = _abs_from_rel(rel)
        if p.exists():
            local = p
    return {
        "local": local,
        "storage_bucket": render.get("composition_storage_bucket"),
        "storage_path": render.get("composition_storage_path"),
    }
