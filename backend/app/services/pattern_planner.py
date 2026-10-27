"""Where an auto-placed pattern goes in ONE clip — scored on the clip's frame.

1.png (beads_row) has no single place: the owner wants it "basically
anywhere — top, bottom, like 5.png or anything — make it random but all the
randoms should be calculated", and uploads allowed more than one place work
the same way. pattern_library gives such a pattern several CANDIDATE layouts;
this module scores every candidate on the clip's own first frame and the
speaker's mask, keeps the near-best, and picks one of those at random —
seeded, so the same experiment always lands on the same place and a new
experiment may land elsewhere.

THE SCORE (per candidate, from the placement study of 2026-09-26: 15 frames,
13 speakers and framings, eye-labelled GOOD/BAD candidates, weights fitted
over 38,880 settings with leave-one-frame-out checks) is a PRODUCT of
factors, each in (0, 1] and floored at FLOOR:

  presence  visible ink as a share of the frame / PRESENCE_FULL, capped at 1
            — the frame should show enough pattern (the owner: BIG).
  visible   share of the art's ink the speaker leaves visible. Being partly
            hidden is the approved 2.png look; this only sinks a candidate
            cut to fragments.
  calm      1 - edge density of the footage under the visible ink: lines over
            a plain wall read, over shelves and ceiling pipes they are noise.
  legible   CIELAB delta-E between the pattern's colour and the footage under
            its ink / DE_FULL. WCAG luminance contrast called violet on grey
            illegible; delta-E only bites on same-hue decor (violet ink on a
            violet 42 banner) or a non-vivid colour.
  head      1 - share of the visible ink in a halo around the speaker's head
            (reaching HEAD_UP times further upwards). Ink on a letterbox bar
            never counts — the picture's edge separates it from the head.
  ground    bottom bands only: 1 - GROUND_PENALTY x (share of their ink NOT
            on a letterbox bar). The bottom of a talking-head frame is floor,
            desk, sofa or lap, and a band there reads as a rug or a print on
            the desk; in a bar it is clean brand space.

  score = presence * visible * calm^0.5 * legible * head^2 * ground

A product, not a sum: a ratio between two scores then means the same on every
frame, which the choice relies on, and one bad factor (ink hugging the face)
sinks a candidate however good the rest is.

THE CHOICE: every candidate scoring at least KEEP_RATIO x the best (stricter,
KEEP_RATIO_NO_FACE, when the head is only estimated from the mask) is in the
pool; one is drawn weighted by score^PICK_POWER from random.Random(seed). A
string seed is hashed with SHA-512, so it is stable across processes. Below
ABS_FLOOR nothing fits (a tight close-up) and the pattern keeps its default,
first candidate.

Cheap on purpose: everything is scored on a quarter-resolution grid; each
candidate is drawn by the REAL renderer (rebrand_pattern_ref) so "visible"
means visible in Omni's target frame; the only model is the speaker mask the
pipeline already computes (local MODNet). Pure numpy/PIL, except the head:
OpenCV's bundled Haar face cascade (no download, ~70 ms) when cv2 is there,
else the top of the speaker's mask. ~0.5 s a frame. No paid call.

choose() never raises: any failure falls back to the first candidate and
says so in its report.

SEVERAL PATTERNS (only when choose() is given `occupied` / `taken`; without
them every score and pick is exactly as above): the patterns already placed
in the frame are `occupied` — their ink, grown by APART_MARGIN of the short
side — and the slots they hold are `taken` (a strip's side, a band's corner,
the whole frame). A candidate whose slot is taken, or whose visible ink lies
more than OVERLAP_MAX inside `occupied`, leaves the pool; the rest are scored
with one more factor,

  apart     1 - share of the visible ink inside `occupied`, used as apart^2,

so the pattern keeps its distance even among the places still free. When no
candidate is left, the one overlapping least is used (reason "no free
place"). Calibrated on a contact sheet of 2 and 3 patterns over the real
studio frames (experiments 14-24) plus run 815 — see APART_MARGIN.
"""
from __future__ import annotations

