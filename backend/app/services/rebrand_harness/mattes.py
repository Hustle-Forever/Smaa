"""The speaker's matte for every frame of a scene, made once — the matte bank.

  MatteBank.open(clip, cache_dir)   the scene clip's bank, read back from
                                    cache_dir/mattes_<key>.npz when it is
                                    there (key: the clip's bytes, the MODNet
                                    model and its size, MATTE_EVERY)
  bank.fill()                       every keyframe it needs, in one decode of
                                    the clip; saved whole or not at all
  bank.at(i, bars=)                 frame i's matte, float32 HxW 0..1

The in-house engine cuts its graphics by the speaker on EVERY frame, and the
studio's stills, its moving preview and a run must all cut by the very same
matte — so it is worked out once per scene clip and kept, never per preview
or per version. MODNet runs on every MATTE_EVERY-th frame (the keyframes)
plus each shot's first and last frame, exactly the frames
speaker_front.matte_keys unions: frame i's matte is

    clean(keyframe a) ∪ clean(keyframe b),   (a, b) = matte_keys(i, n, every, cuts)

with speaker_front.clean (holes filled, grown, feathered) on each keyframe
scaled to the frame. Measured on run 815: the union misses 0.00% of the
speaker (worst frame 0.04%) and adds a 0.38% rim; ~80 keyframes of an 8 s
scene take ~9 s of MODNet once (every frame would be 27 s).

The bank keeps MODNet's OWN output (uint8, at the model's 448x256 for a
720x1280 frame: ~0.35 MB compressed for a scene), not the cleaned frames:
cleaning a keyframe at full size costs ~10 ms and is redone as asked, the
last few kept (CLEANED_KEPT).

The clip's jump cuts (media.cuts_of — what normalize_scene wrote beside it)
keep every union inside one shot, so a frame is never cut by another shot's
pose. The letterbox bars are never the speaker: at() empties the matte on the
`bars` it is given (bars_of reads them as the speaker track does: the first,
middle and last frames, two of three agreeing).

LOCAL MODNet only (speaker_front.raw_matte): the bank never pays for a matte
and never falls back to anything that does. Nothing here is uploaded: the
bank lives beside the scene's other analysis, REBRAND_ROOT/_cache/scene/.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger("archivist.rebrand.harness.mattes")

# Bumped whenever the bank's stored mattes can change for the same clip.
MATTE_BANK_VERSION = 1
# Cleaned keyframes kept at full size (each a float frame, ~3.7 MB at
# 720x1280): a frame's union needs two, the next frame shares one.
CLEANED_KEPT = 4
# A clip has a person in it when its median keyframe's matte (over half)
# covers more than this share of the frame: person_matte's own rule for a
# frame where it "finds no one". Only an uploaded intro or outro video is
# judged by it (house.person_of): on the reels' own cards MODNet's ghosts are
# far stronger (U6, 2026-10-01: 63 card bookends with no one in them read
# 0-64 % median coverage, 48 of them over 1 %; talking heads read 26-76 %).
PERSON_MIN = 0.01


class Cancelled(Exception):
    """fill() was asked to stop (its `cancel` said so)."""


def bank_key(clip: Path, every: int) -> Optional[str]:
    """The bank's key: the clip's bytes, the matte model, its size and the
    keyframe step. None when the clip is not there."""
    from .. import speaker_front
    from . import stages as st

    d = st.digest(Path(clip))
    if d is None:
        return None
    return st.key(clip=d, repo=speaker_front.MODNET_REPO, file=speaker_front.MODNET_FILE,
                  rev=speaker_front.MODNET_REVISION, short=speaker_front.MATTE_SHORT,
                  every=int(every), v=MATTE_BANK_VERSION)


def bars_of(frames: Sequence[Any]) -> Any:
    """The letterbox bars (bool HxW) two of the first / middle / last of
    `frames` agree on, or None — rebrand_pattern_ref.bar_mask on each, read
    as speaker_track reads its samples'."""
    from .. import rebrand_pattern_ref as pattern_ref
    from . import speaker_track

    if not frames:
        return None
    h, w = frames[0].shape[:2]
    pictures = []
    for f in frames:
        b = pattern_ref.bar_mask(f)
        pictures.append(speaker_track.picture_of(b, w, h) if b is not None else None)
    return speaker_track._bars_of(pictures, w, h)


