"""Local asset storage layout + path resolution.

Layout (under backend/data/):
  assets/<asset_type>/<urlhash[:2]>/<urlhash>.<ext>
  frames/<asset_id>/<frame_idx>.jpg

The DB stores paths *relative to backend/* so the data dir can be moved as
a unit. _resolve_local() converts those back to absolute Paths.
"""
from __future__ import annotations

from pathlib import Path

from ..config import config


PROJECT_ROOT = config.asset_root.parent  # backend/


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def first_existing(primary: Path, fallback: Path) -> Path:
    """`primary` if it exists, else `fallback` if IT exists, else `primary`.

    Stored local paths use two bases, backend/ and backend/data, depending on
    which code wrote them, and each reader assumes one. Trying the other base
    only when the expected file is missing fixes those reads without moving a
    path that already resolves, and without changing where a new file is
    written: a path that exists nowhere still resolves to the reader's own base.
    """
    if primary.exists() or not fallback.exists():
        return primary
    return fallback


def asset_local_path(asset_type: str, url_hash: str, ext: str) -> Path:
    """Where a downloaded asset of this type with this hash lives."""
    sub = url_hash[:2]
    base = config.asset_root / asset_type / sub
    ensure_dir(base)
    return base / f"{url_hash}.{ext.lstrip('.')}"


def frames_dir(asset_id: int) -> Path:
    d = config.frames_root / str(asset_id)
    ensure_dir(d)
    return d


def resolve_local(path_str: str) -> Path:
    """DB stores paths relative to backend/. Return absolute Path.
    Normalises Windows backslashes so cross-platform paths work."""
    p = Path(path_str.replace("\\", "/"))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def relativise(path: Path) -> str:
    """Convert an absolute Path back to a project-relative string for DB storage.
    Falls back to the absolute string if path is outside the project tree."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)
