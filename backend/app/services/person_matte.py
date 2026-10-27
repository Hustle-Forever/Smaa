"""Where the speaker is in a frame — so a pattern can sit BEHIND them.

The owner wants the Omni pattern as part of the background: big, running in
from the left edge to the middle of the frame, and the speaker standing in
front of it. The reference frame Omni copies therefore has to show the
pattern already occluded by the person, which needs a person mask.

The mask comes from the local MODNet matte (speaker_front — the same one
that later puts the speaker back in front of Omni's output, ~1 s a frame).
When that cannot run, Gemini 2.5 Flash's segmentation output is the fallback
(a JSON list of {box_2d, mask: base64 PNG, label}). Gemini 3 dropped
segmentation, hence the pinned 2.5 model.

Everything here degrades: a failed call, an unparsable answer or no person
at all returns None, and the caller lays the pattern without a cut-out.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("archivist.rebrand.person_matte")

MATTE_MODEL = os.getenv("PERSON_MATTE_MODEL", "gemini-2.5-flash")
MATTE_TIMEOUT_SEC = float(os.getenv("PERSON_MATTE_TIMEOUT_SEC", "120"))

_PROMPT = (
    "Give the segmentation mask for every person in this image — the whole person: "
    "hair, head covering, face, body, clothes and hands. Output a JSON list of "
    'segmentation masks where each entry contains the 2D bounding box in the key "box_2d", '
    'the segmentation mask in key "mask", and the text label in the key "label".'
)


def parse_masks(text: str, width: int, height: int):
    """Gemini's segmentation answer → one float32 HxW mask in 0..1 (the union
    of every entry), or None when it holds no usable mask.

    Each box_2d is [y0, x0, y1, x1] on a 0-1000 grid, and each mask is a PNG
    (optionally a data: URL) covering just that box, so it is resized into
    its box and pasted."""
    import numpy as np
    from PIL import Image

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text or "", re.S)
    body = fenced.group(1) if fenced else (text or "")
    try:
        items = json.loads(body)
    except (TypeError, ValueError):
        return None
    if isinstance(items, dict):
        items = [items]
    full = np.zeros((height, width), dtype="float32")
    found = False
    for it in items if isinstance(items, list) else []:
        try:
            y0, x0, y1, x1 = (float(v) for v in it["box_2d"])
            png = str(it["mask"]).split(",", 1)[-1]
            mask = Image.open(io.BytesIO(base64.b64decode(png))).convert("L")
        except Exception:  # noqa: BLE001 — one bad entry must not sink the rest
            continue
        top, bottom = int(round(y0 / 1000 * height)), int(round(y1 / 1000 * height))
        left, right = int(round(x0 / 1000 * width)), int(round(x1 / 1000 * width))
        top, left = max(0, top), max(0, left)
        bottom, right = min(height, bottom), min(width, right)
        if bottom <= top or right <= left:
            continue
        m = np.asarray(mask.resize((right - left, bottom - top), Image.BILINEAR),
                       dtype="float32") / 255.0
        full[top:bottom, left:right] = np.maximum(full[top:bottom, left:right], m)
        found = True
    return full if found and full.max() > 0 else None


def _local_mask(frame: Path) -> Optional[Any]:
    """speaker_front's MODNet matte for a still, or None when it cannot run
    (no onnxruntime, no model download) or finds no one."""
    try:
        import cv2

        from . import speaker_front

        bgr = cv2.imread(str(frame), cv2.IMREAD_COLOR)
        if bgr is None:
            return None
        m = speaker_front.person_matte(bgr)
        return m if float((m > 0.5).mean()) > 0.01 else None
    except Exception as e:  # noqa: BLE001 — Gemini is the fallback
        log.warning("[person_matte] local matte unavailable for %s: %s", frame.name, str(e)[:200])
        return None


async def local_mask(frame: Path) -> Optional[Any]:
    """person_mask with the LOCAL matte only — never the paid Gemini
    fallback. The Rebrand studio's previews use it: a preview is free, and a
    machine that cannot run MODNet would otherwise pay Gemini per preview.
    Reads and writes the same `<frame stem>.person.png` cache."""
    import asyncio

    import numpy as np
    from PIL import Image

    frame = Path(frame)
    cache = frame.with_name(f"{frame.stem}.person.png")
    try:
        if cache.exists() and cache.stat().st_mtime >= frame.stat().st_mtime:
            with Image.open(cache) as c:
                return np.asarray(c.convert("L"), dtype="float32") / 255.0
        local = await asyncio.to_thread(_local_mask, frame)
        if local is None:
            return None
        Image.fromarray(np.clip(np.rint(local * 255), 0, 255).astype("uint8")).save(cache)
        log.info("[person_matte] %s: person covers %.0f%% of the frame (local matte)",
                 frame.name, 100 * float((local > 0.5).mean()))
        return local
    except Exception as e:  # noqa: BLE001 — no mask means no cut-out, never no preview
        log.warning("[person_matte] local mask for %s failed: %s", frame.name, str(e)[:200])
        return None


async def person_mask(frame: Path) -> Optional[Any]:
    """The speaker's mask for `frame` (float32 HxW, 0..1), or None.

    Cached as `<frame stem>.person.png` next to the frame, so a retried chunk
    does not pay for the call again."""
    import asyncio

    import numpy as np
    from PIL import Image

    frame = Path(frame)
    cache = frame.with_name(f"{frame.stem}.person.png")
    try:
        with Image.open(frame) as im:
            width, height = im.size
        if cache.exists() and cache.stat().st_mtime >= frame.stat().st_mtime:
            with Image.open(cache) as c:
                return np.asarray(c.convert("L"), dtype="float32") / 255.0
        # The local matte first — the same one that later puts the speaker in
        # front of Omni's output (speaker_front), ~1 s instead of Gemini's
        # 20-45 s. Gemini is the fallback when it cannot run.
        local = await asyncio.to_thread(_local_mask, frame)
        if local is not None:
            Image.fromarray(np.clip(np.rint(local * 255), 0, 255).astype("uint8")).save(cache)
            log.info("[person_matte] %s: person covers %.0f%% of the frame (local matte)",
                     frame.name, 100 * float((local > 0.5).mean()))
            return local
        from .vertex_client import gemini_multimodal

        # Two tries: the same frame and prompt sometimes come back without a
        # usable mask, and a second answer usually has one.
        mask = None
        for attempt in (1, 2):
            try:
                raw = await asyncio.wait_for(
                    gemini_multimodal(_PROMPT, images=[frame], model=MATTE_MODEL,
                                      temperature=0.2, max_tokens=8192, thinking_budget=0),
                    timeout=MATTE_TIMEOUT_SEC,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("[person_matte] attempt %d for %s failed: %s",
                            attempt, frame.name, str(e)[:200])
                continue
            mask = parse_masks(raw if isinstance(raw, str) else json.dumps(raw), width, height)
            if mask is not None:
                break
            log.warning("[person_matte] attempt %d for %s: no usable mask in %r",
                        attempt, frame.name, str(raw)[:160])
        if mask is None:
            return None
        Image.fromarray(np.clip(np.rint(mask * 255), 0, 255).astype("uint8")).save(cache)
        log.info("[person_matte] %s: person covers %.0f%% of the frame",
                 frame.name, 100 * float((mask > 0.5).mean()))
        return mask
    except Exception as e:  # noqa: BLE001 — no mask means no cut-out, never no edit
        log.warning("[person_matte] mask for %s failed: %s", frame.name, str(e)[:200])
        return None
