"""Stage keys and the run's manifest — what a retry may reuse.

Every costly step of a harness run (above all the paid Omni call per chunk)
is recorded with a KEY: a hash of exactly what went into it — the chunk's
bytes, the prompt, the target image's bytes, the model, and the TAKE. A
retry is handed its parent's manifest and reuses any output whose key is
unchanged and whose file is still on disk; anything else is made again.

The take is what makes "generate again" a new Omni call even when nothing
changed: a fresh version starts its own take (its experiment id), and only
a retry that keeps a step's take can reuse it.

A key needs its files: when one is missing (a faked chunk in a test, a file
cleaned off disk) the step gets no key and is never reused.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

_DIGESTS: dict[tuple[str, int, int], str] = {}


def digest(path: Path) -> Optional[str]:
    """sha256 of a file's bytes (first 20 hex), memoised by path, size and
    mtime; None when there is no such file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    memo = (str(Path(path).resolve()), st.st_size, st.st_mtime_ns)
    if memo not in _DIGESTS:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        _DIGESTS[memo] = h.hexdigest()[:20]
    return _DIGESTS[memo]


def key(**parts: Any) -> Optional[str]:
    """The key of one step: its parts, files by their bytes. None when a
    file part is missing."""
    def norm(v: Any) -> Any:
        if isinstance(v, Path):
            d = digest(v)
            if d is None:
                raise FileNotFoundError(v)
            return {"file": d}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        return v

    try:
        body = json.dumps({k: norm(v) for k, v in parts.items()}, sort_keys=True,
                          separators=(",", ":"), default=str)
    except FileNotFoundError:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]


class Manifest:
    """The steps of one run: {stage: {item: {key, reused, seconds, path}}}.
    `prior` is the parent version's manifest (retries); empty for a fresh
    run."""

    def __init__(self, prior: Optional[dict] = None):
        self.stages: dict[str, dict[str, dict]] = {}
        self.prior = prior or {}

    def reusable(self, stage: str, item: str, k: Optional[str]) -> Optional[Path]:
        """The parent's output for this step when its key matches and the
        file is still there."""
        if not k:
            return None
        rec = (self.prior.get(stage) or {}).get(item) or {}
        path = rec.get("path")
        if rec.get("key") == k and path and Path(path).exists():
            return Path(path)
        return None

    def record(self, stage: str, item: str, k: Optional[str], *, path: Optional[Path] = None,
               reused: bool = False, seconds: float = 0.0, **extra: Any) -> None:
        self.stages.setdefault(stage, {})[item] = {
            "key": k, "reused": reused, "seconds": round(seconds, 2),
            "path": str(path) if path else None, **extra,
        }

    def public(self) -> dict:
        return json.loads(json.dumps(self.stages, default=str))

    def counts(self) -> dict:
        """{stage: [made, reused]} — for the run's summary line."""
        return {s: [sum(1 for r in items.values() if not r["reused"]),
                    sum(1 for r in items.values() if r["reused"])]
                for s, items in self.stages.items()}
