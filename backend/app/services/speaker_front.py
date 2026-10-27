"""Put the speaker back IN FRONT of the Omni pattern, frame by frame.

The owner wants the pattern as part of the background: "make the character
to the front and the pattern behind her". Omni does not do that. Shown a
target frame with the speaker already covering the pattern, and told in so
many words never to draw a line over the speaker, it still drew the lattice
straight across her abaya and hands (experiment 17). A generative edit
treats added graphics as a layer on top. So the occlusion is done here,
deterministically, after Omni:

    out = omni_frame * (1 - M) + original_frame * M

where M is the speaker's matte in the ORIGINAL frame. Omni keeps the
speaker's geometry (their silhouette in its output matches the original at
IoU >= 0.99 across experiment 17's clip), so the original pixels land
exactly where Omni's speaker was. The speaker also comes back pixel for
pixel: Omni redraws every pixel, faces included, and this undoes that too.

The same pass clears the frosted panel Omni sometimes slides in with the
pattern's entrance (veil_restore_mask). It runs only on the clip that
carries the entrance, over its first VEIL_GUARD_SEC.

Building on a finished version ("Add on top"), Omni edits that version's
clip, not the original, and the pass takes it as a third stream, the
BACKDROP: the veil is judged against it, and outside the new layers (the
HOLD) the frame is put back to it exactly — so the version stays as it was
wherever nothing new was asked for. The speaker still comes from the
original, last.

With the stray-drawing clean-up on (the recipe's guards.stray "clean"), the
pass is also handed the FOOTPRINT of what the recipe draws (its patterns'
ink, grown a little, and our icons' boxes): after the veil and the hold,
everything outside it is put back to the original — to the backdrop when
there is one — so a mark Omni drew on its own (experiment 23's line-art
head) is gone.

The recipe's ICONS are stamped here too (an icon_render.Stamper), after the
veil, the hold and the clean-up and before the speaker: the matte wins, so
a hand passes in front of an icon and the face is never covered. An icon
the planner put IN FRONT of her (only where it stays clear of the face) is
stamped after the speaker instead (Stamper.over). One frame of it is
compose_frame; frame_at makes any single frame of a pass exactly as the
pass writes it (the studio's exact still), and stamp_only lays the icons
on a clip with no matte at all — only when the pass itself failed.

The matte is MODNet (portrait matting, Apache-2.0, the Xenova ONNX export,
revision-pinned) on onnxruntime. Nothing else installed does person
segmentation, mediapipe would pull a second OpenCV, and a Gemini mask per
frame would take hours. On this CPU it costs ~0.11 s a frame at a 256 px short
side with MODNET_THREADS threads and no spinning (it was 0.2-0.4 s on every
core with ORT's busy-waiting, which also starved numpy and ffmpeg beside it;
the output is bit-identical), so it runs on every MATTE_EVERY-th frame. A
frame in between takes the UNION of the two keyframe mattes around it, so a
moving hand is always "speaker" and no pattern line ever crosses it. The worst
case is a thin rim of plain background beside a fast hand, never a line on
the hand.

A clip that jumps to another shot part way (a Veo push-in cut) is passed its
cuts: the union then never takes a keyframe from the other side of one
(matte_keys), so a frame's speaker is cut by its own shot's pose only.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger("archivist.rebrand.speaker_front")

MODNET_REPO = os.getenv("MODNET_REPO", "Xenova/modnet")
MODNET_FILE = os.getenv("MODNET_FILE", "onnx/model.onnx")
MODNET_REVISION = os.getenv("MODNET_REVISION", "fa2fa546052fba4c08921230a26cc69a333fca12")
MODNET_PATH = os.getenv("MODNET_PATH", "")          # a local .onnx overrides the download
MATTE_SHORT = int(os.getenv("MATTE_SHORT", "256"))  # IoU 0.955 vs 512, 3x faster
MATTE_EVERY = int(os.getenv("MATTE_EVERY", "3"))
# The matte is grown by this fraction of the short side before feathering, so
# no half-covered pattern stroke hugs the speaker's outline.
GROW_FRAC = 0.004

# The entrance veil (see veil_restore_mask). Omni sometimes opens the pattern's
# entrance with a frosted white panel sliding in over the room (experiment 19,
# frames 11-30) — its own idea of a "wipe". Only the entrance is checked.
VEIL_GUARD_SEC = 3.0
# A veil pixel is the original pulled evenly towards white: the fraction it
# moved (per channel, relative to how far it could move) is in this range...
VEIL_MIN_A, VEIL_MAX_A = 0.08, 0.85
# ...and the channels agree to within this (a coloured line or icon does not).
VEIL_SPREAD = 0.10
# Inside a veil, any change more neutral than this is undone; anything more
# coloured (the pattern's lines, icons) is kept.
VEIL_KEEP_SPREAD = 0.25
# Only a connected veil at least this share of the frame counts. Omni's
# re-render noise is scattered specks: on experiment 20 (a clean entrance)
# 0.08/2% flagged nothing in any of its 238 frames, while 0.05 already did.
VEIL_MIN_AREA = 0.02

_session: Any = None
_session_lock = threading.Lock()


def _model_path() -> str:
    if MODNET_PATH:
        return MODNET_PATH
    from huggingface_hub import hf_hub_download

    return hf_hub_download(MODNET_REPO, MODNET_FILE, revision=MODNET_REVISION)


def modnet_threads() -> int:
    """The threads one MODNet call runs on: MODNET_THREADS, else up to 8.
    Past 8 this CPU gains nothing (4, 6, 8 and 12 threads give the same bits;
    8 is the fastest), and every thread taken is one numpy and ffmpeg lose."""
    try:
        wanted = int(os.getenv("MODNET_THREADS") or 0)
    except ValueError:
        wanted = 0
    return wanted if wanted > 0 else max(1, min(8, os.cpu_count() or 4))


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort

            so = ort.SessionOptions()
            so.intra_op_num_threads = modnet_threads()
            # No busy-waiting between calls: a spinning pool held every core
            # after each matte (a warp beside it went from 2 ms to 15 ms) and
            # made MODNet itself twice as slow.
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            _session = ort.InferenceSession(_model_path(), so, providers=["CPUExecutionProvider"])
        return _session


def raw_matte(bgr) -> Any:
    """MODNet's own output for a BGR uint8 frame: float32 alpha at the
    model's input size (MATTE_SHORT on the short side, a multiple of 32 —
    448x256 for a 720x1280 frame), not yet scaled to the frame."""
    import cv2
    import numpy as np

    sess = _get_session()
    h, w = bgr.shape[:2]
    s = MATTE_SHORT / min(h, w)
    nh, nw = max(32, int(round(h * s / 32)) * 32), max(32, int(round(w * s / 32)) * 32)
    rgb = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)[:, :, ::-1]
    x = ((rgb.astype(np.float32) / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None]
    return sess.run(None, {sess.get_inputs()[0].name: np.ascontiguousarray(x)})[0][0, 0]


def matte(bgr) -> Any:
    """Raw MODNet alpha (float32 HxW, 0..1) for a BGR uint8 frame."""
    import cv2
    import numpy as np

    h, w = bgr.shape[:2]
    return np.clip(cv2.resize(raw_matte(bgr), (w, h), interpolation=cv2.INTER_LINEAR), 0.0, 1.0)


def clean(alpha) -> Any:
    """A raw matte made safe to composite with: holes inside the silhouette
    filled (MODNet reads the darkest folds of a black abaya as background),
    grown a few pixels, and feathered."""
    import cv2
    import numpy as np

    h, w = alpha.shape
    # Background is whatever the frame's border can reach through non-person
    # pixels; any other non-person pixel is a hole in the person. A one-pixel
    # background frame around the mask lets one flood from a corner reach
    # every border-touching background region.
    bg = np.pad((alpha <= 0.5).astype(np.uint8), 1, constant_values=1)
    cv2.floodFill(bg, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 2)
    holes = (bg[1:-1, 1:-1] == 1).astype(np.float32)
    a = np.maximum(alpha, holes)
    grow = max(1, int(round(min(h, w) * GROW_FRAC)))
    a = cv2.dilate(a, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1)))
    return np.clip(cv2.GaussianBlur(a, (0, 0), sigmaX=1.2), 0.0, 1.0)


def person_matte(bgr) -> Any:
    """The cleaned speaker matte for one BGR frame."""
    return clean(matte(bgr))


def veil_restore_mask(edited, original) -> Optional[Any]:
    """Where `edited` shows a translucent white/grey veil over `original`
    (float32 HxW, 0..1: how much of the original to put back), or None when
    there is none.

    A veil pixel is the original pulled evenly towards white, so its three
    channels moved the same fraction of the way. A pattern line or an icon
    is a new colour, and its channels disagree. The veil region is the large
    connected blobs of veil pixels, grown a little to take the panel's own
    edge and shadow. Inside it every NEUTRAL change is undone and every
    coloured one kept, so pattern lines revealed under a sliding panel
    survive it."""
    import cv2
    import numpy as np

    e = edited.astype(np.float32)
    o = original.astype(np.float32)
    head = 255.0 - o
    valid = head >= 25.0                     # a channel near white cannot show a veil
    a = (e - o) / np.maximum(head, 1.0)
    count = valid.sum(axis=2)
    mean = np.where(count > 0, (a * valid).sum(axis=2) / np.maximum(count, 1), 0.0)
    spread = np.where(
        count > 0,
        np.where(valid, a, -np.inf).max(axis=2) - np.where(valid, a, np.inf).min(axis=2),
        1.0,
    )
    veil = ((mean > VEIL_MIN_A) & (mean < VEIL_MAX_A) & (spread < VEIL_SPREAD)).astype(np.uint8)
    closed = cv2.morphologyEx(veil, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    h, w = veil.shape
    big = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= VEIL_MIN_AREA * h * w]
    if not big:
        return None
    region = cv2.dilate(np.isin(labels, big).astype(np.uint8),
                        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))).astype(bool)
    # Soften only the region's outer edge; the keep-the-colour test stays per
    # pixel, so a thin coloured stroke inside the veil is never faded by it.
    soft = cv2.GaussianBlur(region.astype(np.float32), (0, 0), sigmaX=1.5)
    return soft * (spread < VEIL_KEEP_SPREAD).astype(np.float32)


def _probe_fps(path: Path) -> float:
    import cv2

    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    finally:
        cap.release()
    return fps if fps > 1 else 30.0


def _fit_keep(keep, width: int, height: int):
    """A veil keep mask (float HxW, 0..1, any size) at the clip's size."""
    import cv2
    import numpy as np

    k = np.clip(np.asarray(keep, dtype=np.float32), 0.0, 1.0)
    if k.ndim != 2:
        raise ValueError(f"not a keep mask: shape {k.shape}")
    if k.shape != (height, width):
        k = cv2.resize(k, (width, height), interpolation=cv2.INTER_LINEAR)
    return k


