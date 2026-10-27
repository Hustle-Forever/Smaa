"""Where the speaker is, over the whole scene — so an icon can be placed where
nobody is, for as long as it is on screen.

  build(base_clip, work_dir)   the scene's Track, cached as work_dir/track.npz
                               (+ track.json, + track_frame0.png)

One still cannot say whether a spot stays clear: a hand comes up at 5.2 s, the
head turns. So the base clip is sampled at SAMPLE_FPS (2 a second: 16 samples
for an 8 s scene, ~7 s of MODNet on this CPU) and each sample keeps

  - the speaker's matte at 1/MASK_DIV resolution (LOCAL MODNet,
    speaker_front.person_matte — never person_matte's paid Gemini fallback),
  - the head box (pattern_planner.head_box: a Haar face on the matte, grown to
    the whole head, else the top of the matte).

The letterbox bars (rebrand_pattern_ref.bar_mask, read on the first, middle
and last samples; two of three must agree) are never the speaker: the matte
is ignored there, as restore_speaker does with clear_bars.

A clip's jump cuts (what normalize_scene wrote beside it) add two samples
each — the last frame before the cut and the first after it — so no read
of the speaker ever blends two shots: mask_at uses only the samples on its
own side of a cut, and the moving preview is told the cuts (its masks'
`cuts`). A clip with no cut is sampled exactly as before (the grid alone),
and the bars, the first frame and the spots are read off the grid's
samples only, so the icon director's room read never moves.

The SPOTS are a fixed grid that depends only on the frame's size and its
bars — never on a pattern or a preference — so the icon director's room read
(one image with the spots numbered) stays cacheable:

  full-bleed portrait  L1-L4 at x = 0.04 W, centres y = 0.26/0.34/0.42/0.50 H;
                       R1-R4 mirrored; TL and TR in the top corners. Size
                       0.16 of the short side. No centre or bottom spots.
  letterboxed          the top and bottom bars' left / centre / right
                       (TB-L … BB-R), sized min(0.7 x bar, 0.22 W), plus the
                       picture's own edges PL / PR (and pillar-box bars'
                       LB-1/2, RB-1/2 when the bars are at the sides).
  landscape            the middle of the left and right thirds at centres
                       y = 0.30 / 0.50 H (L1, L2, R1, R2).

Each spot is {id, group: left|right|top|bottom, box: {x, y, w, h} as fractions
of the frame, calm}. The hard rules an icon's placer applies over its window
are here: head_clear (off the head, grown by 0.02 W), overlap (the share of the
box the speaker covers, bars excluded), inside the frame (sized keeps a box
there). cover() says which of them a box breaks ("face", "speaker"): the
studio's outline for an icon dragged by hand is the same rule
(frontend/src/lib/rebrandCover.ts, checked against a shared fixture).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("archivist.rebrand.harness.speaker_track")

# Bumped whenever a track or its spots can change for the same clip. 2: a
# sample either side of each jump cut, and the cuts kept (the times are
# uneven around a cut). A clip with no cut is sampled exactly as version 1
# sampled it, so its track keeps version 1's key and file (NO_CUT_VERSION):
# nothing is made again, and no icon plan's key moves.
TRACK_VERSION = 2
NO_CUT_VERSION = 1
SAMPLE_FPS = 2.0
MASK_DIV = 8
SPOT_FRAC = 0.16            # a spot's side, of the frame's short side (size M)
EDGE_FRAC = 0.04            # a column's distance from the frame edge, of W
FULL_YS = (0.26, 0.34, 0.42, 0.50)
LAND_YS = (0.30, 0.50)
BAR_SPOT = 0.7              # a bar spot's side, of the bar's height...
BAR_SPOT_MAX = 0.22         # ...at most this share of W
BAR_MIN = 0.06              # a bar thinner than this share of the frame has no spots
PICTURE_Y = 0.40            # a picture-edge spot's centre, down the picture
HEAD_GROW = 0.02            # the head box grows this share of W (head_clear)
OVERLAP_MAX = 0.03          # the speaker may cover at most this share of an icon
PERSON = 0.5                # a matte value this high is the speaker
EDGE_T = 0.25               # a Sobel magnitude (gray 0..1) this high is an edge (calm)
GROUPS = ("left", "right", "top", "bottom")

_LOCK = threading.Lock()


def _px(box: dict, width: int, height: int) -> tuple[int, int, int, int]:
    """A fractional box as whole pixels (x0, y0, x1, y1), inside the frame."""
    x0 = int(round(float(box["x"]) * width))
    y0 = int(round(float(box["y"]) * height))
    x1 = int(round((float(box["x"]) + float(box["w"])) * width))
    y1 = int(round((float(box["y"]) + float(box["h"])) * height))
    return max(0, x0), max(0, y0), min(width, x1), min(height, y1)


def _frac(x: float, y: float, w: float, h: float, width: int, height: int) -> dict:
    return {"x": round(x / width, 4), "y": round(y / height, 4),
            "w": round(w / width, 4), "h": round(h / height, 4)}


def picture_of(bars, width: int, height: int) -> tuple[int, int, int, int]:
    """(y0, y1, x0, x1) of the picture inside the bars (the whole frame
    without any)."""
    import numpy as np

    if bars is None:
        return 0, height, 0, width
    rows = np.flatnonzero(~bars.all(axis=1))
    cols = np.flatnonzero(~bars.all(axis=0))
    if not len(rows) or not len(cols):
        return 0, height, 0, width
    return int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1


def layout_of(width: int, height: int, picture: tuple[int, int, int, int], has_bars: bool) -> str:
    """"letterbox" when a bar is thick enough for spots, else "landscape"
    for a wide frame, else "full"."""
    y0, y1, x0, x1 = picture
    if has_bars and (max(y0, height - y1) >= BAR_MIN * height or max(x0, width - x1) >= BAR_MIN * width):
        return "letterbox"
    return "landscape" if width > height else "full"


def spot_grid(width: int, height: int, picture: Optional[tuple[int, int, int, int]] = None,
              layout: Optional[str] = None) -> list[dict]:
    """The fixed spots for a frame (module docstring): [{id, group, box,
    pin, size}]. `pin` says which side of the box stays put when it is
    resized (sized()); `size` is its side as a share of the short side."""
    W, H = int(width), int(height)
    pic = picture or (0, H, 0, W)
    layout = layout or layout_of(W, H, pic, picture is not None and pic != (0, H, 0, W))
    short = min(W, H)
    mx = EDGE_FRAC * W
    spots: list[dict] = []

    def add(id_: str, group: str, x: float, y: float, L: float, pin: str) -> None:
        spots.append({"id": id_, "group": group, "box": _frac(x, y, L, L, W, H), "pin": pin,
                      "size": round(L / short, 4)})

    if layout == "full":
        L = SPOT_FRAC * short
        for k, yf in enumerate(FULL_YS, 1):
            add(f"L{k}", "left", mx, yf * H - L / 2, L, "l")
        for k, yf in enumerate(FULL_YS, 1):
            add(f"R{k}", "right", W - mx - L, yf * H - L / 2, L, "r")
        add("TL", "top", mx, mx, L, "tl")
        add("TR", "top", W - mx - L, mx, L, "tr")
    elif layout == "landscape":
        L = SPOT_FRAC * short
        for k, yf in enumerate(LAND_YS, 1):
            add(f"L{k}", "left", W / 6 - L / 2, yf * H - L / 2, L, "c")
        for k, yf in enumerate(LAND_YS, 1):
            add(f"R{k}", "right", 5 * W / 6 - L / 2, yf * H - L / 2, L, "c")
    else:
        y0, y1, x0, x1 = pic
        for name, group, top, bottom in (("TB", "top", 0, y0), ("BB", "bottom", y1, H)):
            bh = bottom - top
            if bh < BAR_MIN * H:
                continue
            L = min(BAR_SPOT * bh, BAR_SPOT_MAX * W)
            cy = (top + bottom) / 2
            add(f"{name}-L", group, mx, cy - L / 2, L, "c")
            add(f"{name}-C", group, W / 2 - L / 2, cy - L / 2, L, "c")
            add(f"{name}-R", group, W - mx - L, cy - L / 2, L, "c")
        for name, group, left, right in (("LB", "left", 0, x0), ("RB", "right", x1, W)):
            bw = right - left
            if bw < BAR_MIN * W:
                continue
            L = min(BAR_SPOT * bw, BAR_SPOT_MAX * H)
            cx = (left + right) / 2
            for k, yf in enumerate(LAND_YS, 1):
                add(f"{name}-{k}", group, cx - L / 2, yf * H - L / 2, L, "c")
        ph, pw = y1 - y0, x1 - x0
        L = min(SPOT_FRAC * short, 0.45 * ph, 0.45 * pw)
        if L >= 0.05 * short:
            cy = y0 + PICTURE_Y * ph
            add("PL", "left", x0 + EDGE_FRAC * pw, cy - L / 2, L, "l")
            add("PR", "right", x1 - EDGE_FRAC * pw - L, cy - L / 2, L, "r")
    return spots


def anchors_of(spots: list[dict]) -> list[str]:
    """The placement anchors this frame can honour: "auto" and every group
    that has a spot, in GROUPS order."""
    have = {s["group"] for s in spots}
    return ["auto"] + [g for g in GROUPS if g in have]


def calm_of(frame_bgr, box_px: tuple[int, int, int, int]) -> float:
    """1 - the share of edge pixels (Sobel) in a box of the frame: 1 on a
    plain wall, lower on a poster, a plant or a screen."""
    import cv2
    import numpy as np

    x0, y0, x1, y1 = box_px
    if x1 <= x0 or y1 <= y0:
        return 1.0
    gray = cv2.cvtColor(frame_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    mag = np.hypot(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))
    return round(float(1.0 - (mag > EDGE_T).mean()), 4)


@dataclass
class Track:
    """The speaker over the scene (module docstring). Arrays are numpy."""

    width: int
    height: int
    fps: float
    frames: int
    step: int                          # frames between samples
    times: Any                         # (N,) seconds of each sample
    heads: Any                         # (N, 4) int32 x0, y0, x1, y1 px; -1 = none found
    masks: Any                         # (N, H/MASK_DIV, W/MASK_DIV) uint8 matte, 0..255
    bars: Any = None                   # (H, W) bool letterbox bars, or None
    layout: str = "full"
    picture: tuple[int, int, int, int] = (0, 0, 0, 0)
    spots: list[dict] = field(default_factory=list)
    frame0: Optional[Path] = None      # the first sampled frame that is not black
    key: str = ""
    cuts: list = field(default_factory=list)   # the clip's jump cuts (frames: each new shot's first)
    _bars_small: Any = field(default=None, repr=False, compare=False)
    _frame_img: Any = field(default=None, repr=False, compare=False)

    # -- basics ------------------------------------------------------------
    @property
    def duration(self) -> float:
        return self.frames / self.fps if self.fps else 0.0

    @property
    def digest(self) -> str:
        return self.key

    @property
    def anchors_available(self) -> list[str]:
        return anchors_of(self.spots)

    def spot(self, spot_id: str) -> Optional[dict]:
        return next((s for s in self.spots if s["id"] == spot_id), None)

    def bar_mask(self):
        """(H, W) bool letterbox bars, or None."""
        return self.bars

    def public(self) -> dict:
        """What the studio serves about the track (no arrays)."""
        y0, y1, x0, x1 = self.picture
        return {
            "key": self.key, "width": self.width, "height": self.height, "layout": self.layout,
            "picture": _frac(x0, y0, x1 - x0, y1 - y0, self.width, self.height),
            "bars": self.bars is not None,
            "spots": [{"id": s["id"], "group": s["group"], "box": s["box"], "calm": s.get("calm")}
                      for s in self.spots],
            "anchors_available": self.anchors_available,
            "samples": int(len(self.times)), "sample_fps": SAMPLE_FPS,
            "duration": round(self.duration, 3),
        }

    # -- the window --------------------------------------------------------
    @property
    def cut_times(self) -> list[float]:
        """Where the footage jumps to another shot (seconds)."""
        return [c / self.fps for c in self.cuts] if self.fps else []

    def samples_in(self, t0: float, t1: float) -> list[int]:
        """The samples covering [t0, t1], including the one at or before t0
        and the one at or after t1 (a hand moves between samples). Evenly
        spaced (no cut) by the grid's step, else by the samples' own times."""
        n = len(self.times)
        if not n:
            return []
        if not self.cuts:
            dt = self.step / self.fps if self.fps else 1.0 / SAMPLE_FPS
            lo = int(max(0, min(n - 1, (max(0.0, t0) + 1e-9) // dt)))
            hi = int(max(0, min(n - 1, -(-(max(t0, t1) - 1e-9) // dt))))
            return list(range(lo, max(lo, hi) + 1))
        import numpy as np

        times = np.asarray(self.times, dtype=np.float64)
        lo = int(max(0, min(n - 1, np.searchsorted(times, max(0.0, t0) + 1e-9, side="right") - 1)))
        hi = int(max(0, min(n - 1, np.searchsorted(times, max(t0, t1) - 1e-9, side="left"))))
        return list(range(lo, max(lo, hi) + 1))

    def _bars8(self):
        import numpy as np

        if self._bars_small is None:
            h8, w8 = self.masks.shape[1:] if self.masks is not None and len(self.masks) else (0, 0)
            if self.bars is None or not h8:
                self._bars_small = np.zeros((h8, w8), bool)
            else:
                import cv2

                self._bars_small = cv2.resize(self.bars.astype(np.float32), (w8, h8),
                                              interpolation=cv2.INTER_AREA) > 0.5
        return self._bars_small

    def union(self, t0: float, t1: float) -> tuple[Any, list[tuple[int, int, int, int]]]:
        """(the speaker's matte at 1/MASK_DIV, 0..1, the max over the window,
        bars cleared; every head box found in the window, px)."""
        import numpy as np

        idx = self.samples_in(t0, t1)
        if not idx:
            return np.zeros(self.masks.shape[1:], np.float32), []
        m = self.masks[idx].max(axis=0).astype(np.float32) / 255.0
        m[self._bars8()] = 0.0
        heads = [tuple(int(v) for v in self.heads[i]) for i in idx if self.heads[i][0] >= 0]
        return m, heads

    def blend_at(self, t: float) -> tuple[int, int, float]:
        """(i0, i1, a): the two samples around time t and how far t is from
        the first to the second (the matte at t is theirs, blended) — never
        across a cut: the samples either side of one stand on their own."""
        import numpy as np

        n = len(self.times)
        if not self.cuts:
            dt = self.step / self.fps
            f = max(0.0, min(n - 1.0, t / dt))
            i0 = int(f)
            return i0, min(n - 1, i0 + 1), f - i0
        times = np.asarray(self.times, dtype=np.float64)
        i0 = int(max(0, min(n - 1, np.searchsorted(times, t + 1e-9, side="right") - 1)))
        i1 = min(n - 1, i0 + 1)
        span = float(times[i1] - times[i0])
        a = min(1.0, max(0.0, (t - float(times[i0])) / span)) if span > 0 else 0.0
        for c in self.cut_times:
            if float(times[i0]) < c - 1e-9 and c <= float(times[i1]) + 1e-9:
                a = 0.0 if t < c - 1e-9 else 1.0
                break
        return i0, i1, a

    def mask_at(self, t: float):
        """The matte at time t (1/MASK_DIV, 0..1), blended between the two
        samples around it (the moving preview's approximate cut) — only
        within t's own shot."""
        import numpy as np

        i0, i1, a = self.blend_at(t)
        m = (self.masks[i0].astype(np.float32) * (1 - a) + self.masks[i1].astype(np.float32) * a) / 255.0
        m[self._bars8()] = 0.0
        return m

    def _box_cells(self, box: dict) -> tuple[int, int, int, int]:
        h8, w8 = self.masks.shape[1:]
        x0 = int(max(0, min(w8, float(box["x"]) * w8)))
        y0 = int(max(0, min(h8, float(box["y"]) * h8)))
        x1 = int(max(x0 + 1, min(w8, -(-(float(box["x"]) + float(box["w"])) * w8 // 1))))
        y1 = int(max(y0 + 1, min(h8, -(-(float(box["y"]) + float(box["h"])) * h8 // 1))))
        return x0, y0, x1, y1

    def overlap(self, box: dict, t0: float, t1: float) -> float:
        """The largest share of `box` (fractions) the speaker covers at any
        sample in [t0, t1]; the bars are never the speaker."""
        import numpy as np

        idx = self.samples_in(t0, t1)
        if not idx:
            return 0.0
        x0, y0, x1, y1 = self._box_cells(box)
        cells = self.masks[idx][:, y0:y1, x0:x1] > int(PERSON * 255)
        cells &= ~self._bars8()[y0:y1, x0:x1][None]
        if not cells.size:
            return 0.0
        return round(float(cells.reshape(len(idx), -1).mean(axis=1).max()), 4)

    def head_clear(self, box: dict, t0: float, t1: float, grow: float = HEAD_GROW) -> bool:
        """True when `box` (fractions) stays off the head box, grown by
        `grow` x W, at every sample in [t0, t1]."""
        bx0, by0, bx1, by1 = _px(box, self.width, self.height)
        g = grow * self.width
        for i in self.samples_in(t0, t1):
            hx0, hy0, hx1, hy1 = (int(v) for v in self.heads[i])
            if hx0 < 0:
                continue
            if not (bx1 <= hx0 - g or bx0 >= hx1 + g or by1 <= hy0 - g or by0 >= hy1 + g):
                return False
        return True

    def inside(self, box: dict) -> bool:
        """True when `box` lies inside the frame."""
        e = 1e-4
        return (float(box["x"]) >= -e and float(box["y"]) >= -e
                and float(box["x"]) + float(box["w"]) <= 1 + e and float(box["y"]) + float(box["h"]) <= 1 + e)

    def feasible(self, box: dict, t0: float, t1: float, *, max_overlap: float = OVERLAP_MAX,
                 grow: float = HEAD_GROW) -> bool:
        """The hard rules over a window: inside the frame, off the (grown)
        head, and at most `max_overlap` of it covered by the speaker."""
        return self.inside(box) and self.head_clear(box, t0, t1, grow) \
            and self.overlap(box, t0, t1) <= max_overlap

    def cover(self, box: dict, t0: float, t1: float, *, max_overlap: float = OVERLAP_MAX,
              grow: float = HEAD_GROW) -> Optional[str]:
        """What the speaker does to `box` (fractions) over [t0, t1]: "face"
        when it meets her (grown) head at some sample, "speaker" when she
        covers more than `max_overlap` of it, None when it stays clear — the
        outline a box dragged by hand shows (green, amber, red), and what a
        box of the user's kept over her says."""
        if not self.head_clear(box, t0, t1, grow):
            return "face"
        if self.overlap(box, t0, t1) > max_overlap:
            return "speaker"
        return None

    def first_blocked(self, box: dict, t0: float, t1: float, *, max_overlap: float = OVERLAP_MAX,
                      grow: float = HEAD_GROW) -> Optional[float]:
        """The first sample time in [t0, t1] where `box` breaks a hard rule
        (for "the speaker's hands are up at 5.2 s"), or None."""
        for i in self.samples_in(t0, t1):
            t = float(self.times[i])
            if not self.feasible(box, t, t, max_overlap=max_overlap, grow=grow):
                return round(t, 2)
        return None

    def calm(self, spot_or_box: dict) -> float:
        """1 - Sobel edge density on the first frame under a spot (its
        stored value) or any box (fractions)."""
        if "box" in spot_or_box and spot_or_box.get("calm") is not None:
            return float(spot_or_box["calm"])
        box = spot_or_box.get("box", spot_or_box)
        img = self._frame()
        if img is None:
            return 1.0
        return calm_of(img, _px(box, self.width, self.height))

    def _frame(self):
        if self._frame_img is None and self.frame0 is not None and Path(self.frame0).is_file():
            import cv2

            self._frame_img = cv2.imread(str(self.frame0), cv2.IMREAD_COLOR)
        return self._frame_img

    def sized(self, spot: dict, size: Optional[float]) -> dict:
        """The spot's box at another size (a share of the short side: S 0.12,
        M 0.16, L 0.20; None = as the grid has it), keeping its pinned side
        (a left-column spot keeps its left edge, a corner its corner, a bar
        spot its centre) and staying inside the frame. A bar spot scales
        from its own size, so L in a bar is a quarter larger than M there."""
        W, H = self.width, self.height
        short = min(W, H)
        x0, y0, x1, y1 = (float(spot["box"]["x"]) * W, float(spot["box"]["y"]) * H,
                          (float(spot["box"]["x"]) + float(spot["box"]["w"])) * W,
                          (float(spot["box"]["y"]) + float(spot["box"]["h"])) * H)
        L0 = x1 - x0
        if not size:
            return dict(spot["box"])
        L = L0 * (float(size) / SPOT_FRAC) if spot["group"] in ("top", "bottom") and self.layout == "letterbox" \
            else float(size) * short
        pin = spot.get("pin", "c")
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if pin == "l":
            nx, ny = x0, cy - L / 2
        elif pin == "r":
            nx, ny = x1 - L, cy - L / 2
        elif pin == "tl":
            nx, ny = x0, y0
        elif pin == "tr":
            nx, ny = x1 - L, y0
        else:
            nx, ny = cx - L / 2, cy - L / 2
        nx = min(max(0.0, nx), W - L)
        ny = min(max(0.0, ny), H - L)
        return _frac(nx, ny, L, L, W, H)


# ---------------------------------------------------------------------------
# Building and caching
# ---------------------------------------------------------------------------

def version_for(cuts) -> int:
    """The track version a clip with these jump cuts is sampled by."""
    return TRACK_VERSION if cuts else NO_CUT_VERSION


def track_key(base_clip: Path, cuts: Optional[list[int]] = None) -> Optional[str]:
    """The track's key: its clip's bytes, the matte model and the sampling
    (its version: a clip with a jump cut is sampled either side of it).
    `cuts` default to the clip's own (normalize_scene's description)."""
    from .. import speaker_front
    from . import stages as st

    d = st.digest(Path(base_clip))
    if d is None:
        return None
    version = version_for(_cuts_of(base_clip) if cuts is None else cuts)
    body = json.dumps([version, d, speaker_front.MODNET_REPO, speaker_front.MODNET_REVISION,
                       speaker_front.MATTE_SHORT, SAMPLE_FPS, MASK_DIV], separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]


def _save(track: Track, work_dir: Path) -> None:
    import numpy as np

    work_dir.mkdir(parents=True, exist_ok=True)
    bars = track.bars
    tag = f"{os.getpid()}.{threading.get_ident()}"
    tmp = work_dir / f"track.{tag}.tmp.npz"
    np.savez_compressed(
        tmp, times=np.asarray(track.times, np.float32), heads=np.asarray(track.heads, np.int32),
        masks=np.asarray(track.masks, np.uint8),
        bars=np.packbits(bars.astype(np.uint8), axis=None) if bars is not None else np.zeros(0, np.uint8),
    )
    os.replace(tmp, work_dir / "track.npz")
    meta = {
        "version": version_for(track.cuts), "key": track.key, "width": track.width, "height": track.height,
        "fps": track.fps, "frames": track.frames, "step": track.step, "layout": track.layout,
        "picture": list(track.picture), "spots": track.spots, "has_bars": bars is not None,
        "frame0": Path(track.frame0).name if track.frame0 else None,
        "anchors_available": track.anchors_available, "samples": int(len(track.times)),
        "cuts": [int(c) for c in track.cuts],
    }
    tmp = work_dir / f"track.{tag}.tmp.json"
    tmp.write_text(json.dumps(meta, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, work_dir / "track.json")


def load(work_dir: Path, key: Optional[str] = None) -> Optional[Track]:
    """The cached track in work_dir (when its key matches `key`, if given),
    or None."""
    import numpy as np

    work_dir = Path(work_dir)
    try:
        meta = json.loads((work_dir / "track.json").read_text(encoding="utf-8"))
        if meta.get("version") not in (NO_CUT_VERSION, TRACK_VERSION) or (key is not None and meta.get("key") != key):
            return None
        with np.load(work_dir / "track.npz", allow_pickle=False) as z:
            times, heads, masks, packed = z["times"], z["heads"], z["masks"], z["bars"]
    except Exception:  # noqa: BLE001 — a missing or broken cache is built again
        return None
    W, H = int(meta["width"]), int(meta["height"])
    bars = None
    if meta.get("has_bars") and packed.size:
        bars = np.unpackbits(packed, count=W * H).reshape(H, W).astype(bool)
    frame0 = work_dir / meta["frame0"] if meta.get("frame0") else None
    return Track(width=W, height=H, fps=float(meta["fps"]), frames=int(meta["frames"]),
                 step=int(meta["step"]), times=times, heads=heads, masks=masks, bars=bars,
                 layout=meta["layout"], picture=tuple(meta["picture"]), spots=meta["spots"],
                 frame0=frame0, key=meta["key"], cuts=[int(c) for c in meta.get("cuts") or []])


def _bars_of(pictures: list, width: int, height: int) -> Any:
    """The bars two of the first / middle / last samples agree on (their
    intersection: outside the union of their pictures), or None. Each entry
    of `pictures` is a sample's picture box inside its bars, or None when it
    has no bars (or is black)."""
    import numpy as np

    if not pictures:
        return None
    picks = [pictures[0], pictures[len(pictures) // 2], pictures[-1]]
    found = [p for p in picks if p is not None]
    if len(found) < 2:
        return None
    y0, y1 = min(p[0] for p in found), max(p[1] for p in found)
    x0, x1 = min(p[2] for p in found), max(p[3] for p in found)
    bars = np.ones((height, width), dtype=bool)
    bars[y0:y1, x0:x1] = False
    return bars if bars.any() else None


def _cuts_of(base_clip: Path) -> list[int]:
    """The clip's jump cuts as normalize_scene described them beside it —
    none for a clip it did not describe (no detector is run here)."""
    from .. import rebrand_media as media

    info = media.scene_info(Path(base_clip))
    return [int(c) for c in (info or {}).get("cuts") or []]


def build(base_clip: Path, work_dir: Path, *, matte: Optional[Callable[[Any], Any]] = None,
          cuts: Optional[list[int]] = None) -> Track:
    """The scene's Track (module docstring), cached in work_dir. Blocking
    (~0.45 s of MODNet per sample): call it from a thread. Raises when the
    local matte cannot run — there is no paid fallback here.

    `matte` (BGR frame -> float HxW 0..1) replaces speaker_front.person_matte
    (tests); `cuts` (frames) are the clip's own jump cuts (default: as
    normalize_scene described them beside it)."""
    import cv2
    import numpy as np

    from .. import pattern_planner
    from .. import rebrand_pattern_ref as pattern_ref
    from .. import speaker_front

    base_clip, work_dir = Path(base_clip), Path(work_dir)
    cuts = sorted({int(c) for c in (_cuts_of(base_clip) if cuts is None else cuts) if int(c) > 0})
    key = track_key(base_clip, cuts)
    with _LOCK:
        known = load(work_dir, key) if key else None
    if known is not None:
        return known
    matte = matte or speaker_front.person_matte
    # Either side of each cut: the last frame of its shot, the next one's first.
    extra = {f for c in cuts for f in (c - 1, c)}
    t_start = time.time()
    cap = cv2.VideoCapture(str(base_clip))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {base_clip.name}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    fps = fps if fps > 1 else 30.0
    step = max(1, int(round(fps / SAMPLE_FPS)))
    # Only what each sample leaves behind is kept (its small matte, head box
    # and picture box), plus the first frame worth showing: a 30 s scene's
    # full frames would be ~170 MB.
    times, heads, masks, pictures = [], [], [], []
    first = None
    W = H = 0
    i = 0
    try:
        while True:
            ok, f = cap.read()
            if not ok:
                break
            grid = i % step == 0
            if grid or i in extra:
                H, W = f.shape[:2]
                m = np.clip(np.asarray(matte(f), dtype=np.float32), 0.0, 1.0)
                hb, _src = pattern_planner.head_box(np.ascontiguousarray(f[:, :, ::-1]), m)
                small = cv2.resize(m, (max(1, W // MASK_DIV), max(1, H // MASK_DIV)),
                                   interpolation=cv2.INTER_AREA)
                times.append(i / fps)
                heads.append(list(hb) if hb else [-1, -1, -1, -1])
                masks.append(np.clip(np.rint(small * 255), 0, 255).astype(np.uint8))
            if grid:
                # The bars and the first frame from the grid's samples only:
                # the room read (the spots, frame0) is the same with or
                # without the cut samples.
                b = pattern_ref.bar_mask(f)
                pictures.append(picture_of(b, W, H) if b is not None else None)
                # The first sample, or the first that is not black when the
                # scene fades in.
                if first is None or (float(first.mean()) <= 16.0 < float(f.mean())):
                    first = f.copy()
            i += 1
    finally:
        cap.release()
    if first is None:
        raise RuntimeError(f"no frames in {base_clip.name}")
    bars = _bars_of(pictures, W, H)
    picture = picture_of(bars, W, H)
    layout = layout_of(W, H, picture, bars is not None)
    spots = spot_grid(W, H, picture if layout == "letterbox" else None, layout)
    frame0 = work_dir / "track_frame0.png"
    work_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(frame0), first)
    for s in spots:
        s["calm"] = calm_of(first, _px(s["box"], W, H))
    track = Track(width=W, height=H, fps=float(fps), frames=i, step=step,
                  times=np.asarray(times, np.float32), heads=np.asarray(heads, np.int32),
                  masks=np.stack(masks), bars=bars, layout=layout, picture=picture, spots=spots,
                  frame0=frame0, key=key or "", cuts=list(cuts))
    if key:
        with _LOCK:
            _save(track, work_dir)
    log.info("[speaker_track] %s: %dx%d %s, %d samples, %d spots, %.1fs", base_clip.name, W, H, layout,
             len(times), len(spots), time.time() - t_start)
    return track