import logging
import random
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from . import rebrand_pattern_ref as pattern_ref
from .rebrand_pattern_ref import Layout

log = logging.getLogger("archivist.rebrand.planner")

# Quarter resolution: 180x320 for a 720x1280 frame.
GRID = 4
# Exponents of the product (fitted; see the module docstring).
A_PRESENCE, A_VISIBLE, A_CALM, A_LEGIBLE, A_HEAD = 1.0, 1.0, 0.5, 1.0, 2.0
PRESENCE_FULL = 0.05       # visible ink over 5% of the frame is "enough"
EDGE_FULL = 0.10           # local mean |grad luma| (0..1, at 1/4 res) that counts as busy
DE_FULL = 50.0             # CIELAB delta-E that reads fully
HEAD_HALO = 0.5            # halo scale around the head box, x the head's size
HEAD_UP = 2.0              # the halo reaches this many times further UP than sideways
# A bottom band on footage (not a bar). 0.65 let a bottom-right band into a
# mid shot's pool with the blue intro colour #0663a3; 0.75 holds every label.
GROUND_PENALTY = 0.75
FLOOR = 0.02               # no factor below this (keeps the product log-safe)
# Letterbox bars: flat dark rows running in from the top or bottom edge —
# rebrand_pattern_ref's own rule.
BAR_MAX_MEAN = pattern_ref.BAR_MAX_MEAN
BAR_MAX_STD = pattern_ref.BAR_MAX_STD
# The choice.
KEEP_RATIO = 0.50          # with a detected face
KEEP_RATIO_NO_FACE = 0.70  # head only estimated from the mask: pickier (a BAD candidate
                           # entered 5 of 14 pools at 0.5, 1 of 14 at 0.7)
PICK_POWER = 2.0
# Best score below this = nothing fits this framing. The tight close-up's
# best was 0.0003 and the lowest real best 0.020, so it sits 4x from each.
ABS_FLOOR = 0.005
# Several patterns (see the module docstring): how far apart two patterns'
# ink stays (a share of the frame's short side), how much of a candidate's
# visible ink may lie inside the others' grown footprint, and the exponent of
# the "apart" factor. Calibrated on 11 real frames (experiments 14-24 but 18,
# run 815) x five 2- and 3-pattern mixes: every 2-pattern mix lands clean at
# 0.03 / 0.05. At 0.10, 1.png's top-right band was let across structure's
# pole in a letterbox bar (overlap 0.10); at 0.05 it takes the top-left band
# and its beads pass beside the pole (0.04). A 3rd pattern after two strips
# has no free place on any frame (both strips reach the middle) and is warned.
APART_MARGIN = 0.03
OVERLAP_MAX = 0.05
A_APART = 2.0


@dataclass
class FrameInfo:
    """One frame read once, at grid resolution, for every candidate."""
    w: int
    h: int
    keep: Any                  # 1 where the pattern may show (outside the grown speaker mask)
    edges: Any                 # 0..1 busy-ness
    lab: Any                   # CIELAB of the blurred footage (h, w, 3)
    head: Optional[tuple[int, int, int, int]]   # x0, y0, x1, y1 at grid res
    head_src: str              # "haar" | "mask-top" | "none"
    halo: Any                  # 0..1 head-halo weight (0 on bars)
    bar: Any                   # 1 on letterbox-bar rows
    letterbox: float           # share of rows that are bar


# ---------------------------------------------------------------------------
# Small image arithmetic (numpy only)
# ---------------------------------------------------------------------------

def _box_blur(a, k: int):
    """Mean over a k x k window (k odd), edges reflected — OpenCV's blur()."""
    import numpy as np

    r = k // 2
    p = np.pad(a, [(r, r), (r, r)] + [(0, 0)] * (a.ndim - 2), mode="reflect").astype("float64")
    c = p.cumsum(0).cumsum(1)
    c = np.pad(c, [(1, 0), (1, 0)] + [(0, 0)] * (a.ndim - 2))
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return (s / (k * k)).astype("float32")


