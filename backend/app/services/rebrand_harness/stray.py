"""Did Omni draw a symbol of its own? — a check on every run in which Omni
is asked for nothing but the patterns and the stray-drawing clean-up is off
(guards.checks_strays): our icons stamped after Omni, or the harness's own
opening. Not the approved 42 default, whose brief asks Omni for icons.

With our icons stamped after Omni, Omni is told to add none of its own
(prompt.ICONS_CLAUSE). It does not always listen: experiment 23 came back
with line art nobody asked for. The clean-up (the recipe's guards.stray
"clean") takes such marks away in the speaker pass; with it off, this
finds them. Outside what
it was asked to draw (the patterns' footprint, and a margin around it:
Omni runs a pattern on a little past its end), what our icons will cover
and the speaker, its clip should be the original room — re-rendered, so
never pixel for pixel, but never a new mark. A few frames of the fitted
clip are held against the original part there; a compact, hard-edged blob
that stays put across two of them is a symbol, and the version says so,
with the clean-up as the way to be rid of it (warning). Local and free:
the speaker is the local MODNet matte.

Set on the real runs kept on this machine (14-24): those whose brief still
asked Omni for icons are caught on them (the badges and brackets beside
the speaker), while what every run has — the frame's padded edge, a
pattern running on past its end, the speaker a little off where Omni
redrew them, the letterbox bars Omni fills in, a last second that no
longer lines up with the original — is not taken for one.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("archivist.rebrand.harness.stray")

# A pixel this far (any channel, of 255, both frames lightly blurred) from
# the original is changed; Omni's re-render of the room stays well under it.
STRAY_DIFF = 56
# A symbol's share of the frame: more than a speck, less than a relit room.
STRAY_MIN = 0.0015
STRAY_MAX = 0.04
# ... compact: its changed pixels fill this much of its box, which is at
# least this many pixels and at most this share of the width either way.
STRAY_FILL = 0.12
STRAY_SIDE = 16
STRAY_SPAN = 0.30
# Kept clear of the speaker (their matte in the original and in Omni's
# frame, grown by this share of the short side), of the patterns' footprint
# (grown by this, and a blob within NEAR of it is the pattern running on)
# and of the icons' boxes.
SPEAKER_GROW = 0.03
FOOTPRINT_GROW = 0.02
NEAR = 0.04
EDGE = 12              # a blob hugging the frame's edge this thin is its padding
# More than this share of the room changed: the frames no longer line up
# (a cut in the clip, a relit room) — nothing can be told from it.
MISMATCH = 0.12
SAMPLES = 6
SKIP_SEC = 1.0          # an entrance is settling before this
TAIL_SEC = 0.5          # ... and the last moments may not line up
SAME_PLACE = 0.3        # a blob seen again (box overlap) is the same mark


def _grown(mask: Any, frac: float) -> Any:
    import cv2
    import numpy as np

    h, w = mask.shape
    r = max(1, int(round(min(h, w) * frac)))
    return cv2.dilate((np.asarray(mask) > 0.5).astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))) > 0


def _iou(a: tuple, b: tuple) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def changed(fe: Any, fo: Any, *, keep_out: Any = None, near: Any = None) -> Optional[list[dict]]:
    """The blobs where `fe` (Omni's frame) carries a new mark over `fo` (the
    original) outside `keep_out` (bool HxW), leaving out those that touch
    `near` (bool HxW: around the patterns) or hug the frame's edge:
    [{box: (x0, y0, x1, y1), share}], largest first. None when the two
    frames do not line up at all (MISMATCH)."""
    import cv2
    import numpy as np

    a = cv2.GaussianBlur(fe, (0, 0), 1.2).astype(np.int16)
    b = cv2.GaussianBlur(fo, (0, 0), 1.2).astype(np.int16)
    hot = (np.abs(a - b).max(axis=2) >= STRAY_DIFF)
    if keep_out is not None:
        hot &= ~keep_out
        room = max(1, int((~keep_out).sum()))
    else:
        room = hot.size
    if hot.sum() / room > MISMATCH:
        return None
    hot = cv2.morphologyEx(hot.astype(np.uint8), cv2.MORPH_CLOSE,
                           cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    hot = cv2.morphologyEx(hot, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(hot, connectivity=8)
    h, w = hot.shape
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        share = area / float(h * w)
        fill = area / float(max(1, bw * bh))
        if not (STRAY_MIN <= share <= STRAY_MAX and fill >= STRAY_FILL):
            continue
        if min(bw, bh) < STRAY_SIDE or max(bw, bh) > STRAY_SPAN * w:
            continue
        touches = x == 0 or y == 0 or x + bw >= w or y + bh >= h
        if touches and min(bw, bh) <= EDGE:
            continue
        if near is not None and near[labels == i].any():
            continue
        out.append({"box": (x, y, x + bw, y + bh), "share": round(share, 4)})
    return sorted(out, key=lambda b: -b["share"])


def find(edited: Path, original: Path, *, footprint: Any = None, boxes: Optional[Callable[[float], list]] = None,
         matte: Optional[Callable[[Any], Any]] = None, samples: int = SAMPLES,
         skip_sec: float = SKIP_SEC, tail_sec: float = TAIL_SEC) -> Optional[float]:
    """The first sampled moment (seconds into the part) where Omni's fitted
    clip `edited` carries a mark the original part `original` does not —
    seen at the same place in the next sample too — outside `footprint`
    (float HxW: the patterns it was asked for), the icons on screen then
    (`boxes(t)` -> [(x0, y0, x1, y1)]), the speaker and the letterbox bars;
    None when there is none. Synchronous; two MODNet passes a sample."""
    import cv2
    import numpy as np

    from ..rebrand_pattern_ref import bar_mask

    if matte is None:
        from .. import speaker_front

        matte = speaker_front.person_matte
    cap_e = cv2.VideoCapture(str(edited))
    cap_o = cv2.VideoCapture(str(original))
    try:
        if not (cap_e.isOpened() and cap_o.isOpened()):
            return None
        fps = cap_e.get(cv2.CAP_PROP_FPS) or 0.0
        fps = fps if fps > 1 else 30.0
        n = int(cap_e.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        first = int(round(skip_sec * fps))
        last = n - 1 - int(round(tail_sec * fps))
        if n <= 0 or last < first:
            return None
        want = sorted({int(round(x)) for x in np.linspace(first, last, max(2, samples))})
        fixed = near = None
        seen: Optional[list[dict]] = None
        seen_t = 0.0
        i = 0
        last_o = None
        for k in want:
            fe = fo = None
            while i <= k:
                ok_e, f_e = cap_e.read()
                ok_o, f_o = cap_o.read()
                if not ok_e:
                    return None
                if ok_o:
                    last_o = f_o
                fe, fo = f_e, (f_o if ok_o else last_o)
                i += 1
            if fe is None or fo is None:
                continue
            h, w = fe.shape[:2]
            if fo.shape[:2] != (h, w):
                fo = cv2.resize(fo, (w, h), interpolation=cv2.INTER_AREA)
            if fixed is None:
                fixed = np.zeros((h, w), dtype=bool)
                bars = bar_mask(fo)
                if bars is not None:
                    fixed |= bars
                if footprint is not None:
                    fp = np.asarray(footprint, dtype=np.float32)
                    if fp.shape != (h, w):
                        fp = cv2.resize(fp, (w, h), interpolation=cv2.INTER_LINEAR)
                    fixed |= _grown(fp, FOOTPRINT_GROW)
                    near = _grown(fp, NEAR)
            person = np.maximum(np.asarray(matte(fo), dtype=np.float32), np.asarray(matte(fe), dtype=np.float32))
            keep_out = fixed | _grown(person, SPEAKER_GROW)
            t = k / fps
            if boxes is not None:
                icons = np.zeros((h, w), dtype=np.float32)
                for x0, y0, x1, y1 in boxes(t) or []:
                    icons[max(0, int(y0)):max(0, int(y1)), max(0, int(x0)):max(0, int(x1))] = 1.0
                keep_out = keep_out | _grown(icons, FOOTPRINT_GROW)
            found = changed(fe, fo, keep_out=keep_out, near=near)
            if found is None:
                seen = None                 # these frames do not line up
                continue
            if seen and any(_iou(a["box"], b["box"]) >= SAME_PLACE for a in seen for b in found):
                return round(seen_t, 2)
            seen, seen_t = found, t
        return None
    finally:
        cap_e.release()
        cap_o.release()


def warning(t: float) -> str:
    """The version's warning, in the owner's words: a fresh take, or Clean it
    up — the studio's button beside it, which makes the version again with
    the clean-up on, reusing Omni's clip. The studio knows it by its start
    (VersionActions)."""
    return f"Omni drew its own symbol at {t:.1f} s — Try again, or Clean it up"
