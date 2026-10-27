"""Rebrand — the 42 brand frame (the "brand" arm: nothing is regenerated).

Omni and Veo rebrand a reel by generating new pixels, and the speaker is
among them. This arm makes a finished TH v2 reel look like 42 Abu Dhabi's own
reels (violet slabs, white bead strings, a grid, the logo, slab wipes on the
cuts) while every footage pixel outside the drawn graphics stays the source
pixel — only the final encode touches it.

  1. MEASURE    each raw source: its exact length in 30 fps frames and its
                content box (Veo TH clips are 720x1280 with a 1:1 or 3:2
                picture band between black bars — the bars are free brand
                space, the band is never drawn on).
  2. GRAPHICS   Remotion (`brand-overlay-42`) renders ONLY the graphics, on a
                transparent canvas, as a PNG sequence. The footage never goes
                through Remotion/Chrome: that path measured ~4 dB worse than an
                ffmpeg re-encode and once served a frame 3 frames early.
  3. COMPOSITE  ONE ffmpeg run normalises every source, hard-cuts them
                together and lays the PNG sequence on top. The wipes the
                graphics draw over each cut are what hide it.

A "glitch" cut is the one exception to "the footage is untouched": over its
12 frames (F-6..F+5) the footage itself glitches — slices of the outgoing and
incoming shots torn sideways, handed over slice by slice around the cut, the
colour channels split. Those frames are built here in numpy (`glitch_plate`)
and laid UNDER the graphics PNGs of the same frames, so the one ffmpeg pass
stays the only encode. Every other frame's footage is still bit-exact.

Pure functions + one async orchestrator. No DB here — the arm wrapper in
rebrand_pipeline does the persistence. Every ffmpeg helper is SYNC and is
called through `asyncio.to_thread`, like rebrand_media.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

import numpy as np

from . import rebrand_media as media

log = logging.getLogger("archivist.rebrand.brand")

FPS = 30
AUDIO_RATE = 48000
TEMPLATE = "brand-overlay-42"

STYLE_OPTIONS = ("auto", "cover", "handoff", "short", "glitch")
CARD_KINDS = ("intro", "outro")

# Letterbox detection, on the luma of a few samples AFTER the composite's own
# scale/pad. A bar row/column must be black on average (mean luma over every
# sample <= BAR_LUMA_MAX; limited range, so black is 16) AND never carry a
# real feature (no pixel in any sample above BAR_PEAK_MAX), and it must touch
# the frame edge through other bar rows. Anything thinner than BAR_MIN_FRAC is
# noise, not a bar.
#
# Why not "every pixel <= 18": measured on the Veo TH clips here, codec noise
# in the bars reaches 19-26 once lanczos has upscaled it to 1080x1920 (up to a
# fifth of a bar row's pixels sit above 18), so a strict max never finds a
# single bar. Measured means: bar rows 16.9-17.3; the lanczos ringing rows
# right next to the picture 18.1-19.0 (still bar — excluding them left a 3 px
# black keyline between the plate and the picture); the first anti-aliased
# picture row 22+; a dark-but-not-black picture edge (RGB ~10) about 25.
BAR_LUMA_MAX = 20
BAR_PEAK_MAX = 40
BAR_MIN_FRAC = 0.01
# The picture's own first row next to a bar is often a half-black blend (the
# Veo band's anti-aliased edge, then lanczos): measured mean luma 22-30 against
# 60-100 one row further in (7-27% of it above black), and 37.6 against 58.6
# (51%) under a TH intro card. It is not a bar by the rules above, so it stayed
# inside the box and drew a near-black keyline between the charcoal plate
# (#303030 = luma 57) and the picture. Such an edge row goes to the bar: when
# its brightness above black is under EDGE_BLEND_MAX of the next row's AND it
# is darker than the plate. One row per edge at most — it costs at most half a
# picture row, and a genuinely dark picture edge is never eaten further.
EDGE_BLEND_MAX = 0.6
PLATE_LUMA = 57
DETECT_SAMPLES = 6
# "bars" layout: both bars of a portrait canvas at least this tall.
BARS_LAYOUT_MIN = 0.10

# Frames a seam style draws on either side of the cut at F:
#   cover    F-8 .. F+10  → 8 of the outgoing segment, 11 of the incoming
#   short    F-5 .. F+4   → 5 and 5
#   handoff  exit over F-18 .. F-1; the incoming entrance is ordinary layout
#            animation, not a wipe, so it claims nothing of the incoming side
#   glitch   F-6 .. F+5 (the footage glitches too: 6 frames each side)
SEAM_NEEDS: dict[str, tuple[int, int]] = {
    "cover": (8, 11),
    "short": (5, 5),
    "handoff": (18, 0),
    "glitch": (6, 6),
}

# The glitch cut, per frame from F-6 to F+5: its strength (the composition's
# brand42/tokens.ts GLITCH.seamBefore / seamAfter — keep the two in step).
GLITCH_BEFORE = (0.35, 0.5, 0.65, 0.8, 0.95, 1.0)
GLITCH_AFTER = (1.0, 0.85, 0.65, 0.45, 0.28, 0.12)
GLITCH_SLICES = 16
# Largest ordinary sideways jump and the channel split at full strength, px
# at 720 wide (the composition's GLITCH.shift * 1.6 / GLITCH.split * 1.5).
GLITCH_SHIFT = 42.0
GLITCH_SPLIT = 7.5
GLITCH_STEP = 2
VIOLET_RGB = (0x7F, 0x2B, 0xF2)

# Tags the untagged Veo/Omni footage as what it is. Without it ffmpeg guessed
# a range conversion (luma 155 → 150) and the untouched area fell to 38 dB.
_TAG = "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"
# The graphics are sRGB RGBA; convert them with the same matrix/range as the
# footage so the violet lands on the token colour.
_RGBA_TO_YUV = "scale=out_color_matrix=bt709:out_range=tv,format=yuva420p"
# The one encode every footage pixel goes through. Before it the untouched
# area is bit-exact (0 differing pixels, measured on 5 reels); CRF 16 then
# cost detailed, upscaled Veo footage 45-48 dB against the same pipeline with
# a transparent overlay, below the 48 dB the frame promises. CRF 12 measured
# 48.4 dB at ~6 Mbps for 1080x1920 (+~3 s per 16 s reel) — visually lossless,
# and far under anything Instagram re-encodes to.
CRF = 12

Box = dict[str, float]          # {"x","y","w","h"} as FRACTIONS of the frame
GraphicsRenderer = Callable[[dict, Path], Union[dict, Awaitable[dict]]]
StageCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class BrandSource:
    kind: str                   # intro | scene | outro
    index: int                  # scene index (0 for bookends)
    path: Path                  # the RAW source (never the 720p normalised copy)
    is_still: bool
    still_seconds: float = 0.0  # how long a still card is held


@dataclass
class BrandResult:
    final: Path
    props: dict
    segments: list[dict] = field(default_factory=list)
    seams: list[dict] = field(default_factory=list)
    graphics_seconds: float = 0.0
    composite_seconds: float = 0.0
    # The composition's report on the heading / sub-line (shown, shrunk,
    # partly or wholly left out, and why); None when there was no text or the
    # renderer did not say.
    text_fit: Optional[dict] = None
    # The topic icons the frame showed (props.icons).
    icons: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Measuring the sources
# ---------------------------------------------------------------------------

def segment_frames(src: BrandSource) -> int:
    """Length of one segment in 30 fps frames.

    Videos use the VIDEO stream's own length (decoded, at the clip's own frame
    rate — a raw Veo clip is 24 fps), not the container's, which reports the
    longest stream and is routinely a few ms of audio longer."""
    if src.is_still:
        return int(round(float(src.still_seconds) * FPS))
    return int(round(media.video_seconds(src.path) * FPS))


def fit_chain(width: int, height: int) -> str:
    """Letterbox (never crop) onto the canvas. Shared by detection and the
    composite so the box that is measured is the box that is drawn around."""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )


def source_chain(width: int, height: int, frames: int) -> str:
    """The per-source video chain of the composite. The trim → tpad → trim
    sandwich yields EXACTLY `frames` frames: short sources are held on their
    last frame, long ones are cut."""
    return (
        f"{_TAG},fps={FPS},{fit_chain(width, height)},"
        f"trim=end_frame={frames},tpad=stop_mode=clone:stop={frames},"
        f"trim=end_frame={frames},setpts=PTS-STARTPTS,format=yuv420p"
    )


def _edge_run(is_bar: np.ndarray) -> int:
    """How many leading entries are bars (contiguous from the edge)."""
    if not len(is_bar) or not bool(is_bar[0]):
        return 0
    off = np.flatnonzero(~is_bar)
    return int(off[0]) if len(off) else int(len(is_bar))


def _luma_samples(src: BrandSource, width: int, height: int, frames: Optional[int]) -> np.ndarray:
    """(n, H, W) luma planes of a few frames, taken AFTER the composite's own
    fps + scale + pad chain, at the full output size."""
    if src.is_still:
        cmd = [media.ffmpeg_exe(), "-v", "error", "-i", str(src.path),
               "-vf", f"{_TAG},{fit_chain(width, height)},format=yuv420p,extractplanes=y",
               "-frames:v", "1"]
    else:
        n = frames if frames is not None else segment_frames(src)
        n = max(1, int(n))
        picks = sorted({min(n - 1, int((i + 0.5) * n / DETECT_SAMPLES)) for i in range(DETECT_SAMPLES)})
        sel = "+".join(f"eq(n\\,{p})" for p in picks)
        cmd = [media.ffmpeg_exe(), "-v", "error", "-i", str(src.path), "-map", "0:v:0",
               "-vf", (f"{_TAG},fps={FPS},select='{sel}',{fit_chain(width, height)},"
                       "format=yuv420p,extractplanes=y"),
               "-fps_mode", "passthrough"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=media.ENCODE_TIMEOUT_SEC)
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
        raise media.MediaError(f"ffmpeg content-box sample of {src.path.name} failed: {tail}")
    plane = width * height
    count = len(proc.stdout) // plane
    if count == 0:
        raise media.MediaError(f"no frames sampled from {src.path.name}")
    return np.frombuffer(proc.stdout[: count * plane], np.uint8).reshape(count, height, width)


def _half_black(edge: float, inner: float) -> bool:
    """An edge line that is a blend of black and the picture (see
    EDGE_BLEND_MAX): dimmer than the plate, and less than half as far above
    black as the line inside it."""
    return edge < PLATE_LUMA and (edge - 16.0) < EDGE_BLEND_MAX * (inner - 16.0)


def _shed_blend_edges(
    mean: np.ndarray, top: int, bottom: int, left: int, right: int,
) -> tuple[int, int, int, int]:
    """Move each edge that borders a bar in by one line when that line is a
    half-black blend. Measured on the picture only (rows across the columns
    between the side bars, and the other way round)."""
    h, w = mean.shape
    y0, y1, x0, x1 = top, h - bottom, left, w - right
    if y1 - y0 < 4 or x1 - x0 < 4:
        return top, bottom, left, right
    rows = mean[:, x0:x1].mean(axis=1)
    cols = mean[y0:y1, :].mean(axis=0)
    if top and _half_black(rows[y0], rows[y0 + 1]):
        top += 1
    if bottom and _half_black(rows[y1 - 1], rows[y1 - 2]):
        bottom += 1
    if left and _half_black(cols[x0], cols[x0 + 1]):
        left += 1
    if right and _half_black(cols[x1 - 1], cols[x1 - 2]):
        right += 1
    return top, bottom, left, right


def detect_content_box(
    src: BrandSource, width: int, height: int, *, frames: Optional[int] = None,
) -> Box:
    """Where the picture sits on the canvas, as fractions snapped to whole
    output pixels. Outside it the composite shows black letterbox bars — the
    source's own (Veo TH clips carry a 1:1 or 3:2 band) and/or the ones the
    pad adds when the source's aspect differs from the canvas.

    A row is a bar only when it is black across ALL samples (see BAR_* above)
    and connected to the frame edge, so a dark shot, a dark wall at the top of
    the picture or a fade-in never reads as a bar. At the picture edge the
    lanczos ringing joins the bar, and a first picture row that is a
    half-black blend does too (`_shed_blend_edges`: otherwise it is a dark
    keyline between the plate and the picture), so the box can be up to one
    row inside the visible picture, never outside it by more than the
    ringing. `frames` (optional) saves re-counting the clip when the caller
    already knows its length."""
    lum = _luma_samples(src, width, height, frames)
    peak = lum.max(axis=0)
    mean = lum.mean(axis=0, dtype=np.float32)
    is_bar_row = (peak.max(axis=1) <= BAR_PEAK_MAX) & (mean.mean(axis=1) <= BAR_LUMA_MAX)
    is_bar_col = (peak.max(axis=0) <= BAR_PEAK_MAX) & (mean.mean(axis=0) <= BAR_LUMA_MAX)

    def _bar(run: int, size: int) -> int:
        return run if run >= BAR_MIN_FRAC * size else 0

    top = _bar(_edge_run(is_bar_row), height)
    bottom = _bar(_edge_run(is_bar_row[::-1]), height)
    left = _bar(_edge_run(is_bar_col), width)
    right = _bar(_edge_run(is_bar_col[::-1]), width)
    if top + bottom >= height or left + right >= width:
        # Nothing but black (an empty card): there is no picture to protect
        # and no band to frame — treat it as full-bleed.
        top = bottom = left = right = 0
    else:
        top, bottom, left, right = _shed_blend_edges(mean, top, bottom, left, right)
    return {
        "x": round(left / width, 6),
        "y": round(top / height, 6),
        "w": round((width - left - right) / width, 6),
        "h": round((height - top - bottom) / height, 6),
    }


def layout_for(box: Box, width: int, height: int) -> str:
    """"bars" when the portrait canvas has a usable bar above AND below the
    picture (the brand plate goes there); "full" otherwise (full-bleed or
    landscape footage: corner elements only)."""
    top = float(box["y"])
    bottom = 1.0 - float(box["y"]) - float(box["h"])
    eps = 1e-9
    if height > width and top + eps >= BARS_LAYOUT_MIN and bottom + eps >= BARS_LAYOUT_MIN:
        return "bars"
    return "full"


def build_timeline(sources: list[BrandSource], width: int, height: int) -> list[dict]:
    """One contract segment per source, contiguous, in reel order."""
    segments: list[dict] = []
    cursor = 0
    for src in sources:
        frames = segment_frames(src)
        if frames < 1:
            raise media.MediaError(f"{src.kind} {src.index} ({src.path.name}) has no frames")
        box = detect_content_box(src, width, height, frames=frames)
        segments.append({
            "kind": src.kind,
            "index": int(src.index),
            "from": cursor,
            "frames": frames,
            "content": box,
            "layout": layout_for(box, width, height),
        })
        cursor += frames
    return segments


def _seam_fits(style: str, out_frames: int, in_frames: int) -> bool:
    need_out, need_in = SEAM_NEEDS[style]
    return out_frames >= need_out and in_frames >= need_in


def build_seams(segments: list[dict], style_option: str = "auto") -> list[dict]:
    """One seam per join. "auto": a slab wipe ("cover") on any cut that touches
    a card, a GLITCH cut between two talking-head scenes.

    A style that the frames either side cannot hold is downgraded to the
    10-frame "short" wipe. "short" is the floor: a segment under 5 frames is
    degenerate and still gets one (the composition clamps it)."""
    opt = str(style_option or "auto").strip().lower()
    if opt not in STYLE_OPTIONS:
        opt = "auto"
    seams: list[dict] = []
    for prev, cur in zip(segments, segments[1:]):
        if opt == "auto":
            touches_card = prev["kind"] in CARD_KINDS or cur["kind"] in CARD_KINDS
            style = "cover" if touches_card else "glitch"
        else:
            style = opt
        if not _seam_fits(style, int(prev["frames"]), int(cur["frames"])):
            style = "short"
        seams.append({
            "frame": int(cur["from"]),
            "style": style,
            "from_kind": prev["kind"],
            "to_kind": cur["kind"],
        })
    return seams


def build_props(
    segments: list[dict],
    seams: list[dict],
    *,
    width: int,
    height: int,
    text: Optional[dict] = None,
    logo_on_cards: bool = False,
    icons: Optional[list[dict]] = None,
    edge_patterns: bool = True,
) -> dict:
    """The `brand-overlay-42` input props, exactly per the shared contract."""
    cursor = 0
    for seg in segments:
        if int(seg["from"]) != cursor:
            raise ValueError(f"segments are not contiguous at frame {cursor}")
        cursor += int(seg["frames"])
    t = None
    if text:
        heading = str(text.get("heading") or "").strip()
        sub = str(text.get("sub") or "").strip()
        if heading or sub:
            t = {"heading": heading, "sub": sub}
    return {
        "width": int(width),
        "height": int(height),
        "fps": FPS,
        "durationInFrames": cursor,
        "segments": [
            {
                "kind": s["kind"], "index": int(s["index"]), "from": int(s["from"]),
                "frames": int(s["frames"]), "content": dict(s["content"]), "layout": s["layout"],
            }
            for s in segments
        ],
        "seams": [
            {
                "frame": int(s["frame"]), "style": s["style"],
                "from_kind": s["from_kind"], "to_kind": s["to_kind"],
            }
            for s in seams
        ],
        "text": t,
        "logo": {"on_cards": bool(logo_on_cards)},
        "icons": [
            {"icon": str(c["icon"]), "from": int(c["from"]), "frames": int(c["frames"])}
            for c in (icons or [])
        ],
        "edge_patterns": bool(edge_patterns),
    }


# ---------------------------------------------------------------------------
# Glitch cuts: the footage frames around the cut
# ---------------------------------------------------------------------------

def _hash01(*key: object) -> float:
    """Deterministic 0..1 from a key (same value on every run and machine)."""
    h = hashlib.blake2b("|".join(map(str, key)).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2**64


def _decode_rgb(src: BrandSource, frames: int, picks: list[int], width: int, height: int) -> dict[int, np.ndarray]:
    """RGB frames `picks` (segment-local) of a source, through the composite's
    OWN per-source chain, so a plate pixel is the pixel the composite would
    have shown there."""
    picks = sorted({min(max(0, p), frames - 1) for p in picks})
    sel = "+".join(f"eq(n\\,{p})" for p in picks)
    cmd = [media.ffmpeg_exe(), "-v", "error"]
    if src.is_still:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{frames / FPS:.6f}"]
    cmd += ["-i", str(src.path), "-map", "0:v:0", "-vf",
            f"{source_chain(width, height, frames)},select='{sel}',"
            "scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=media.ENCODE_TIMEOUT_SEC)
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
        raise media.MediaError(f"glitch frames of {src.path.name}: {tail}")
    size = width * height * 3
    got = [np.frombuffer(proc.stdout[i * size:(i + 1) * size], np.uint8).reshape(height, width, 3)
           for i in range(len(proc.stdout) // size)]
    if len(got) != len(picks):
        raise media.MediaError(f"glitch frames of {src.path.name}: got {len(got)} of {len(picks)}")
    return dict(zip(picks, got))


def glitch_plate(old: np.ndarray, new: np.ndarray, k: int, amount: float, seed: str) -> np.ndarray:
    """One frame of a glitch cut, `k` frames from the cut (negative = before).

    The frame is cut into GLITCH_SLICES horizontal slices (jittered bounds).
    Each slice switches from the outgoing shot to the incoming one at its OWN
    frame between F-3 and F+2 (a staggered hand-over: the cut is smeared over
    six frames instead of landing on one), jumps sideways by a stepped
    pseudo-random amount (a few tear much further), and the red and blue
    channels are pushed apart; a few blocks are dragged from elsewhere in the
    frame or flash violet."""
    h, w, _ = old.shape
    s = w / 720 if w <= h else h / 720
    step = (k + 100) // GLITCH_STEP
    bounds = [0]
    for i in range(1, GLITCH_SLICES):
        jitter = (_hash01(seed, "b", i, step) - 0.5) * 0.7
        bounds.append(int(round((i + jitter) / GLITCH_SLICES * h)))
    bounds.append(h)
    out = np.empty_like(old)
    for i in range(GLITCH_SLICES):
        y0, y1 = bounds[i], bounds[i + 1]
        if y1 <= y0:
            continue
        handover = -3 + int(_hash01(seed, "hand", i) * 6)          # -3 .. 2
        src = new if k >= handover else old
        tear = 2.6 if _hash01(seed, "tear", i, step) < 0.18 else 1.0
        dx = int(round((_hash01(seed, "dx", i, step) - 0.5) * 2 * GLITCH_SHIFT * s * amount * tear))
        out[y0:y1] = np.roll(src[y0:y1], dx, axis=1) if dx else src[y0:y1]
    d = int(round(GLITCH_SPLIT * s * amount * (0.6 + 0.4 * _hash01(seed, "split", step))))
    if d:
        out[..., 0] = np.roll(out[..., 0], d, axis=1)
        out[..., 2] = np.roll(out[..., 2], -d, axis=1)
    if amount > 0.35:
        for b in range(1 + int(_hash01(seed, "nb", step) * 4 * amount)):
            bh = max(2, int((3 + 16 * _hash01(seed, "bh", b, step)) * s))
            bw = int((0.1 + 0.35 * _hash01(seed, "bw", b, step)) * w)
            bx = int(_hash01(seed, "bx", b, step) * (w - bw))
            by = int(_hash01(seed, "by", b, step) * (h - bh))
            if _hash01(seed, "bk", b, step) < 0.35:
                out[by:by + bh, bx:bx + bw] = VIOLET_RGB
            else:
                sx = int(_hash01(seed, "sx", b, step) * (w - bw))
                out[by:by + bh, bx:bx + bw] = (new if k >= 0 else old)[by:by + bh, sx:sx + bw]
    return out


def apply_glitch_plates(
    sources: list[BrandSource], segments: list[dict], seams: list[dict],
    overlay_dir: Path, frame_pattern: str, start_number: int, width: int, height: int,
) -> int:
    """Glitch the footage under every "glitch" cut: for each of its 12 frames
    the glitched footage is composited UNDER that frame's graphics PNG, and
    the PNG is rewritten opaque. Returns how many frames were rewritten."""
    from PIL import Image

    done = 0
    for i, seam in enumerate(seams):
        if seam.get("style") != "glitch":
            continue
        F = int(seam["frame"])
        a, b = segments[i], segments[i + 1]
        na, nb = int(a["frames"]), int(b["frames"])
        before, after = len(GLITCH_BEFORE), len(GLITCH_AFTER)
        # Outgoing: its last `before` frames (held on its last one after the
        # cut); incoming: its first `after` frames (held on its first before).
        old_frames = _decode_rgb(sources[i], na, list(range(na - before, na)), width, height)
        new_frames = _decode_rgb(sources[i + 1], nb, list(range(0, after)), width, height)
        seed = f"cut-{F}"
        for k in range(-before, after):
            old = old_frames[min(max(0, na + min(k, -1)), na - 1)]
            new = new_frames[min(max(0, k), nb - 1)]
            amount = GLITCH_BEFORE[k + before] if k < 0 else GLITCH_AFTER[k]
            plate = Image.fromarray(glitch_plate(old, new, k, amount, seed)).convert("RGBA")
            path = overlay_dir / (frame_pattern % (start_number + F + k))
            with Image.open(path) as g:
                plate.alpha_composite(g.convert("RGBA"))
            plate.save(path)
            done += 1
    return done


# ---------------------------------------------------------------------------
# Compositing
# ---------------------------------------------------------------------------

def audio_shift(path: Path) -> str:
    """Filters (with a leading comma, or "") that put a source's sound back
    where it starts relative to its picture, after both were zeroed.

    The video chain zeroes on the first frame and the audio chain on the
    first sample, each on its own, so a clip whose sound starts 177 ms after
    its picture came out 177 ms early (an uploaded bookend, a remux, a
    Remotion render whose audio lags 45 ms). Sound that starts late gets that
    much silence in front; sound that starts early loses the part before the
    first frame. Sample-exact at 48 kHz; raw Veo clips (both at 0) get none."""
    v0, a0 = media.stream_starts(path)
    if v0 is None or a0 is None:
        return ""
    n = int(round((a0 - v0) * AUDIO_RATE))
    if n > 0:
        return f",adelay=delays={n}S:all=1"
    if n < 0:
        return f",atrim=start_sample={-n},asetpts=PTS-STARTPTS"
    return ""


def composite(
    sources: list[BrandSource],
    segments: list[dict],
    overlay_dir: Path,
    frame_pattern: str,
    start_number: int,
    out: Path,
    *,
    width: int,
    height: int,
) -> Path:
    """Normalise + hard-cut every source and lay the graphics on top, in ONE
    ffmpeg run and one encode.

    Each source is cut/held to exactly its segment's frame count and its audio
    padded/cut to exactly the same length, so every join lands on the frame
    the graphics were drawn for and the voice stays on the lips (no AAC
    frame drift: nothing is stream-copied between pieces). A source whose
    sound starts before/after its picture keeps that offset (`audio_shift`)."""
    if len(sources) != len(segments):
        raise ValueError("one segment per source")
    out.parent.mkdir(parents=True, exist_ok=True)
    total = sum(int(s["frames"]) for s in segments)
    cmd = [media.ffmpeg_exe(), "-y", "-hide_banner"]
    chains: list[str] = []
    pairs: list[str] = []
    for i, (src, seg) in enumerate(zip(sources, segments)):
        frames = int(seg["frames"])
        seconds = frames / FPS
        if src.is_still:
            cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{seconds:.6f}", "-i", str(src.path)]
        else:
            cmd += ["-i", str(src.path)]
        chains.append(f"[{i}:v]{source_chain(width, height, frames)}[v{i}]")
        if not src.is_still and media.has_audio(src.path):
            chains.append(
                f"[{i}:a]aresample={AUDIO_RATE},aformat=channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS{audio_shift(src.path)},"
                f"apad,atrim=duration={seconds:.6f},asetpts=PTS-STARTPTS[a{i}]"
            )
        else:
            chains.append(
                f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=duration={seconds:.6f},"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
        pairs.append(f"[v{i}][a{i}]")
    g = len(sources)
    cmd += ["-framerate", str(FPS), "-start_number", str(int(start_number)),
            "-i", str(overlay_dir / frame_pattern)]
    chains.append(f"{''.join(pairs)}concat=n={len(sources)}:v=1:a=1[vc][ac]")
    # trim: a longer PNG sequence would otherwise keep the output running on
    # the last footage frame (the overlay follows its second input past the
    # main's end). The count was verified; this makes it certain. NOT
    # `-frames:v` — that stops the mux the moment the video is done and cut
    # the last ~45 ms of AUDIO off every reel.
    chains.append(f"[{g}:v]trim=end_frame={total},{_RGBA_TO_YUV}[g]")
    chains.append("[vc][g]overlay=format=yuv420:eof_action=pass[out]")
    cmd += [
        "-filter_complex", ";".join(chains),
        "-map", "[out]", "-map", "[ac]",
        # The concat filter works in microseconds; without an explicit rate
        # the file is tagged "30.51 fps, 1000k tbn". The frames are already on
        # the 1/30 grid, so this only fixes the tag (no dup/drop — the count
        # is checked after the encode).
        "-r", str(FPS),
        "-c:v", "libx264", "-crf", str(CRF), "-preset", "medium", "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-color_range", "tv",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    # One 1080p encode of the whole reel: measured 0.05-0.12 s per
    # frame on the dev laptop depending on load, so a long reel needs more
    # than the default 10 minutes.
    media._run(cmd, what="brand composite",
               timeout=max(media.ENCODE_TIMEOUT_SEC, total * 0.6))
    if not out.exists() or out.stat().st_size == 0:
        raise media.MediaError("brand composite wrote no file")
    return out


def count_frames(path: Path) -> int:
    """Video frames in a file, by counting packets (no decode — instant). An
    mp4 video sample is one frame, so this is exact for our own output.

    framecrc prints one line per packet. The `frame=` progress line is not a
    contract: the Linux ffmpeg 7.0 build imageio-ffmpeg ships leaves it out of
    a stream copy's final stats, which read as 0 frames on the server."""
    proc = subprocess.run(
        [media.ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy",
         "-f", "framecrc", "-"],
        capture_output=True, text=True, errors="replace", timeout=120,
    )
    return sum(1 for line in (proc.stdout or "").splitlines() if line and not line.startswith("#"))