def _count(clip: Path) -> tuple[int, int, int, float]:
    """(frames, width, height, fps) of a clip as OpenCV decodes it: the
    frames every other reader here counts."""
    import cv2

    cap = cv2.VideoCapture(str(clip))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {Path(clip).name}")
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        n = 0
        while cap.grab():
            n += 1
    finally:
        cap.release()
    return n, width, height, (fps if fps > 1 else 30.0)


def frames_of(clip: Path) -> int:
    """How many frames a clip has as a bank counts them (OpenCV, decoded):
    a bank of it has exactly this `n`. Blocking."""
    return _count(Path(clip))[0]


class MatteBank:
    """One scene clip's speaker mattes (module docstring). Thread-safe: a
    render and the studio may ask one bank at once."""

    def __init__(self, clip: Path, path: Optional[Path], *, key: str, n: int, width: int, height: int,
                 fps: float, every: int, cuts: Sequence[int],
                 raw: Optional[Callable[[Any], Any]] = None):
        self.clip = Path(clip)
        self.path = path                    # the cache file (None: not kept)
        self.key = key
        self.n = int(n)
        self.width = int(width)
        self.height = int(height)
        self.fps = float(fps)
        self.every = max(1, int(every))
        self.cuts = sorted({int(c) for c in cuts if 0 < int(c) < self.n})
        self.raw: dict[int, Any] = {}       # keyframe -> MODNet's own output, uint8
        self.bars: Any = None               # bool HxW, or None (read by fill / bars_of)
        self.bars_read = False
        self.seconds = 0.0                  # MODNet time spent filling
        self._raw_fn = raw
        self._lock = threading.RLock()
        self._cleaned: "OrderedDict[int, Any]" = OrderedDict()

    # --- opening and saving ---------------------------------------------------

    @classmethod
    def open(cls, clip: Path, cache_dir: Path, *, cuts: Optional[Sequence[int]] = None,
             every: Optional[int] = None, raw: Optional[Callable[[Any], Any]] = None) -> "MatteBank":
        """The clip's bank, with whatever cache_dir already holds of it.
        `cuts` default to the clip's own (media.cuts_of); `raw` (BGR frame ->
        float alpha 0..1 at any size) replaces speaker_front.raw_matte
        (tests). Blocking: it counts the clip's frames when nothing is kept."""
        from .. import rebrand_media as media
        from .. import speaker_front

        clip = Path(clip)
        every = int(every or speaker_front.MATTE_EVERY)
        key = bank_key(clip, every)
        if key is None:
            raise FileNotFoundError(clip)
        path = Path(cache_dir) / f"mattes_{key}.npz"
        cuts = list(cuts) if cuts is not None else media.cuts_of(clip)
        bank = cls._load(clip, path, key=key, every=every, cuts=cuts, raw=raw)
        if bank is None:
            n, width, height, fps = _count(clip)
            if n == 0:
                raise RuntimeError(f"no frames in {clip.name}")
            bank = cls(clip, path, key=key, n=n, width=width, height=height, fps=fps, every=every,
                       cuts=cuts, raw=raw)
        return bank

    @classmethod
    def _load(cls, clip: Path, path: Path, *, key: str, every: int, cuts: Sequence[int],
              raw: Optional[Callable[[Any], Any]]) -> Optional["MatteBank"]:
        import numpy as np

        try:
            with np.load(path, allow_pickle=False) as z:
                meta = json.loads(str(z["meta"]))
                keys, raws, packed = z["keys"], z["raws"], z["bars"]
        except Exception:  # noqa: BLE001 — a missing or broken bank is made again
            return None
        if meta.get("version") != MATTE_BANK_VERSION or meta.get("key") != key:
            return None
        bank = cls(clip, path, key=key, n=meta["n"], width=meta["width"], height=meta["height"],
                   fps=meta["fps"], every=every, cuts=cuts, raw=raw)
        bank.raw = {int(k): np.ascontiguousarray(raws[j]) for j, k in enumerate(keys)}
        bank.seconds = float(meta.get("seconds") or 0.0)
        if meta.get("bars_read"):
            bank.bars_read = True
            if meta.get("has_bars") and packed.size:
                W, H = bank.width, bank.height
                bank.bars = np.unpackbits(packed, count=W * H).reshape(H, W).astype(bool)
        return bank

    def save(self) -> None:
        """Written whole (a temp file renamed into place) or not at all."""
        import numpy as np

        if self.path is None:
            return
        with self._lock:
            keys = sorted(self.raw)
            if not keys:
                return
            meta = {"version": MATTE_BANK_VERSION, "key": self.key, "n": self.n, "width": self.width,
                    "height": self.height, "fps": self.fps, "every": self.every,
                    "bars_read": self.bars_read, "has_bars": self.bars is not None,
                    "seconds": round(self.seconds, 2)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.npz")
            np.savez_compressed(
                tmp, meta=np.array(json.dumps(meta)), keys=np.asarray(keys, np.int32),
                raws=np.stack([self.raw[k] for k in keys]),
                bars=(np.packbits(self.bars.astype(np.uint8), axis=None) if self.bars is not None
                      else np.zeros(0, np.uint8)))
            os.replace(tmp, self.path)

    # --- which frames -------------------------------------------------------------

    def keys_of(self, i: int) -> tuple[int, int]:
        """The two keyframes frame i's matte is the union of."""
        from .. import speaker_front

        i = min(max(0, int(i)), self.n - 1)
        return speaker_front.matte_keys(i, self.n, self.every, self.cuts)

    def keyframes(self) -> list[int]:
        """Every keyframe the clip's frames need, in order."""
        found: set[int] = set()
        for i in range(self.n):
            found.update(self.keys_of(i))
        return sorted(found)

    def missing(self) -> list[int]:
        with self._lock:
            return [k for k in self.keyframes() if k not in self.raw]

    def progress(self) -> tuple[int, int]:
        """(keyframes made, keyframes needed)."""
        needed = self.keyframes()
        with self._lock:
            return sum(1 for k in needed if k in self.raw), len(needed)

    @property
    def filled(self) -> bool:
        return not self.missing() and self.bars_read

    # --- making them ----------------------------------------------------------------

    def _matte(self, bgr) -> Any:
        """MODNet's own output for one frame, as the bank keeps it (uint8)."""
        import numpy as np

        from .. import speaker_front

        fn = self._raw_fn or speaker_front.raw_matte
        a = np.clip(np.asarray(fn(bgr), dtype=np.float32), 0.0, 1.0)
        return np.clip(np.rint(a * 255.0), 0, 255).astype(np.uint8)

    def _make(self, wanted: set[int], *, progress: Optional[Callable[[int, int], None]] = None,
              cancel: Optional[Callable[[], bool]] = None, read_bars: bool = False) -> int:
        """Decode the clip in order from its start (so each frame is exact)
        and matte the `wanted` frames; with `read_bars`, read the bars off
        the first, middle and last frames on the way. Returns how many were
        made."""
        import cv2

        with self._lock:
            wanted = {k for k in wanted if 0 <= k < self.n and k not in self.raw}
        bar_at = {0, self.n // 2, self.n - 1} if read_bars else set()
        if not wanted and not bar_at:
            return 0
        top = max(wanted | bar_at)
        done0, total = self.progress() if progress else (0, 0)
        made = 0
        bar_frames: dict[int, Any] = {}
        t0 = time.time()
        cap = cv2.VideoCapture(str(self.clip))
        if not cap.isOpened():
            raise RuntimeError(f"cannot open {self.clip.name}")
        try:
            i = 0
            while i <= top:
                if cancel is not None and cancel():
                    raise Cancelled()
                ok, f = cap.read()
                if not ok:
                    break
                if i in bar_at:
                    bar_frames[i] = f
                if i in wanted:
                    m = self._matte(f)
                    with self._lock:
                        self.raw[i] = m
                    made += 1
                    if progress is not None:
                        progress(done0 + made, total)
                i += 1
        finally:
            cap.release()
        with self._lock:
            self.seconds += time.time() - t0
            if read_bars:
                self.bars = bars_of([bar_frames[k] for k in sorted(bar_frames)]) if bar_frames else None
                self.bars_read = True
        return made

    def fill(self, *, progress: Optional[Callable[[int, int], None]] = None,
             cancel: Optional[Callable[[], bool]] = None) -> "MatteBank":
        """Every keyframe the clip needs (and its bars), in one decode, then
        saved. Blocking (~0.11 s of MODNet a keyframe): call it from a
        thread. `progress(done, total)` after each keyframe; `cancel()`
        returning True stops it (Cancelled) — what was made is kept."""
        wanted = set(self.missing())
        try:
            made = self._make(wanted, progress=progress, cancel=cancel, read_bars=not self.bars_read)
        finally:
            self.save()
        if made:
            log.info("[mattes] %s: %d keyframes of %d frames, %.1fs", self.clip.name, made, self.n,
                     self.seconds)
        return self

    def read_bars(self) -> Any:
        """The clip's letterbox bars (bool HxW) or None, read once."""
        if not self.bars_read:
            self._make(set(), read_bars=True)
            self.save()
        return self.bars

    # --- frame i ----------------------------------------------------------------------

    def _cleaned_key(self, k: int) -> Any:
        """Keyframe k's matte at the frame's size, cleaned (speaker_front.clean)."""
        import cv2
        import numpy as np

        from .. import speaker_front

        with self._lock:
            got = self._cleaned.get(k)
            if got is not None:
                self._cleaned.move_to_end(k)
                return got
            raw = self.raw.get(k)
        if raw is None:
            # Made on demand: a sparse frame of a bank not yet filled. A
            # render fills the bank first (this decodes from the start).
            self._make({k})
            self.save()
            with self._lock:
                raw = self.raw[k]
        up = cv2.resize(raw.astype(np.float32) / 255.0, (self.width, self.height),
                        interpolation=cv2.INTER_LINEAR)
        cleaned = speaker_front.clean(np.clip(up, 0.0, 1.0)).astype(np.float32)
        with self._lock:
            self._cleaned[k] = cleaned
            while len(self._cleaned) > CLEANED_KEPT:
                self._cleaned.popitem(last=False)
        return cleaned

    def at(self, i: int, *, bars: Any = None) -> Any:
        """Frame i's matte (float32 HxW, 0..1): the union of its two
        keyframes' cleaned mattes, never across a cut, zero on `bars` (bool
        HxW: the speaker is never in a letterbox bar)."""
        import numpy as np

        ka, kb = self.keys_of(i)
        m = self._cleaned_key(ka)
        if kb != ka:
            m = np.maximum(m, self._cleaned_key(kb))
        else:
            m = m.copy()
        if bars is not None:
            m[np.asarray(bars, dtype=bool)] = 0.0
        return m

    def coarse(self, i: int, width: int, height: int) -> Any:
        """Frame i's union straight from MODNet's own output at width x
        height (uint8 0..255, not cleaned) — cheap, for the camera's mask of
        where the speaker is."""
        import cv2
        import numpy as np

        ka, kb = self.keys_of(i)
        with self._lock:
            missing = {k for k in (ka, kb) if k not in self.raw}
        if missing:
            self._make(missing)
            self.save()
        with self._lock:
            a, b = self.raw[ka], self.raw[kb]
        u = a if kb == ka else np.maximum(a, b)
        return cv2.resize(u, (int(width), int(height)), interpolation=cv2.INTER_LINEAR)

    def has_person(self) -> bool:
        """Whether anyone is in the clip at all (PERSON_MIN, over the
        keyframes made — call it once filled). A rough test: read only for
        an uploaded intro or outro video (house.person_of)."""
        import numpy as np

        with self._lock:
            raws = [self.raw[k] for k in sorted(self.raw)]
        if not raws:
            return False
        return float(np.median([float((r > 127).mean()) for r in raws])) > PERSON_MIN

    def public(self) -> dict:
        done, total = self.progress()
        return {"every": self.every, "keyframes": total, "made": done, "frames": self.n,
                "cuts": list(self.cuts), "bars": self.bars is not None, "seconds": round(self.seconds, 1)}
