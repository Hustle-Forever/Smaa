"""Shared helpers usable by multiple agent packages.

Keep this small — only put genuinely cross-agent helpers here. Agent-specific
logic stays under each agent's package.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable

from ..services.storage import resolve_local


def resolve_storage_path(storage_path: str) -> Path:
    """brand_documents.storage_path uses the same DB convention as
    assets.local_path — relative to backend/data/ (i.e. 'uploads/<uuid>.md').
    Re-use the same resolver so the convention stays single-sourced."""
    return resolve_local(storage_path)


async def materialize_doc(row: dict) -> tuple[Path, Callable[[], None]]:
    """Give a brand_documents row's file a local path for the file-based
    extractors, plus a cleanup callback.

    • Supabase-stored (storage_bucket set) → download to an OS temp file and
      return (temp_path, delete_temp). The temp is NOT a durable local copy.
    • Legacy local path → resolve it and return (path, no-op).

    Always call the returned cleanup in a finally block.
    """
    bucket = row.get("storage_bucket")
    sp = row.get("storage_path")
    if bucket and sp:
        from ..services import asset_storage  # local import avoids any cycle

        data = await asset_storage.download_bytes(storage_bucket=bucket, storage_path=str(sp))
        fd, tmp_name = tempfile.mkstemp(suffix=Path(str(sp)).suffix or "", prefix="branddoc_")
        try:
            with open(fd, "wb") as f:
                f.write(data)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        tmp_path = Path(tmp_name)

        def _cleanup() -> None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

        return tmp_path, _cleanup

    return resolve_storage_path(str(sp)), (lambda: None)
