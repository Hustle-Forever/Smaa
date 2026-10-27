"""How the camera moves over a scene, so a pattern painted "in the room" can
move with it — the camera track.

  build(clip, cache_dir, bank=)   the scene clip's CameraTrack, cached as
                                  cache_dir/camera_<key>.npz (key: the clip's
                                  bytes, its matte bank, its cuts and bars,
                                  CAMERA_VERSION)
  track.at(i)                     frame i's 2x3 matrix: a point on frame 0
                                  (pixels) to where that spot of the room is
                                  on frame i
  track.public()                  the studio's copy: per frame [a, b, tx, ty]
                                  (M = [[a, -b, tx], [b, a, ty]]), the cuts,
                                  where it was held, what to tell the owner

The owner's talking-head clips are a slow push-in, not a pan: run 815 zooms
~12% about the frame's centre over 8 s, run 806 the same, rotation 0.00 ±
0.03°. A point at the frame's edge moves 34-61 px by 4 s, so a translation
alone is wrong, and so is phase correlation (it failed on the zoom outright).
The model is a SIMILARITY (scale, turn, shift: four numbers), which is all the
footage shows and can never fold a flat pattern into a false perspective.

How it is found, per shot (media.cuts_of splits the clip at its jump cuts):

  1. every frame shrunk to TRACK_LONG on its long side (360x640 for
     720x1280), grey;
  2. the room only: not the speaker (the matte bank's union, > 0.3, grown
     3% of the short side) and not the letterbox bars (grown the same);
  3. Shi-Tomasi corners on the shot's first frame, followed frame to frame
     by pyramidal Lucas-Kanade, each checked backwards (< 1 px) and dropped
     once it walks onto the speaker;
  4. the similarity from the shot's FIRST-frame positions to today's, by
     RANSAC (1.5 px) — always from the start, so no error piles up the way
     chained frame-to-frame matrices do; when too few are left (40% of the
     start, or 60) new corners are found and mapped back to the start;
  5. smoothed lightly: a centred 5-frame mean of (shift, log scale, turn),
     inside the shot only.

Measured on 815 against ECC: within 1 px and 0.001 of scale, at 5-7 ms a
frame (ECC costs 60-105 ms). A shot with too few corners to follow (a plain
wall) is followed by ECC instead (affine, a quarter size, warm-started,
projected onto a similarity; its correlation must be ≥ 0.90).

At a cut the next shot is followed on its own and CHAINED to the one before
(M_i = T_shot(i) · M_before-the-cut): a pattern keeps its place on screen
across the cut, then moves with the new shot's room. The camera is HELD —
the patterns stay put on screen — only where it is lost: too few points
agree, the scale runs past ±35% or the turn past 8°, or one frame jumps by
more than 3% of scale or ~20 px. A clip that can't be followed from its
first frame is "still" (the identity throughout), and says so.

~2 s for an 8 s scene on this CPU, once: it is cached beside the matte bank
(local only, REBRAND_ROOT/_cache/scene/), keyed by the clip's bytes, so every
preview, every version and a run share it. Nothing here calls a model.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .mattes import Cancelled, MatteBank

log = logging.getLogger("archivist.rebrand.harness.camera")

# Bumped whenever a track can change for the same clip.
CAMERA_VERSION = 1
# Features are followed on the frame shrunk to this long side (half of a
# 720x1280 scene), ECC on this one (a quarter).
TRACK_LONG = 640
ECC_LONG = 320
# Shi-Tomasi corners: how many, how strong (of the best), how far apart (px).
MAX_CORNERS = 400
CORNER_QUALITY = 0.01
CORNER_DIST = 8
# Fewer corners than this on a shot's first frame: ECC follows it instead.
MIN_CORNERS = 40
LK_WIN = 21
LK_LEVELS = 3
BACK_MAX_PX = 1.0                 # a point followed back must land within this
RANSAC_PX = 1.5
# New corners once fewer than this share of the shot's start (or this many)
# are left.
REDETECT_SHARE = 0.4
REDETECT_MIN = 60
# Lost: fewer RANSAC inliers than this, an ECC correlation under this, a
# scale past 1 ± SCALE_MAX, a turn past TURN_MAX_DEG, or one frame's jump past
# JUMP_SCALE of scale or JUMP_FRAC of the short side (~20 px at 720).
MIN_INLIERS = 25
ECC_MIN_CC = 0.90
SCALE_MAX = 0.35
TURN_MAX_DEG = 8.0
JUMP_SCALE = 0.03
JUMP_FRAC = 20.0 / 720.0
# The speaker, as the bank's union at the tracking size: this level and up,
# grown this share of the short side (the bars too).
SPEAKER_LEVEL = 0.3
SPEAKER_GROW = 0.03
SMOOTH = 5
# A held stretch longer than this is said on the version.
HOLD_WARN_SEC = 0.5

STILL_WARNING = "The camera couldn't be followed, so the patterns hold still on screen."
# build()'s `bars` when none are given: the matte bank's own read of them.
FROM_BANK: Any = object()


def _clock(sec: float) -> str:
    """0:07.8"""
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def held_warning(sec: float) -> str:
    return f"The camera can't be followed from {_clock(sec)}, so the patterns hold still on screen from there."


@dataclass
class CameraTrack:
    """A scene's camera (module docstring). `mats[i]` maps a frame-0 pixel
    to frame i's; frame 0's is the identity exactly."""
    key: str
    fps: float
    n: int
    width: int
    height: int
    mats: Any                                      # (n, 2, 3) float32
    method: str                                    # "features" | "ecc" | "still"
    shots: list[tuple[int, int]] = field(default_factory=list)   # [start, end) of each shot
    methods: list[str] = field(default_factory=list)             # each shot's
    held: list[tuple[int, int]] = field(default_factory=list)    # [start, end) held stretches
    quality: Any = None                            # (n,) inlier share, or ECC correlation; 0 held
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def cuts(self) -> list[int]:
        return [s for s, _e in self.shots[1:]]

    @property
    def held_from(self) -> Optional[int]:
        return self.held[0][0] if self.held else None

    def at(self, i: int) -> Any:
        """Frame i's matrix (2x3 float32; the clip's last past its end)."""
        return self.mats[min(max(0, int(i)), self.n - 1)]

    def scale_at(self, i: int) -> float:
        m = self.at(i)
        return float(math.hypot(float(m[0, 0]), float(m[1, 0])))

    def public(self) -> dict:
        import numpy as np

        mats = np.asarray(self.mats, dtype=np.float64)
        c = np.array([(self.width - 1) / 2.0, (self.height - 1) / 2.0])
        centre = mats[:, :, :2] @ c + mats[:, :, 2]
        scales = np.hypot(mats[:, 0, 0], mats[:, 1, 0])
        return {
            "version": CAMERA_VERSION, "fps": round(self.fps, 3), "n": self.n,
            "width": self.width, "height": self.height, "method": self.method,
            # [a, b, tx, ty] per frame: M = [[a, -b, tx], [b, a, ty]].
            "m": [[round(float(m[0, 0]), 4), round(float(m[1, 0]), 4),
                   round(float(m[0, 2]), 2), round(float(m[1, 2]), 2)] for m in mats],
            "cuts_sec": [round(s / self.fps, 3) for s in self.cuts],
            "held_from_sec": round(self.held_from / self.fps, 3) if self.held_from is not None else None,
            "warnings": list(self.warnings),
            "zoom_max": round(float(scales.max()), 4) if len(scales) else 1.0,
            # How far the frame's centre moves (the pan; a zoom about the
            # centre moves it not at all).
            "shift_max_px": round(float(np.hypot(*(centre - c).T).max()), 2) if len(centre) else 0.0,
        }


# ---------------------------------------------------------------------------
# Similarities
# ---------------------------------------------------------------------------

def _params(m) -> tuple[float, float, float, float]:
    """(tx, ty, log scale, turn in radians) of a 2x3 similarity."""
    a, b = float(m[0, 0]), float(m[1, 0])
    return float(m[0, 2]), float(m[1, 2]), math.log(max(1e-9, math.hypot(a, b))), math.atan2(b, a)


def _similarity(tx: float, ty: float, log_s: float, turn: float):
    import numpy as np

    s = math.exp(log_s)
    a, b = s * math.cos(turn), s * math.sin(turn)
    return np.array([[a, -b, tx], [b, a, ty]], dtype=np.float64)


def _as_similarity(m):
    """The similarity nearest a 2x3 affine (ECC's): its shear dropped."""
    import numpy as np

    m = np.asarray(m, dtype=np.float64)
    a = (m[0, 0] + m[1, 1]) / 2.0
    b = (m[1, 0] - m[0, 1]) / 2.0
    return np.array([[a, -b, m[0, 2]], [b, a, m[1, 2]]], dtype=np.float64)


def _to_full(m, s: float):
    """A similarity found on frames shrunk by `s` (pixel centres: a shrunk
    pixel x covers full pixels around (x + 0.5) / s - 0.5), in full-size
    pixels."""
    import numpy as np

    m = np.asarray(m, dtype=np.float64).copy()
    if s == 1.0:
        return m
    lin = m[:, :2]
    k = 0.5 / s - 0.5
    m[:, 2] = m[:, 2] / s + k * ((np.eye(2) - lin) @ np.ones(2))
    return m


def _h(m):
    """2x3 -> 3x3."""
    import numpy as np

    return np.vstack([np.asarray(m, dtype=np.float64), [0.0, 0.0, 1.0]])


def _smooth(mats: list, start: int, end: int) -> None:
    """A centred SMOOTH-frame mean of (shift, log scale, turn) over
    mats[start:end], in place; the window shrinks evenly at both ends, so
    the shot's first frame stays exactly where it is."""
    if end - start < 3:
        return
    half = SMOOTH // 2
    ps = [_params(mats[i]) for i in range(start, end)]
    out = []
    for j in range(len(ps)):
        r = min(half, j, len(ps) - 1 - j)
        win = ps[j - r:j + r + 1]
        out.append(_similarity(*(sum(p[q] for p in win) / len(win) for q in range(4))))
    for j, m in enumerate(out):
        mats[start + j] = m


# ---------------------------------------------------------------------------
# Building and caching
# ---------------------------------------------------------------------------

def camera_key(bank: MatteBank, cuts: Sequence[int], picture: Sequence[int]) -> str:
    from . import stages as st

    return st.key(bank=bank.key, cuts=[int(c) for c in cuts], picture=[int(v) for v in picture],
                  v=CAMERA_VERSION, track=[TRACK_LONG, ECC_LONG, MAX_CORNERS, CORNER_QUALITY, CORNER_DIST,
                                           MIN_CORNERS, RANSAC_PX, MIN_INLIERS, ECC_MIN_CC, SMOOTH])


def _save(track: CameraTrack, path: Path) -> None:
    import numpy as np

    meta = {"version": CAMERA_VERSION, "key": track.key, "fps": track.fps, "n": track.n,
            "width": track.width, "height": track.height, "method": track.method,
            "shots": [list(s) for s in track.shots], "methods": track.methods,
            "held": [list(h) for h in track.held], "warnings": track.warnings,
            "seconds": round(track.seconds, 2)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.npz")
    np.savez_compressed(tmp, meta=np.array(json.dumps(meta)),
                        mats=np.asarray(track.mats, np.float32), quality=np.asarray(track.quality, np.float32))
    os.replace(tmp, path)


def load(path: Path, key: Optional[str] = None) -> Optional[CameraTrack]:
    """The track kept at `path` (when its key is `key`, if given), or None."""
    import numpy as np

    try:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            mats, quality = z["mats"], z["quality"]
    except Exception:  # noqa: BLE001 — a missing or broken track is made again
        return None
    if meta.get("version") != CAMERA_VERSION or (key is not None and meta.get("key") != key):
        return None
    return CameraTrack(key=meta["key"], fps=float(meta["fps"]), n=int(meta["n"]), width=int(meta["width"]),
                       height=int(meta["height"]), mats=mats, method=meta["method"],
                       shots=[tuple(s) for s in meta["shots"]], methods=list(meta.get("methods") or []),
                       held=[tuple(h) for h in meta["held"]], quality=quality,
                       warnings=list(meta.get("warnings") or []), seconds=float(meta.get("seconds") or 0.0))


def path_for(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / f"camera_{key}.npz"


class _Room:
    """Where the room shows on a frame, at one size: not the speaker (the
    bank's union, grown) and not the bars (grown)."""

    def __init__(self, bank: MatteBank, bars: Any, width: int, height: int):
        import cv2
        import numpy as np

        self.bank, self.w, self.h = bank, int(width), int(height)
        g = max(1, int(round(SPEAKER_GROW * min(self.w, self.h))))
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1))
        self.bars = None
        if bars is not None:
            small = cv2.resize(np.asarray(bars, dtype=np.uint8), (self.w, self.h), interpolation=cv2.INTER_NEAREST)
            self.bars = cv2.dilate(small, self.kernel) > 0

    def mask(self, i: int) -> Any:
        """uint8 HxW at this size: 255 on the room."""
        import cv2
        import numpy as np

        speaker = self.bank.coarse(i, self.w, self.h) >= int(round(SPEAKER_LEVEL * 255))
        off = cv2.dilate(speaker.astype(np.uint8), self.kernel) > 0
        if self.bars is not None:
            off |= self.bars
        return np.where(off, 0, 255).astype(np.uint8)


def build(clip: Path, cache_dir: Path, *, bank: MatteBank, cuts: Optional[Sequence[int]] = None,
          bars: Any = FROM_BANK, progress: Optional[Callable[[int, int], None]] = None,
          cancel: Optional[Callable[[], bool]] = None) -> CameraTrack:
    """The clip's camera track (module docstring), from its matte bank
    (filled first when it is not). `cuts` default to the bank's, `bars` to
    the bank's own read of them (None: no bars). Cached in cache_dir.
    Blocking (~2 s for an 8 s scene): call it from a thread. `progress(done,
    total)` per frame; `cancel()` returning True stops it (Cancelled)."""
    import cv2
    import numpy as np

    from . import speaker_track

    clip, cache_dir = Path(clip), Path(cache_dir)
    if bars is FROM_BANK:
        bars = bank.read_bars()
    cuts = bank.cuts if cuts is None else sorted({int(c) for c in cuts if 0 < int(c) < bank.n})
    W, H, n, fps = bank.width, bank.height, bank.n, bank.fps
    picture = speaker_track.picture_of(bars, W, H) if bars is not None else (0, H, 0, W)
    key = camera_key(bank, cuts, picture)
    path = path_for(cache_dir, key)
    known = load(path, key)
    if known is not None:
        return known
    if bank.missing():
        bank.fill(cancel=cancel)
    t_start = time.time()
    s_t = min(1.0, TRACK_LONG / max(W, H))
    tw, th = max(8, int(round(W * s_t))), max(8, int(round(H * s_t)))
    s_e = min(1.0, ECC_LONG / max(W, H))
    ew, eh = max(8, int(round(W * s_e))), max(8, int(round(H * s_e)))
    room_t = _Room(bank, bars, tw, th)
    room_e = _Room(bank, bars, ew, eh)
    short = min(W, H)
    centre = np.array([(W - 1) / 2.0, (H - 1) / 2.0])
    bounds = [0, *cuts, n]
    shots = [(bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1) if bounds[k + 1] > bounds[k]]
    shot_of = {}
    for k, (s, e) in enumerate(shots):
        for i in range(s, e):
            shot_of[i] = k
    # Each shot's own track T (frame s px -> frame i px, full size), then
    # chained.
    T: list = [np.eye(2, 3, dtype=np.float64) for _ in range(n)]
    quality = np.zeros(n, np.float32)
    methods: list[str] = []
    lost_at: dict[int, int] = {}           # shot -> first held frame
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-5)

    def lost(k: int, i: int) -> None:
        lost_at[k] = i
        T[i] = T[i - 1].copy()

    def sane(k: int, i: int, m) -> bool:
        """m (full size) is believable after T[i-1]."""
        _tx, _ty, log_s, turn = _params(m)
        if abs(math.exp(log_s) - 1.0) > SCALE_MAX or abs(math.degrees(turn)) > TURN_MAX_DEG:
            return False
        prev = T[i - 1]
        if abs(math.exp(log_s - _params(prev)[2]) - 1.0) > JUMP_SCALE:
            return False
        moved = (m[:, :2] @ centre + m[:, 2]) - (prev[:, :2] @ centre + prev[:, 2])
        return float(np.hypot(*moved)) <= JUMP_FRAC * short

    state: dict = {}
    cap = cv2.VideoCapture(str(clip))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {clip.name}")
    prev_gray = None
    try:
        i = 0
        while i < n:
            if cancel is not None and cancel():
                raise Cancelled()
            ok, f = cap.read()
            if not ok:
                break
            k = shot_of[i]
            s, _e = shots[k]
            grey = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            g_t = cv2.resize(grey, (tw, th), interpolation=cv2.INTER_AREA) if s_t < 1.0 else grey
            if i == s:
                # A shot's first frame: its own start, found by features or,
                # with too few, by ECC.
                state = {}
                mask = room_t.mask(i)
                pts = cv2.goodFeaturesToTrack(g_t, MAX_CORNERS, CORNER_QUALITY, CORNER_DIST, mask=mask)
                if pts is not None and len(pts) >= MIN_CORNERS:
                    methods.append("features")
                    state.update(kind="features", orig=pts.astype(np.float32), cur=pts.astype(np.float32),
                                 start=len(pts))
                else:
                    methods.append("ecc")
                    g_e = cv2.resize(grey, (ew, eh), interpolation=cv2.INTER_AREA) if s_e < 1.0 else grey
                    state.update(kind="ecc", template=g_e, warp=np.eye(2, 3, dtype=np.float32))
                quality[i] = 1.0
            elif k in lost_at:
                T[i] = T[i - 1].copy()
            elif state.get("kind") == "features":
                cur = state["cur"]
                nxt, st1, _ = cv2.calcOpticalFlowPyrLK(prev_gray, g_t, cur, None, winSize=(LK_WIN, LK_WIN),
                                                       maxLevel=LK_LEVELS)
                back, st2, _ = cv2.calcOpticalFlowPyrLK(g_t, prev_gray, nxt, None, winSize=(LK_WIN, LK_WIN),
                                                        maxLevel=LK_LEVELS)
                good = ((st1.ravel() == 1) & (st2.ravel() == 1)
                        & (np.linalg.norm((back - cur).reshape(-1, 2), axis=1) < BACK_MAX_PX))
                mask = room_t.mask(i)
                p = nxt.reshape(-1, 2)
                xi = np.clip(np.rint(p[:, 0]).astype(int), 0, tw - 1)
                yi = np.clip(np.rint(p[:, 1]).astype(int), 0, th - 1)
                good &= mask[yi, xi] > 0
                orig, cur = state["orig"][good], nxt[good]
                m_t, inl = (None, None)
                if len(cur) >= 3:
                    m_t, inl = cv2.estimateAffinePartial2D(orig, cur, method=cv2.RANSAC,
                                                           ransacReprojThreshold=RANSAC_PX)
                count = int(inl.sum()) if inl is not None else 0
                if m_t is None or count < MIN_INLIERS or not sane(k, i, _to_full(m_t, s_t)):
                    lost(k, i)
                else:
                    T[i] = _to_full(m_t, s_t)
                    quality[i] = count / max(1, len(cur))
                    keep = inl.ravel() == 1
                    orig, cur = orig[keep], cur[keep]
                    if len(cur) < max(REDETECT_MIN, REDETECT_SHARE * state["start"]):
                        # Fresh corners on today's room, mapped back to the
                        # shot's start through today's matrix.
                        fresh = cv2.goodFeaturesToTrack(g_t, MAX_CORNERS, CORNER_QUALITY, CORNER_DIST, mask=mask)
                        if fresh is not None and len(fresh) > len(cur):
                            inv = cv2.invertAffineTransform(m_t)
                            orig = cv2.transform(fresh, inv).astype(np.float32)
                            cur = fresh.astype(np.float32)
                            state["start"] = len(cur)
                    state["orig"], state["cur"] = orig, cur
            else:
                g_e = cv2.resize(grey, (ew, eh), interpolation=cv2.INTER_AREA) if s_e < 1.0 else grey
                cc = float("nan")
                warp = state["warp"].copy()
                try:
                    cc, warp = cv2.findTransformECC(state["template"], g_e, warp, cv2.MOTION_AFFINE, crit,
                                                    room_e.mask(i), 5)
                except cv2.error:
                    cc = float("nan")
                m = _to_full(_as_similarity(warp), s_e)
                if not (cc >= ECC_MIN_CC) or not sane(k, i, m):
                    lost(k, i)
                else:
                    state["warp"] = warp
                    T[i] = m
                    quality[i] = cc
            if progress is not None:
                progress(i + 1, n)
            prev_gray = g_t
            i += 1
    finally:
        cap.release()
    n_read = i
    for j in range(n_read, n):                # the clip decoded short: its last frame holds
        T[j] = T[max(0, j - 1)].copy()
    held: list[tuple[int, int]] = []
    shot_methods: list[str] = []
    for k, (s, e) in enumerate(shots):
        h = lost_at.get(k, e)
        if n_read < e:
            h = min(h, max(s + 1, n_read))
        _smooth(T, s, h)
        for j in range(h, e):
            T[j] = T[h - 1].copy() if h > s else np.eye(2, 3, dtype=np.float64)
        if h < e:
            held.append((h, e))
        shot_methods.append("still" if h <= s + 1 and e - s > 1 else methods[k] if k < len(methods) else "still")
    # Chained: each shot starts where the one before it left the room.
    mats = np.zeros((n, 2, 3), np.float64)
    before = np.eye(3)
    for k, (s, e) in enumerate(shots):
        for j in range(s, e):
            mats[j] = (_h(T[j]) @ before)[:2]
        before = _h(mats[e - 1])
    mats[0] = np.eye(2, 3)                     # frame 0 is itself, exactly
    for a, b in held:
        quality[a:b] = 0.0
    followed = [m for m in shot_methods if m != "still"]
    if not followed:
        method = "still"
    else:
        longest = max((e - s, m) for (s, e), m in zip(shots, shot_methods) if m != "still")
        method = longest[1]
    warnings: list[str] = []
    if method == "still" and n > 1:
        warnings.append(STILL_WARNING)
    else:
        for a, b in held:
            if (b - a) / fps > HOLD_WARN_SEC:
                warnings.append(held_warning(a / fps))
    track = CameraTrack(key=key, fps=float(fps), n=n, width=W, height=H, mats=mats.astype(np.float32),
                        method=method, shots=shots, methods=shot_methods, held=held, quality=quality,
                        warnings=warnings, seconds=time.time() - t_start)
    _save(track, path)
    log.info("[camera] %s: %s, zoom up to %.3f, %d shot(s)%s, %.1fs", clip.name, method,
             max(track.scale_at(j) for j in range(n)), len(shots),
             f", held from frame {track.held_from}" if track.held_from is not None else "", track.seconds)
    return track