def _blend(fe, fo, m):
    """fe with fo laid over it by the matte m (float HxW): fe * (1 - m) + fo * m."""
    import numpy as np

    if m is None or fo is None:
        return fe
    mm = m[:, :, None]
    return np.clip(np.rint(fe.astype(np.float32) * (1.0 - mm) + fo.astype(np.float32) * mm),
                   0, 255).astype(np.uint8)


def speaker_matte(fo, *, clear_bars: bool = False) -> Any:
    """The speaker's matte for one ORIGINAL frame as the pass uses it: the
    cleaned MODNet matte, ignored on the letterbox bars when asked."""
    import numpy as np

    from .rebrand_pattern_ref import bar_mask

    m = person_matte(fo)
    if clear_bars:
        bars = bar_mask(fo)
        if bars is not None:
            m = np.where(bars, 0.0, m).astype(np.float32)
    return m


def matte_keys(i: int, n: int, every: int = MATTE_EVERY, cuts: Sequence[int] = ()) -> tuple[int, int]:
    """The two keyframes whose mattes' union is frame i's matte in a pass of
    n frames (restore_speaker): the keyframe at or before it and the next
    one — or, after the last keyframe, the clip's own last frame (the tail);
    the same index twice when that frame IS the keyframe.

    `cuts` (the first frame of each new shot) keep both inside frame i's own
    shot: the one before it is never earlier than the shot's first frame,
    the one after it never later than the shot's last — so a union never
    mixes two shots' poses. With no cuts it is exactly the rule above (the
    whole clip is one shot)."""
    every = max(1, int(every))
    i = int(i)
    k = (i // every) * every
    if not cuts:
        if k + every <= n - 1:
            return k, k + every
        last = n - 1
        return (k, last) if last > k else (k, k)
    start, end = 0, int(n)
    for c in sorted({int(c) for c in cuts if 0 < int(c) < n}):
        if c <= i:
            start = c
        else:
            end = c
            break
    return max(k, start), max(i, min(k + every, end - 1))


def entrance_veil(fe, under, let_through: Any = None) -> Optional[Any]:
    """How much of `under` to put back over `fe` for the entrance veil
    (veil_restore_mask), the patterns' own ink let through — or None."""
    v = veil_restore_mask(fe, under)
    if v is not None and let_through is not None:
        v = v * let_through
    return v


def _clear_boxes(m, fo, boxes) -> Any:
    """The matte with nothing of the speaker left where a letterbox bar and
    an icon's pixels meet (the speaker is never in a bar: MODNet's specks
    there would punch the bar's black through the icon)."""
    import numpy as np

    from .rebrand_pattern_ref import bar_mask

    if m is None or not boxes:
        return m
    bars = bar_mask(fo)
    if bars is None:
        return m
    zone = np.zeros(bars.shape, dtype=bool)
    for x0, y0, x1, y1 in boxes:
        zone[max(0, int(y0)):max(0, int(y1)), max(0, int(x0)):max(0, int(x1))] = True
    zone &= bars
    if not zone.any():
        return m
    return np.where(zone, 0.0, m).astype(np.float32)


def compose_frame(fe, fo, m, *, stamps: Any = None, t: float = 0.0, veil: Any = None,
                  backdrop: Any = None, hold: Any = None, footprint: Any = None) -> Any:
    """One output frame of restore_speaker (BGR uint8), in its order:

      veil      (float HxW, entrance_veil) fe pulled back towards the backdrop,
                else the original, where Omni slid a frosted panel in;
      hold      (float HxW or HxWx1, needs `backdrop`) fe * (1 - K) + backdrop * K;
      footprint (float HxW, 1 = what the recipe draws) outside it fe is put
                back to the backdrop, else the original: the clean-up;
      stamps    (icon_render.Stamper) the icons on screen at chunk-local t,
                drawn BEHIND the speaker — on a frame they draw, the matte is
                emptied where a letterbox bar and their pixels meet;
      m         the speaker from `fo` laid back over it all;
      stamps    then the icons IN FRONT of her (Stamper.over), if any.

    With none of veil / hold / footprint / stamps it is exactly the frame
    the pass has always written."""
    import numpy as np

    put_back = None
    if footprint is not None:
        put_back = 1.0 - np.clip(np.asarray(footprint, dtype=np.float32), 0.0, 1.0)
    fe = _held(fe, fo, veil=veil, backdrop=backdrop, hold=hold, put_back=put_back)
    return _finish(fe, fo, m, stamps, t)[0]


def _held(fe, fo, *, veil: Any = None, backdrop: Any = None, hold: Any = None,
          put_back: Any = None) -> Any:
    """compose_frame's first three steps: the veil undone, the backdrop held,
    and — `put_back` (float HxW, 1 - the footprint) — everything outside
    what the recipe draws put back to the backdrop, else the original."""
    import numpy as np

    if veil is not None:
        fe = _blend(fe, fo if backdrop is None else backdrop, veil)
    if hold is not None:
        if backdrop is None:
            raise ValueError("a hold keeps the backdrop: it needs one")
        keep = hold if np.ndim(hold) == 3 else np.asarray(hold, dtype=np.float32)[:, :, None]
        fe = np.clip(np.rint(fe.astype(np.float32) * (1.0 - keep)
                             + backdrop.astype(np.float32) * keep), 0, 255).astype(np.uint8)
    if put_back is not None:
        fe = _blend(fe, fo if backdrop is None else backdrop, put_back)
    return fe


def _over(stamps: Any, frame, t: float):
    """The icons IN FRONT of the speaker laid over a finished frame
    (Stamper.over) — `frame` itself when there are none (or the stamps
    know no front layer)."""
    over = getattr(stamps, "over", None)
    return frame if over is None else over(frame, t)


def _finish(fe, fo, m, stamps: Any, t: float) -> tuple[Any, bool]:
    """compose_frame's last three: the icons under the speaker, the
    speaker, then the icons in front of her — (the frame, whether an icon
    was drawn on it). With no icon in front it is exactly the frame the
    pass has always written."""
    if stamps is not None:
        drawn = stamps.under(fe, t)
        if drawn is not fe:
            return _over(stamps, _blend(drawn, fo, _clear_boxes(m, fo, stamps.boxes(t, drawn=True))), t), True
        out = _blend(fe, fo, m)
        front = _over(stamps, out, t)
        return front, front is not out
    return _blend(fe, fo, m), False


class _Encoder:
    """One ffmpeg encode of raw BGR frames at the clip's size and rate, with
    `audio_from`'s audio copied under them."""

    def __init__(self, out: Path, width: int, height: int, fps: float, audio_from: Path, *,
                 shortest: bool = True):
        from .rebrand_media import ffmpeg_exe

        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg_exe(), "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps:.6f}",
            "-i", "-", "-i", str(audio_from),
            "-map", "0:v", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy", *(["-shortest"] if shortest else []), "-movflags", "+faststart", str(out),
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame) -> None:
        self.proc.stdin.write(frame.tobytes())

    def close(self, what: str) -> None:
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode(errors="replace")
        if self.proc.wait(timeout=600) != 0:
            raise RuntimeError(f"ffmpeg failed {what}: {err[-400:]}")

    def kill(self) -> None:
        self.proc.kill()


