"""Lazy-loaded sentence-transformers CLIP wrapper.

Loads the configured model on first use (downloads ~340MB on first run). If
`USE_CLIP=false`, encode() returns None for every input so callers can detect
the disabled state and short-circuit gracefully.

CLIP work is CPU-heavy; callers wrap it in `asyncio.to_thread` to keep the
event loop responsive.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import config

log = logging.getLogger("archivist.clip")

_model = None

# Set once the ML stack has proven unusable in THIS process, so we stop paying
# the (slow, ~340 MB) load for every subsequent call. `enabled()` is only the
# operator's intent (USE_CLIP); this is whether the intent can actually be met.
_broken = False


def enabled() -> bool:
    return config.use_clip


def _load():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info("Loading CLIP model %s (first run downloads ~340 MB)", config.clip_model)
        _model = SentenceTransformer(config.clip_model, device="cpu")
    return _model


def encode_image_paths(paths: list[Path], *, batch_size: int = 16) -> list[list[float] | None]:
    """Synchronous CLIP encode. Returns 512d vectors (or None per failed image).

    When USE_CLIP=false, returns [None] * len(paths) without loading the model.
    Never raises: a missing or mismatched ML stack yields all-None too, because
    every caller treats a None vector as "no similarity information" already.
    """
    global _broken

    if not enabled() or not paths or _broken:
        return [None] * len(paths)

    from PIL import Image
    try:
        import pillow_avif  # noqa: F401 — registers AVIF plugin
    except ImportError:
        pass

    images = []
    valid_idx: list[int] = []
    for i, p in enumerate(paths):
        try:
            images.append(Image.open(p).convert("RGB"))
            valid_idx.append(i)
        except Exception as e:
            log.warning("Image load failed %s: %s", p, e)

    if not images:
        return [None] * len(paths)

    try:
        model = _load()
        vecs = model.encode(
            images,
            batch_size=batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    except Exception:
        # torch / transformers / sentence-transformers are an UNPINNED transitive
        # stack (requirements.txt asks only for `sentence-transformers>=3.0`), so
        # a rebuilt environment can resolve a combination that explodes deep
        # inside the library rather than failing to import. One real instance:
        # `NameError: name 'nn' is not defined`, raised out of the library on a
        # B-roll run and surfaced to the user as the whole job failing (#32).
        # CLIP is an enhancement everywhere it is used — never a prerequisite —
        # so a broken stack must cost quality, not the run.
        _broken = True
        log.exception(
            "CLIP encode failed; disabling CLIP for this process "
            "(callers fall back to their no-embedding path)",
        )
        return [None] * len(paths)

    out: list[list[float] | None] = [None] * len(paths)
    for j, vi in enumerate(valid_idx):
        out[vi] = vecs[j].tolist()
    return out