def _sobel_magnitude(luma):
    """|gradient| of a 0..1 image with the 3x3 Sobel kernels, /8 so a hard
    0-to-1 edge reads 1."""
    import numpy as np

    p = np.pad(luma, 1, mode="reflect")
    gx = ((p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:])
          - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])) / 8.0
    gy = ((p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:])
          - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])) / 8.0
    return np.sqrt(gx * gx + gy * gy).astype("float32")


def _lab(rgb01):
    """CIELAB (D65, L 0..100) of sRGB values in 0..1, shape (..., 3)."""
    import numpy as np

    c = np.asarray(rgb01, dtype="float64")
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.412453, 0.357580, 0.180423],
                  [0.212671, 0.715160, 0.072169],
                  [0.019334, 0.119193, 0.950227]])
    xyz = (c @ m.T) / np.array([0.950456, 1.0, 1.088754])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    lum = np.where(xyz[..., 1] > 0.008856, 116.0 * f[..., 1] - 16.0, 903.3 * xyz[..., 1])
    return np.stack([lum, 500.0 * (f[..., 0] - f[..., 1]), 200.0 * (f[..., 1] - f[..., 2])],
                    axis=-1).astype("float32")


def _max_filter(a, size: int):
    """PIL's MaxFilter(size) (a size x size square) as two 1-D passes —
    the same result (a max over a window clipped to the image equals one
    over edge-replicated padding) in ~1/15 of the time."""
    import numpy as np

    r = size // 2
    h, w = a.shape
    p = np.pad(a, ((0, 0), (r, r)), mode="edge")
    row = p[:, :w].copy()
    for i in range(1, size):
        np.maximum(row, p[:, i:i + w], out=row)
    p = np.pad(row, ((r, r), (0, 0)), mode="edge")
    out = p[:h].copy()
    for i in range(1, size):
        np.maximum(out, p[i:i + h], out=out)
    return out


def _resize(a, w: int, h: int):
    """A 0..1 float image box-averaged to w x h — a thin stroke counts by its area."""
    import numpy as np
    from PIL import Image

    img = Image.fromarray(np.clip(np.rint(np.asarray(a) * 255.0), 0, 255).astype("uint8"))
    return np.asarray(img.resize((w, h), Image.BOX), dtype="float32") / 255.0


# ---------------------------------------------------------------------------
# The speaker's head
# ---------------------------------------------------------------------------

_CASCADE: Any = None
# One cascade, loaded once; OpenCV does not promise a CascadeClassifier is
# safe to share between threads, and two experiments may plan at once.
_CASCADE_LOCK = threading.Lock()


def _faces(gray) -> list[tuple[int, int, int, int]]:
    """Frontal faces (x, y, w, h) from OpenCV's bundled Haar cascade, or []
    when OpenCV (or its data file) is not there."""
    global _CASCADE
    try:
        import cv2
    except ImportError:
        return []
    h, w = gray.shape
    side = max(24, w // 20)
    with _CASCADE_LOCK:
        if _CASCADE is None:
            c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            _CASCADE = False if c.empty() else c
        if _CASCADE is False:
            return []
        found = _CASCADE.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                          minSize=(side, side))
    return [tuple(int(v) for v in f) for f in found]


def _largest_blob(binary):
    """The largest 8-connected blob of a boolean mask (MODNet leaves specks —
    one sat 170 px above a seated speaker's head); the mask itself without
    OpenCV."""
    try:
        import cv2
    except ImportError:
        return binary
    import numpy as np

    n, labels, stats, _c = cv2.connectedComponentsWithStats(binary.astype("uint8"), connectivity=8)
    if n <= 1:
        return np.zeros_like(binary)
    return labels == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))