# ---------------------------------------------------------------------------
# Graphics (Remotion) + the orchestrator
# ---------------------------------------------------------------------------

_PATTERN_RE = re.compile(r"^[\w.\-]*%0?\d*d[\w.\-]*\.png$")


async def _remotion_graphics(props: dict, out_dir: Path) -> dict:
    """The default renderer: `brand-overlay-42` as a transparent PNG sequence."""
    from . import remotion_client

    # ~0.1 s a frame measured for graphics-only PNGs at 720p; leave headroom
    # for 1080p and a busy render gate, never less than the client default.
    job = await remotion_client.render_frames(
        TEMPLATE, out_dir, props,
        timeout=max(900.0, float(props["durationInFrames"]) * 1.0))
    return {
        "frame_pattern": job.get("frame_pattern"),
        "start_number": job.get("start_number"),
        "frame_count": job.get("frame_count"),
        # The composition measures the text with the real fonts before any
        # frame renders; the server echoes that report on the job.
        "text_fit": job.get("text_fit"),
    }


def _check_sequence(
    overlay_dir: Path, pattern: str, start: int, count: int, width: int, height: int,
) -> None:
    """Fail loudly BEFORE an encode on a sequence the composite would misuse:
    a pattern that is not a plain file name, a missing first/last frame, or a
    frame of the wrong size (the overlay would sit at 0,0 unscaled)."""
    if not _PATTERN_RE.match(pattern or ""):
        raise media.MediaError(f"unexpected graphics frame pattern {pattern!r}")
    first = overlay_dir / (pattern % start)
    last = overlay_dir / (pattern % (start + count - 1))
    for p in (first, last):
        if not p.is_file():
            raise media.MediaError(f"graphics frame missing: {p.name}")
    from PIL import Image

    with Image.open(first) as im:
        if im.size != (width, height):
            raise media.MediaError(
                f"graphics frames are {im.size[0]}x{im.size[1]}, the reel is {width}x{height}")


