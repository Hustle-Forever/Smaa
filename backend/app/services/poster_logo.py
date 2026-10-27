"""
Composite the flat white 42 logo onto a rendered poster (the OpenAI path).

The Nano Banana (Gemini) path bakes the logo in-render via an image part. The
OpenAI gpt-image-1 path is told NOT to draw a logo and leaves the top-right
corner clean; this module stamps the real white logomark there afterward so it
stays crisp and consistently placed — mirroring the original poster-only design.

Pure PIL byte-manipulation: no DB, no network, no config/env reads. Importable
and testable standalone (`tests/test_poster_diversity.py`).
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path


def composite_logo_png(
    scene_bytes: bytes,
    logo_path: Path | None,
    *,
    width_frac: float = 0.18,
    margin_frac: float = 0.04,
) -> bytes:
    """Stamp the logo into the TOP-RIGHT corner of a rendered scene.

    `width_frac`  — logo width as a fraction of the scene width.
    `margin_frac` — inset from the top/right edges as a fraction of width.

    Returns PNG bytes. If `logo_path` is missing/None the scene is returned
    unchanged (re-encoded to PNG).
    """
    from PIL import Image

    scene = Image.open(BytesIO(scene_bytes)).convert("RGBA")
    if not logo_path or not Path(logo_path).exists():
        out = BytesIO()
        scene.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    logo = Image.open(logo_path).convert("RGBA")
    w, _h = scene.size
    target_w = max(1, int(w * width_frac))
    ratio = target_w / logo.width
    logo = logo.resize((target_w, max(1, int(logo.height * ratio))), Image.LANCZOS)
    margin = int(w * margin_frac)
    pos = (w - logo.width - margin, margin)  # top-right
    scene.alpha_composite(logo, pos)
    out = BytesIO()
    scene.convert("RGB").save(out, format="PNG")
    return out.getvalue()