def head_box(frame_rgb, person) -> tuple[Optional[tuple[int, int, int, int]], str]:
    """The speaker's whole head (face + hair or head covering) as full-res
    x0, y0, x1, y1, and how it was found.

    The biggest Haar face that sits ON the speaker's mask (a face on a poster
    behind them does not count), grown to the head; else the top of the
    mask's largest blob, calibrated to the grown Haar box (measured on five
    frames with both: that box starts ~0.25 of a head above the silhouette's
    top and is ~1.1x wider, ~1.3x taller)."""
    import numpy as np

    h, w = frame_rgb.shape[:2]
    gray = (frame_rgb[..., 0] * 0.299 + frame_rgb[..., 1] * 0.587
            + frame_rgb[..., 2] * 0.114).astype("uint8")
    best = None
    for (x, y, fw, fh) in _faces(gray):
        cx, cy = min(w - 1, x + fw // 2), min(h - 1, y + fh // 2)
        if person is not None and person[cy, cx] < 0.5:
            continue
        if best is None or fw * fh > best[2] * best[3]:
            best = (x, y, fw, fh)
    if best is not None:
        x, y, fw, fh = best
        return (max(0, int(x - 0.30 * fw)), max(0, int(y - 0.55 * fh)),
                min(w, int(x + 1.30 * fw)), min(h, int(y + 1.25 * fh))), "haar"
    if person is None:
        return None, "none"
    blob = _largest_blob(person > 0.5)
    rows = np.nonzero(blob.sum(axis=1) > 0.01 * w)[0]
    if not rows.size:
        return None, "none"
    top = int(rows[0])
    xs = np.nonzero(blob[top: top + max(8, h // 12)].any(axis=0))[0]
    if not xs.size:
        return None, "none"
    cx = int((xs[0] + xs[-1]) / 2)
    size = max(int(xs[-1] - xs[0]), w // 8)
    bw, bh = 1.1 * 1.4 * size, 1.3 * 1.3 * size
    y0 = top - 0.25 * 1.3 * size
    return (max(0, int(cx - bw / 2)), max(0, int(y0)), min(w, int(cx + bw / 2)),
            min(h, int(y0 + bh))), "mask-top"


# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------

def keep_grid(frame_rgb, person):
    """Where a pattern can be seen on the frame (uint8 H x W x 3), at grid
    resolution: 1 outside the speaker's mask (float H x W, 0..1), all ones
    without one. The mask is grown exactly as strip_preview grows it — and
    ignored on letterbox bars, as it is for every pattern the planner places
    (PatternRef.clears_bars) — so "visible" means visible in the target frame
    Omni is shown. The overlap warning for several patterns
    (elements/pattern.build_all) measures on this too."""
    import numpy as np
    from PIL import Image, ImageFilter

    H, W = frame_rgb.shape[:2]
    w, h = max(1, W // GRID), max(1, H // GRID)
    if person is None:
        return np.ones((h, w), dtype="float32")
    m8 = np.clip(np.rint(person * 255.0), 0, 255).astype("uint8")
    edge = max(3, round(min(W, H) * 0.008)) | 1
    grown = np.asarray(Image.fromarray(_max_filter(m8, edge)).filter(ImageFilter.GaussianBlur(1.2)),
                       dtype="float32") / 255.0
    bars = pattern_ref.bar_mask(frame_rgb)
    if bars is not None:
        grown[bars] = 0.0
    return _resize(1.0 - grown, w, h)


def frame_info(frame_rgb, person) -> FrameInfo:
    """Read a frame (uint8 H x W x 3) and the speaker's mask (float H x W,
    0..1, or None) once, for every candidate."""
    import numpy as np
    from PIL import Image

    H, W = frame_rgb.shape[:2]
    w, h = max(1, W // GRID), max(1, H // GRID)
    keep = keep_grid(frame_rgb, person)

    small = np.asarray(Image.fromarray(frame_rgb).resize((w, h), Image.BOX))
    luma = small.astype("float32").mean(axis=2) / 255.0
    k = max(3, w // 24) | 1
    edges = np.clip(_box_blur(_sobel_magnitude(luma), k) / EDGE_FULL, 0.0, 1.0)
    lab = _lab(_box_blur(small.astype("float32"), 5) / 255.0)

    value = small.astype("float32").max(axis=2)
    flat = (value.mean(axis=1) <= BAR_MAX_MEAN) & (value.std(axis=1) <= BAR_MAX_STD)
    bar_rows = np.zeros(h, dtype=bool)
    i = 0
    while i < h and flat[i]:
        bar_rows[i] = True
        i += 1
    i = h - 1
    while i >= 0 and flat[i]:
        bar_rows[i] = True
        i -= 1
    if bar_rows.mean() > 0.88:                     # dark throughout: not bars
        bar_rows[:] = False
    bar = np.broadcast_to(bar_rows[:, None], (h, w)).astype("float32")

    hb, src = head_box(frame_rgb, person)
    halo = np.zeros((h, w), dtype="float32")
    head = None
    if hb is not None:
        x0, y0, x1, y1 = (v // GRID for v in hb)
        head = (x0, y0, x1, y1)
        yy, xx = np.mgrid[0:h, 0:w]
        dx = np.maximum(0, np.maximum(x0 - xx, xx - x1))
        dy = np.maximum(0, np.maximum((y0 - yy) / HEAD_UP, yy - y1))
        size = max(x1 - x0, y1 - y0, 1)
        halo = np.exp(-((np.sqrt(dx * dx + dy * dy) / (HEAD_HALO * size)) ** 2)).astype("float32")
    halo = halo * (1.0 - bar)
    return FrameInfo(w=w, h=h, keep=keep, edges=edges, lab=lab, head=head, head_src=src,
                     halo=halo, bar=bar, letterbox=float(bar_rows.mean()))


# ---------------------------------------------------------------------------
# The candidates
# ---------------------------------------------------------------------------

# Grid-resolution alpha per (slug, mask, layout, frame size): an experiment
# plans once, but a test (or a contact sheet) plans many frames of one pattern.
_ALPHAS: dict[tuple, Any] = {}
_ALPHAS_MAX = 64


def candidate_alpha(slug: str, layout: Layout, width: int, height: int, *,
                    mask: Optional[Path] = None, work_dir: Optional[Path] = None):
    """The candidate's ink as float alpha (width x height), drawn by the real
    renderer exactly as strip_preview would lay it into a frame that size."""
    import numpy as np

    src = pattern_ref._mask_for(slug, mask)
    key = (slug, str(src), src.stat().st_mtime, layout, width, height)
    hit = _ALPHAS.get(key)
    if hit is not None:
        return hit

    def draw(folder: Path):
        tag = (f"{layout.placement}_{layout.anchor}_{layout.rotate}_{layout.scale}_"
               f"{layout.reach}_{layout.fit}_{width}x{height}")
        if layout.valign != "top":
            tag += f"_{layout.valign}"
        ref = pattern_ref.PatternRef(
            slug=slug, source="", path=folder / f"plan_{slug}_{tag}.png", ground="#ffffff",
            ink="#000000", fell_back=False, colour="#ffffff", layout=layout, mask=mask)
        if layout.placement in ("left", "right"):
            # Drawn as build() draws it: a bottom-anchored strip (valign)
            # shows the other end of its art.
            pattern_ref.colorize(slug, "#ffffff", ref.path, width=width, height=height,
                                 scale=layout.scale, mask=mask, rotate=layout.rotate,
                                 valign=layout.valign)
        layer = pattern_ref._layer(ref, width, height)
        if layer is None:
            return np.zeros((height, width), dtype="float32")
        return np.asarray(layer.getchannel("A"), dtype="float32") / 255.0

    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        a = draw(Path(work_dir))
    else:
        with tempfile.TemporaryDirectory(prefix="pattern_plan_") as tmp:
            a = draw(Path(tmp))
    if len(_ALPHAS) >= _ALPHAS_MAX:
        _ALPHAS.clear()
    _ALPHAS[key] = a
    return a


def slot(layout: Layout) -> str:
    """The part of the frame a layout holds — two patterns never share one:
    a strip its side, a band its edge and corner, a full background all of
    it ("full")."""
    if layout.placement in ("left", "right"):
        return layout.placement
    if layout.placement in ("top", "bottom"):
        return f"{layout.placement}-{layout.anchor}"
    return "full"


def footprint(occupied, w: int, h: int):
    """The frame's `occupied` ink (float, any size, 0..1) at w x h, grown by
    APART_MARGIN of the short side: 1 where another pattern is, or is too
    close."""
    import numpy as np

    occ = _resize(np.clip(np.asarray(occupied, dtype="float32"), 0.0, 1.0), w, h)
    grow = max(1, round(APART_MARGIN * min(w, h)))
    return _max_filter((occ > 0.02).astype("float32"), 2 * grow + 1)


def overlap_share(ink, occupied) -> float:
    """The share of `ink` (float alpha) that lies inside `occupied` (a
    footprint the same size)."""
    import numpy as np

    total = float(np.asarray(ink, dtype="float64").sum())
    if total <= 1e-6:
        return 0.0
    return float((np.asarray(ink, dtype="float64") * np.asarray(occupied, dtype="float64")).sum()) / total


def factors(fi: FrameInfo, ink, colour: str, layout: Layout, occupied=None) -> dict:
    """The score's factors for one candidate (its alpha at grid res, `ink`).
    `occupied` (a footprint at grid res, or None) adds "apart" and
    "overlap" — only then."""
    import numpy as np

    total = float(ink.sum())
    visible_ink = ink * fi.keep
    vis = float(visible_ink.sum())
    if total <= 0 or vis <= 1e-6:
        empty = {"presence": FLOOR, "visible": FLOOR, "calm": FLOOR, "legible": FLOOR,
                 "head": FLOOR, "ground": 1.0, "ink": 0.0, "bar": 0.0}
        if occupied is not None:
            empty.update(apart=FLOOR, overlap=1.0)
        return empty
    wv = visible_ink / vis
    ink_lab = _lab(np.array(pattern_ref._rgb(colour), dtype="float64") / 255.0)
    de = np.sqrt(((fi.lab - ink_lab) ** 2).sum(axis=2))
    on_bar = float((wv * fi.bar).sum())
    f = {
        "presence": min(1.0, vis / (fi.w * fi.h) / PRESENCE_FULL),
        "visible": vis / total,
        "calm": 1.0 - float((wv * fi.edges).sum()),
        "legible": float((wv * np.clip(de / DE_FULL, 0.0, 1.0)).sum()),
        "head": 1.0 - float((wv * fi.halo).sum()),
        "ground": 1.0 - GROUND_PENALTY * (1.0 - on_bar) if layout.placement == "bottom" else 1.0,
        "ink": vis / (fi.w * fi.h),
        "bar": on_bar,
    }
    for k in ("presence", "visible", "calm", "legible", "head"):
        f[k] = max(FLOOR, f[k])
    if occupied is not None:
        f["overlap"] = overlap_share(visible_ink, occupied)
        f["apart"] = max(FLOOR, 1.0 - f["overlap"])
    return f


def combine(f: dict) -> float:
    s = (f["presence"] ** A_PRESENCE * f["visible"] ** A_VISIBLE * f["calm"] ** A_CALM
         * f["legible"] ** A_LEGIBLE * f["head"] ** A_HEAD * f["ground"])
    if "apart" in f:
        s *= f["apart"] ** A_APART
    return s


def _load_rgb(frame) -> Any:
    import numpy as np
    from PIL import Image

    if isinstance(frame, (str, Path)):
        with Image.open(frame) as im:
            return np.asarray(im.convert("RGB"))
    arr = np.asarray(frame)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f"not an RGB frame: shape {arr.shape}")
    return np.ascontiguousarray(arr[:, :, :3]).astype("uint8")


def _load_person(person, width: int, height: int):
    """The speaker mask as float (height, width) in 0..1, or None."""
    import numpy as np
    from PIL import Image

    if person is None:
        return None
    m = np.clip(np.asarray(person, dtype="float32"), 0.0, 1.0)
    if m.ndim != 2:
        raise ValueError(f"not a mask: shape {m.shape}")
    if m.shape != (height, width):
        m = np.asarray(Image.fromarray(np.rint(m * 255).astype("uint8")).resize(
            (width, height), Image.BILINEAR), dtype="float32") / 255.0
    return m


def score(
    candidates: Sequence[Layout], frame_rgb, person_mask, *, slug: str,
    mask: Optional[Path] = None, colour: str = pattern_ref.BRAND_GROUND,
    work_dir: Optional[Path] = None, occupied=None,
) -> dict:
    """Every candidate scored on one frame: {"head", "letterbox",
    "candidates": [{index, label, layout, score, factors}]} in candidate
    order. `frame_rgb` is a path or a uint8 H x W x 3 array; `person_mask` a
    float H x W mask (any size — it is fitted to the frame) or None.
    `occupied` (float, any size: the ink of the patterns already placed)
    adds the "apart" factor; None scores exactly as before."""
    rgb = _load_rgb(frame_rgb)
    H, W = rgb.shape[:2]
    person = _load_person(person_mask, W, H)
    fi = frame_info(rgb, person)
    occ = footprint(occupied, fi.w, fi.h) if occupied is not None else None
    rows = []
    for i, lay in enumerate(candidates):
        ink = _resize(candidate_alpha(slug, lay, W, H, mask=mask, work_dir=work_dir), fi.w, fi.h)
        f = factors(fi, ink, colour, lay, occ) if occ is not None else factors(fi, ink, colour, lay)
        rows.append({"index": i, "label": pattern_ref.placement_label(lay), "layout": lay,
                     "score": combine(f), "factors": f})
    return {"head": fi.head_src, "letterbox": fi.letterbox, "candidates": rows}


def _pick(scores: list[float], head_src: str, seed: str,
          blocked: Optional[list[bool]] = None,
          overlaps: Optional[list[float]] = None) -> tuple[int, list[int], list[float], str, float]:
    """(picked index, pool, pool probabilities, reason, keep ratio).
    `blocked` (several patterns) keeps those candidates out of the pool; when
    every one is blocked the least-overlapping is used ("no free place")."""
    ratio = KEEP_RATIO if head_src == "haar" else KEEP_RATIO_NO_FACE
    free = list(range(len(scores)))
    if blocked is not None:
        free = [i for i in free if not blocked[i]]
        if not free:
            least = min(range(len(scores)), key=lambda i: ((overlaps or [0.0] * len(scores))[i], -scores[i]))
            return least, [least], [1.0], "no free place", ratio
    best = max(scores[i] for i in free) if free else 0.0
    if best < ABS_FLOOR:
        return free[0], [free[0]], [1.0], "nothing fits", ratio
    pool = [i for i in free if scores[i] > 0 and scores[i] >= ratio * best]
    weights = [scores[i] ** PICK_POWER for i in pool]
    total = sum(weights) or 1.0
    picked = random.Random(seed).choices(pool, weights=weights, k=1)[0]
    return picked, pool, [w / total for w in weights], "near-best", ratio


def choose(
    candidates: Sequence[Layout], frame, person, *, seed: str, slug: str,
    mask: Optional[Path] = None, colour: str = pattern_ref.BRAND_GROUND,
    work_dir: Optional[Path] = None, occupied=None, taken=None,
) -> tuple[Layout, dict]:
    """(the layout this clip gets, a JSON-safe report of why).

    RANDOM AMONG THE NEAR-BEST, seeded: see the module docstring. One
    candidate is returned as is; any failure returns the first (the
    pattern's default) with the error in the report — this never raises
    into the pipeline.

    `occupied` / `taken` (several patterns, see the module docstring): the
    ink and the slots of the patterns already placed. None for both is the
    single-pattern choice, byte for byte."""
    cands = tuple(candidates)
    if not cands:
        raise ValueError("no candidate layouts")
    base = {"mode": "auto", "seed": seed, "frame": str(frame) if isinstance(frame, (str, Path)) else None}
    if len(cands) == 1:
        return cands[0], {**base, "mode": "fixed", "reason": "one place",
                          "picked": _picked(0, cands[0]), "pool": [], "candidates": []}
    several = occupied is not None or taken is not None
    try:
        scored = score(cands, frame, person, slug=slug, mask=mask, colour=colour, work_dir=work_dir,
                       **({"occupied": occupied} if occupied is not None else {}))
        rows = scored["candidates"]
        scores = [float(r["score"]) for r in rows]
        blocked = overlaps = None
        if several:
            slots_taken = set(taken or ())
            overlaps = [float(r["factors"].get("overlap", 0.0)) for r in rows]
            blocked = [slot(r["layout"]) in slots_taken or overlaps[i] > OVERLAP_MAX
                       for i, r in enumerate(rows)]
            idx, pool, probs, reason, ratio = _pick(scores, scored["head"], seed, blocked, overlaps)
        else:
            idx, pool, probs, reason, ratio = _pick(scores, scored["head"], seed)
        best = max(scores) or 1.0
        report = {
            **base, "reason": reason, "head": scored["head"],
            "letterbox": round(scored["letterbox"], 3), "keep_ratio": ratio,
            "best_score": round(max(scores), 5),
            "picked": _picked(idx, cands[idx]),
            "pool": [{"index": i, "label": rows[i]["label"], "p": round(p, 3)}
                     for i, p in sorted(zip(pool, probs), key=lambda t: -t[1])],
            "candidates": [{
                "index": r["index"], "label": r["label"], "layout": r["layout"].public(),
                "score": round(r["score"], 5), "rel": round(r["score"] / best, 3),
                "factors": {k: round(float(v), 3) for k, v in r["factors"].items()},
            } for r in rows],
        }
        if several:
            report["taken"] = sorted(set(taken or ()))
            for c, r, off in zip(report["candidates"], rows, blocked or []):
                c["slot"] = slot(r["layout"])
                c["blocked"] = bool(off)
        log.info("[planner] %s: %s (%s) — pool %s, head via %s", slug, report["picked"]["label"],
                 reason, ", ".join(f"{p['label']} {p['p']:.0%}" for p in report["pool"]),
                 scored["head"])
        return cands[idx], report
    except Exception as e:  # noqa: BLE001 — a failed plan must never cost the run
        log.warning("[planner] %s: planning failed (%s) — using its default place", slug, str(e)[:200])
        return cands[0], {**base, "reason": "error", "error": (str(e) or type(e).__name__)[:200],
                          "picked": _picked(0, cands[0]), "pool": [], "candidates": []}


def _picked(index: int, layout: Layout) -> dict:
    return {"index": index, "label": pattern_ref.placement_label(layout), "layout": layout.public()}


def summary(report: dict, limit: int = 300) -> str:
    """The report in one line, for the arm's step detail."""
    reason = report.get("reason")
    if reason == "error":
        text = f"planning failed ({report.get('error', '')}) — its default place"
    elif reason == "nothing fits":
        text = "nothing fits this framing well — its default place"
    elif reason == "no free place":
        text = ("every place it has is taken by another pattern — it goes where it overlaps "
                "least")
    elif reason in ("no frame", "one place"):
        text = "no frame to plan on — its default place" if reason == "no frame" else "its one place"
    else:
        pool = ", ".join(f"{p['label']} {p['p']:.0%}" for p in report.get("pool", []))
        scores = ", ".join(f"{c['label']} {c['rel']:.2f}" for c in report.get("candidates", []))
        text = (f"drawn at random from the places that fit this clip best ({pool}); "
                f"relative scores: {scores}")
    return text[:limit]