def _promote_to_rgba(overlay_dir: Path, pattern: str, start: int, count: int) -> int:
    """Rewrite every frame that is not RGBA as RGBA; returns how many.

    Chrome writes a frame with no transparent pixel (a full-cover wipe) as an
    RGB PNG. A sequence that mixes RGB and RGBA frames makes ffmpeg rebuild
    its filter graph mid-stream: measured, the overlay then buffered the whole
    reel (16 GB for 477 frames at 1080x1920) and dropped frames. The render
    server already promotes such frames; this makes the composite safe for
    any renderer. Only headers are read for the frames that are fine."""
    from PIL import Image

    fixed = 0
    for i in range(start, start + count):
        path = overlay_dir / (pattern % i)
        with Image.open(path) as im:
            if im.mode == "RGBA":
                continue
            rgba = im.convert("RGBA")
        rgba.save(path)
        fixed += 1
    return fixed


async def _stage(cb: Optional[StageCallback], key: str, detail: str) -> None:
    if cb is None:
        return
    try:
        await cb(key, detail)
    except Exception:  # noqa: BLE001 — progress reporting must never kill a render
        log.exception("[brand] stage callback failed")


async def render_brand_reel(
    sources: list[BrandSource],
    work_dir: Path,
    *,
    width: int,
    height: int,
    style: str = "auto",
    text: Optional[dict] = None,
    logo_on_cards: bool = False,
    scene_text: Optional[dict[int, str]] = None,
    edge_patterns: bool = True,
    render_graphics: Optional[GraphicsRenderer] = None,
    on_stage: Optional[StageCallback] = None,
) -> BrandResult:
    """Measure → draw → composite. Returns the final MP4 and what was drawn.

    `render_graphics(props, out_dir)` renders the PNG sequence and returns
    {"frame_pattern", "start_number", "frame_count"}; sync or async. The
    default is Remotion. The PNG folder lives under `work_dir` (under
    backend/data, where the Remotion container can write) and is deleted once
    the composite succeeded. `on_stage(key, detail)` is told when each phase
    ("measure" | "graphics" | "composite") starts.

    The composition sizes the text itself and leaves out (never cuts) a part
    that cannot fit; the renderer may return its report as "text_fit", which
    comes back on the result for the arm to show.

    `scene_text` ({source position: that scene's dialogue}) turns on topic
    icons (brand_icons), timed on each clip's own voiced span; None = none.
    `edge_patterns` slides the pattern over the picture's left edge too."""
    if not sources:
        raise media.MediaError("nothing to brand")
    work_dir.mkdir(parents=True, exist_ok=True)

    await _stage(on_stage, "measure", f"{len(sources)} clip(s) at {width}x{height}")
    segments = await asyncio.to_thread(build_timeline, sources, width, height)
    seams = build_seams(segments, style)
    icons: list[dict] = []
    if scene_text:
        from . import brand_icons

        voiced = {
            i: await asyncio.to_thread(brand_icons.voiced_span, src.path, is_still=src.is_still)
            for i, src in enumerate(sources) if src.kind == "scene" and scene_text.get(i)
        }
        icons = brand_icons.plan_icons(segments, scene_text, voiced)
    props = build_props(segments, seams, width=width, height=height,
                        text=text, logo_on_cards=logo_on_cards,
                        icons=icons, edge_patterns=edge_patterns)
    total = props["durationInFrames"]
    # Kept next to the output: the exact input of the render, re-playable in
    # Remotion Studio / `npx remotion render` when a frame looks wrong.
    (work_dir / "props.json").write_text(json.dumps(props, indent=2), encoding="utf-8")
    log.info("[brand] %d frames, layouts %s, seams %s", total,
             [s["layout"] for s in segments], [s["style"] for s in seams])

    gdir = work_dir / "graphics"
    if gdir.exists():
        shutil.rmtree(gdir, ignore_errors=True)
    gdir.mkdir(parents=True, exist_ok=True)
    await _stage(on_stage, "graphics",
                 f"{total} frames · {', '.join(s['layout'] for s in segments)}"
                 + (f" · {', '.join(s['style'] for s in seams)}" if seams else ""))
    t0 = time.monotonic()
    renderer = render_graphics or _remotion_graphics
    res = renderer(props, gdir)
    if inspect.isawaitable(res):
        res = await res
    graphics_seconds = time.monotonic() - t0
    res = res or {}
    frame_count = int(res.get("frame_count") or 0)
    if frame_count != total:
        raise media.MediaError(
            f"graphics render produced {frame_count} frames, the reel needs {total}")
    pattern = str(res.get("frame_pattern") or "")
    start = int(res.get("start_number") or 0)
    await asyncio.to_thread(_check_sequence, gdir, pattern, start, frame_count, width, height)
    promoted = await asyncio.to_thread(_promote_to_rgba, gdir, pattern, start, frame_count)
    if promoted:
        log.info("[brand] %d opaque graphics frame(s) rewritten as RGBA", promoted)
    if any(s["style"] == "glitch" for s in seams):
        await asyncio.to_thread(apply_glitch_plates, sources, segments, seams, gdir, pattern, start,
                                width, height)

    await _stage(on_stage, "composite", f"{total} frames")
    t1 = time.monotonic()
    final = await asyncio.to_thread(
        composite, sources, segments, gdir, pattern, start, work_dir / "final.mp4",
        width=width, height=height,
    )
    composite_seconds = time.monotonic() - t1
    written = await asyncio.to_thread(count_frames, final)
    if written != total:
        raise media.MediaError(f"brand composite wrote {written} frames, expected {total}")
    shutil.rmtree(gdir, ignore_errors=True)
    return BrandResult(
        final=final, props=props, segments=segments, seams=seams,
        graphics_seconds=round(graphics_seconds, 2),
        composite_seconds=round(composite_seconds, 2),
        text_fit=res.get("text_fit") if isinstance(res.get("text_fit"), dict) else None,
        icons=list(props["icons"]),
    )