def restore_speaker(
    edited: Path, original: Path, out: Path, *, every: int = MATTE_EVERY,
    clear_veil_sec: float = 0.0, clear_bars: bool = False, veil_keep: Any = None,
    backdrop: Optional[Path] = None, hold: Any = None, stamps: Any = None,
    clean: Optional[Path] = None, footprint: Any = None, cuts: Sequence[int] = (),
) -> Path:
    """`edited` (Omni's clip, already fitted to `original`'s length, size and
    audio) with the speaker from `original` composited back on top. Keeps
    `edited`'s audio. Raises on failure; the caller keeps Omni's clip then.

    `clear_veil_sec` > 0 also takes any entrance veil (veil_restore_mask)
    out of the first that-many seconds — the clip that carries the pattern's
    entrance passes VEIL_GUARD_SEC, every other clip 0.

    `clear_bars` ignores the matte on the original's letterbox bars
    (rebrand_pattern_ref.bar_mask): nothing there is the speaker, and
    MODNet's specks there would paste the plain black bar back over the
    pattern Omni drew in it — the same holes the target frame no longer has
    (PatternRef.clears_bars says which patterns).

    `veil_keep` (float HxW, 0..1 — the patterns' own ink, grown a little)
    is kept out of the veil guard: there the veil mask becomes v * (1 - keep).
    A white or light-grey pattern fading in IS an even pull towards white, and
    with several patterns (or a light colour picked for one) the guard would
    otherwise erase it. None: the veil guard exactly as before.

    `backdrop` (a clip, read in step with the other two; its last frame held
    if it runs out) is what `edited` was made FROM when that is not
    `original` — a finished version Omni drew new layers on ("Add on top").
    The veil is then measured against it and undone towards it, so the
    version's own light graphics are not taken for a veil. `hold` (float HxW,
    0..1, needs `backdrop`) keeps it: fe = fe * (1 - hold) + backdrop * hold,
    so wherever the new layers are not, the frame IS the version's, whatever
    Omni's second pass did there. The speaker still comes from `original`,
    last.

    `footprint` (float HxW, 0..1 — what the recipe draws: its patterns' ink
    grown a little and feathered, our icons' boxes) is the stray-drawing
    clean-up: after the veil and the hold, fe = fe * F + under * (1 - F),
    `under` being the backdrop when there is one, else the original — so
    nothing Omni drew outside what it was asked for survives. None: no
    clean-up.

    `stamps` (an icon_render.Stamper for this clip) lays the icons in after
    the veil, the hold and the clean-up, BEHIND the speaker (compose_frame);
    `clean`, a second output path, gets the very same frames without them —
    the version as it is with none of our icons, what a later pass builds on.

    `cuts` (frames of `original` where a new shot starts: media.cuts_of) keep
    each frame's matte union inside its own shot (matte_keys): the last frame
    before a cut closes its window with the shot's own last matte, and the
    cut starts a new one.

    The order per frame: veil, hold, footprint, icons, speaker. None of
    backdrop / hold / footprint / stamps / clean / cuts: exactly the pass as
    it always was, byte for byte."""
    import cv2
    import numpy as np

    if hold is not None and backdrop is None:
        raise ValueError("a hold keeps the backdrop: it needs one")
    # The frames that end a shot (the frame before each cut).
    shot_last = {int(c) - 1 for c in cuts or () if int(c) > 0}

    def speaker(fo):
        return speaker_matte(fo, clear_bars=clear_bars)

    t0 = time.time()
    a = cv2.VideoCapture(str(original))
    b = cv2.VideoCapture(str(edited))
    if not (a.isOpened() and b.isOpened()):
        raise RuntimeError(f"cannot open {original.name} / {edited.name}")
    width = int(b.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(b.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = _probe_fps(edited)
    # Fitted before ffmpeg starts, so a bad mask cannot leave it running.
    let_through = None
    if veil_keep is not None and clear_veil_sec > 0:
        let_through = 1.0 - _fit_keep(veil_keep, width, height)
    put_back = None
    if footprint is not None:
        put_back = 1.0 - _fit_keep(footprint, width, height)
    back = None
    keep_back = None
    if backdrop is not None:
        if hold is not None:
            keep_back = _fit_keep(hold, width, height)[:, :, None]
        back = cv2.VideoCapture(str(backdrop))
        if not back.isOpened():
            a.release()
            b.release()
            raise RuntimeError(f"cannot open {Path(backdrop).name}")
    out = Path(out)
    encoders = [_Encoder(out, width, height, fps, edited)]
    frames = 0
    mattes = 0
    veiled = 0
    stamped = 0
    last_orig = None
    last_back = None
    veil_frames = int(round(clear_veil_sec * fps)) if clear_veil_sec > 0 else 0

    def emit(fe, fo, fb, m) -> None:
        nonlocal frames, veiled, stamped
        v = None
        if frames < veil_frames:
            v = entrance_veil(fe, fo if fb is None else fb, let_through)
            if v is not None:
                veiled += 1
        fe = _held(fe, fo, veil=v, backdrop=fb, hold=keep_back, put_back=put_back)
        frame, drawn = _finish(fe, fo, m, stamps, frames / fps)
        encoders[0].write(frame)
        if len(encoders) > 1:
            encoders[1].write(_blend(fe, fo, m) if drawn else frame)
        stamped += int(drawn)
        frames += 1

    try:
        if clean is not None:
            encoders.append(_Encoder(Path(clean), width, height, fps, edited))
        # buf holds the frames from one keyframe up to (and then including)
        # the next. Every frame before the next keyframe is blended with the
        # UNION of the two keyframe mattes (matte_keys), then the next
        # keyframe starts the following window. A shot's last frame (the one
        # before a cut) closes the window instead, its own frame with it,
        # and the cut's frame starts a new one with its own matte.
        buf: list = []
        m_prev = None
        i = -1
        while True:
            ok_e, fe = b.read()
            if not ok_e:
                break
            i += 1
            ok_o, fo = a.read()
            if ok_o:
                if fo.shape[:2] != (height, width):
                    fo = cv2.resize(fo, (width, height), interpolation=cv2.INTER_AREA)
                last_orig = fo
            else:
                fo = last_orig              # the original ran out: hold its last frame
            if fo is None:
                raise RuntimeError(f"no frames in {original.name}")
            fb = None
            if back is not None:
                ok_b, fb = back.read()
                if ok_b:
                    if fb.shape[:2] != (height, width):
                        fb = cv2.resize(fb, (width, height), interpolation=cv2.INTER_AREA)
                    last_back = fb
                else:
                    fb = last_back          # the backdrop ran out: hold its last frame
                if fb is None:
                    raise RuntimeError(f"no frames in {Path(backdrop).name}")
            buf.append((fe, fo, fb))
            if len(buf) == 1 and m_prev is None:
                m_prev = speaker(fo)
                mattes += 1
            if i in shot_last:
                # The shot ends here: its window closes on this frame's own
                # matte — and this frame goes with it, alone when it is a
                # keyframe itself (matte_keys).
                m_last = m_prev
                if len(buf) > 1:
                    m_last = speaker(fo)
                    mattes += 1
                    m = np.maximum(m_prev, m_last)
                    for fe_i, fo_i, fb_i in buf[:-1]:
                        emit(fe_i, fo_i, fb_i, m)
                    if i % every:
                        m_last = m
                emit(fe, fo, fb, m_last)
                buf = []
                m_prev = None
            elif len(buf) > 1 and (len(buf) == every + 1 if not shot_last else i % every == 0):
                m_next = speaker(buf[-1][1])
                mattes += 1
                m = np.maximum(m_prev, m_next)
                for fe_i, fo_i, fb_i in buf[:-1]:
                    emit(fe_i, fo_i, fb_i, m)
                buf = buf[-1:]
                m_prev = m_next
        if buf:
            # The tail after the last keyframe: its own last frame closes it.
            m = m_prev
            if len(buf) > 1:
                m = np.maximum(m_prev, speaker(buf[-1][1]))
                mattes += 1
            for fe_i, fo_i, fb_i in buf:
                emit(fe_i, fo_i, fb_i, m)
        for enc in encoders:
            enc.close("restoring the speaker")
    except Exception:
        for enc in encoders:
            enc.kill()
        raise
    finally:
        a.release()
        b.release()
        if back is not None:
            back.release()
    if frames == 0:
        raise RuntimeError(f"no frames in {edited.name}")
    log.info("[speaker_front] %s: %d frames, %d mattes, %d veiled frames cleared, %d with icons, %.1fs -> %s%s",
             edited.name, frames, mattes, veiled, stamped, time.time() - t0, out.name,
             "" if put_back is None else " (stray drawings cleaned up)")
    return out


def _read_at(cap, wanted: set[int]) -> tuple[dict[int, Any], Any, int]:
    """Frames `wanted` of an open capture, read in order from its start (so
    each is exact), with its last frame read and how many were read."""
    got: dict[int, Any] = {}
    last = None
    n = 0
    top = max(wanted) if wanted else -1
    while n <= top:
        ok, f = cap.read()
        if not ok:
            break
        if n in wanted:
            got[n] = f
        last = f
        n += 1
    return got, last, n


def _count(path: Path) -> int:
    """How many frames a clip decodes to (grabbed, not decoded to pixels)."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    n = 0
    try:
        while cap.grab():
            n += 1
    finally:
        cap.release()
    return n


def frame_at(edited: Path, original: Path, at: float, *, every: int = MATTE_EVERY,
             clear_veil_sec: float = 0.0, clear_bars: bool = False, veil_keep: Any = None,
             backdrop: Optional[Path] = None, hold: Any = None, stamps: Any = None,
             layer: Any = None, footprint: Any = None,
             cuts: Sequence[int] = ()) -> tuple[Any, int, float]:
    """The frame `at` seconds into the pass restore_speaker(edited, original,
    …) writes, exactly: the same compose_frame, the matte of the same two
    keyframes (matte_keys — the tail after the last one included, and never
    across one of `cuts`), every stream decoded in order from its start (a
    seek can land a frame off).
    Returns (the frame, BGR uint8; its index; the clip's fps).

    `layer` (RGBA uint8 HxWx4, straight alpha, RGB order) is laid over the
    edited frame first — the studio's stand-in for what Omni will draw, on a
    scene no version has been made of yet."""
    import cv2
    import numpy as np

    if hold is not None and backdrop is None:
        raise ValueError("a hold keeps the backdrop: it needs one")
    fps = _probe_fps(edited)
    n = _count(edited)
    if n == 0:
        raise RuntimeError(f"no frames in {Path(edited).name}")
    i = min(max(0, int(round(float(at) * fps))), n - 1)
    ka, kb = matte_keys(i, n, every, cuts)

    def read(path: Path, wanted: set[int]) -> tuple[dict, Any, int, tuple[int, int]]:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f"cannot open {Path(path).name}")
        try:
            size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            got, last, _n = _read_at(cap, wanted)
        finally:
            cap.release()
        return got, last, _n, size

    got_e, _last_e, _ne, (width, height) = read(edited, {i})
    if i not in got_e:
        raise RuntimeError(f"frame {i} of {Path(edited).name} did not decode")

    def fit(f):
        return f if f.shape[:2] == (height, width) else cv2.resize(f, (width, height),
                                                                    interpolation=cv2.INTER_AREA)

    got_o, last_o, _no, _s = read(original, {i, ka, kb})
    if last_o is None:
        raise RuntimeError(f"no frames in {Path(original).name}")
    orig = {k: fit(got_o[k] if k in got_o else last_o) for k in {i, ka, kb}}   # held when it ran out
    fb = None
    if backdrop is not None:
        got_b, last_b, _nb, _s = read(backdrop, {i})
        if last_b is None:
            raise RuntimeError(f"no frames in {Path(backdrop).name}")
        fb = fit(got_b.get(i, last_b))
    fe = got_e[i]
    if layer is not None:
        from .rebrand_harness.icon_render import composite

        fe = np.array(fe, dtype=np.uint8, copy=True)
        composite(fe, np.asarray(layer, dtype=np.uint8), 0, 0)
    fo = orig[i]
    v = None
    if clear_veil_sec > 0 and i < int(round(clear_veil_sec * fps)):
        let_through = 1.0 - _fit_keep(veil_keep, width, height) if veil_keep is not None else None
        v = entrance_veil(fe, fo if fb is None else fb, let_through)
    m = speaker_matte(orig[ka], clear_bars=clear_bars)
    if kb != ka:
        m = np.maximum(m, speaker_matte(orig[kb], clear_bars=clear_bars))
    keep = _fit_keep(hold, width, height)[:, :, None] if hold is not None else None
    fp = _fit_keep(footprint, width, height) if footprint is not None else None
    return compose_frame(fe, fo, m, stamps=stamps, t=i / fps, veil=v, backdrop=fb, hold=keep,
                         footprint=fp), i, fps


def stamp_only(clip: Path, out: Path, stamps: Any) -> Path:
    """`clip` with the icons laid over it and nothing else — no matte, so
    they would cover the speaker too. Used only when restore_speaker itself
    failed on a clip; the runner then hands it only the icons the speaker
    never reaches (max_overlap 3 % at most) and those in front of her.
    Keeps the clip's audio and its frame count."""
    import cv2

    cap = cv2.VideoCapture(str(clip))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {Path(clip).name}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = _probe_fps(clip)
    enc = _Encoder(Path(out), width, height, fps, clip, shortest=False)
    frames = 0
    try:
        while True:
            ok, f = cap.read()
            if not ok:
                break
            if stamps is not None:
                f = _over(stamps, stamps.under(f, frames / fps), frames / fps)
            enc.write(f)
            frames += 1
        enc.close("stamping the icons")
    except Exception:
        enc.kill()
        raise
    finally:
        cap.release()
    if frames == 0:
        raise RuntimeError(f"no frames in {Path(clip).name}")
    return Path(out)
