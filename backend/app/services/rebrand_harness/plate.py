"""The in-house engine's graphics for one frame — the plate.

  plate_at(spec, i, camera=, bank=)   frame i's graphics as premultiplied
                                      RGBA uint8 HxW (transparent wherever
                                      nothing is drawn), for ffmpeg to lay
                                      over the untouched footage (house.render)
  over_frame(frame_bgr, plate)        a still: that footage frame with the
                                      plate laid on it
  motion(art, ...)                    one pattern layer in its entrance or
                                      its exit at progress q

The footage is never painted: only this plate is made, and ffmpeg lays it
on the source in one encode, so outside it every pixel and the sound are
the source's own. ONE function draws a frame's graphics — the run pipes it
for every frame, and the studio's stills and moving preview are made from
the same spec with the same function — so what the studio shows is what
Generate makes.

Per frame (t = i / fps, scene seconds), in this order:

  1. each pattern layer BEHIND the speaker that shows at t (its span:
     elements/pattern.timing), back to front: its motion at t in the
     layer's own space (the entrance's or the exit's; nothing while it
     holds), then ONE warp by camera(i) · hand · T(-margin) — a "room"
     layer moves with the footage's push-in, a "screen" one (a letterbox
     bar's, by default) holds still — laid "over" the plate;
  2. our icons behind the speaker (icon_render.Stamper.drawn), fixed on
     screen;
  3. the speaker cut out of all of it: plate × (1 − matte(i)), the matte
     bank's (mattes.MatteBank: the same matte for every caller), zero on
     the letterbox bars — the speaker is never in one;
  4. the icons in front of her, and any layer placed in front, uncut.

The warp is skipped when it is a whole-pixel move (frame 0 of every camera
track is the identity), so a pattern that has not moved is its art, byte
for byte. `settled` is the design still: the layers that show at `at` (all
of them without it) fully in, the icons on screen then at rest.

The motions (the 42 grammar; the easing is the icons', icon_render):

  fade                 every line together
  left_to_right, …     a wipe: a soft edge (WIPE_SOFT of the ink box) sweeps
                       the pattern's own ink box, so it lasts its full length
  to_left, …           an exit: that wipe played backwards (the lines go back
                       towards that edge)
  glitch               horizontal slices of the lines jump sideways and split
                       into red / cyan copies, hand over one by one and
                       flicker (seeded: the same seed, the same frames)
  slide / slide_out    the whole pattern slides in from (out to) the edge it
                       grows from
  none                 there from its first frame / to its last

An entrance eases out-expo, an exit in-out cubic (the glitch runs linear,
so its bursts last the whole ramp). Pure: numpy and OpenCV —
no disk, no clock, no model. The same spec and frame give the same bytes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from .icon_render import _hash01, _in_out_cubic, _out_expo

if TYPE_CHECKING:
    from .elements.pattern import Timing

# Bumped whenever the plate can come out differently for the same spec (a
# version records the one it was drawn with; a stale draft render is made
# again).
HOUSE_VERSION = 1

# A wipe's soft edge, as a share of the ink box along its sweep.
WIPE_SOFT = 0.12
# The glitch at layer scale (icon_render's grammar): new slice bounds every
# GLITCH_STEP frames, GLITCH_SLICES slices jumping up to GLITCH_SHIFT of the
# ink box's shorter side (a few 2.6x further), red / cyan copies split
# GLITCH_SPLIT apart, and a strong glitch flickering the layer off now and
# then.
GLITCH_STEP = 2
GLITCH_SLICES = 10
GLITCH_SHIFT = 0.08
GLITCH_SPLIT = 0.02
GLITCH_TEAR = 2.6
GLITCH_FLICKER = 0.16
_RED = np.array([1.0, 0.12, 0.3], np.float32)
_CYAN = np.array([0.1, 0.9, 1.0], np.float32)

# kind -> (axis, sweeps from the far end): an exit is the entrance of that
# direction played backwards.
WIPES: dict[str, tuple[str, bool]] = {
    "left_to_right": ("x", False), "right_to_left": ("x", True),
    "top_to_bottom": ("y", False), "bottom_to_top": ("y", True),
    "to_left": ("x", False), "to_right": ("x", True),
    "to_top": ("y", False), "to_bottom": ("y", True),
}
EDGES = ("left", "right", "top", "bottom")


@dataclass(frozen=True, eq=False)
class LayerSpec:
    """One pattern as the plate draws it.

      art       its layer in its place, premultiplied RGBA uint8 — frame-
                sized; moved by hand, its place UNMOVED over the window the
                move needs (rebrand_pattern_ref.windowed), cut to its ink
      margin    (mW, mH): where the frame's origin sits on the art (art px;
                0 unless moved by hand, and negative when the art starts
                inside the frame)
      ink_box   (x0, y0, x1, y1): where its ink is, in art px (wipes sweep
                it; nothing outside it is drawn)
      follow    "room" (moves with the camera) | "screen" (holds still)
      z         "behind" (cut by the speaker) | "front"
      timing    its span (elements/pattern.timing), None: it never shows
      seed      its motion's seed
      hand      2x3 frame px -> frame px: its hand move (None: none) — in
                the ONE warp with the camera, so a thin line is resampled
                once
      edge      where a slide comes from (left | right | top | bottom)
      colour    #rrggbb it is drawn in (what the icons are told)"""
    element_id: str
    art: Any
    ink_box: tuple[int, int, int, int]
    timing: Optional["Timing"]
    margin: tuple[int, int] = (0, 0)
    follow: str = "room"
    z: str = "behind"
    seed: str = ""
    hand: Any = None
    edge: str = "left"
    colour: str = ""


@dataclass(frozen=True, eq=False)
class PlateSpec:
    """Everything a scene's plate is drawn from: the frame's size and rate,
    its pattern layers in draw order (the first is the back-most), our
    icons (ONE Stamper for the whole scene, offset 0, and the resolved
    icons it was made from), the letterbox bars and whether the speaker is
    cut out."""
    width: int
    height: int
    fps: float
    n: int
    layers: tuple = ()
    stamper: Any = None
    icons: tuple = ()
    bars: Any = None
    speaker_front: bool = True
    engine_v: int = HOUSE_VERSION


@dataclass(frozen=True, eq=False)
class Moved:
    """A layer in its motion this frame: the art as drawn, moved (dx, dy)
    art px, with its ink inside `box` (art px)."""
    art: Any
    dx: float
    dy: float
    box: tuple[int, int, int, int]


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

def shown_share(direction: str, q: float) -> float:
    """How much of the layer shows at progress q of its entrance ("in",
    out-expo) or its exit ("out", in-out cubic): 0 gone, 1 fully in."""
    q = 0.0 if q < 0 else 1.0 if q > 1 else float(q)
    return _out_expo(q) if direction == "in" else 1.0 - _in_out_cubic(q)


def wipe_ramp(size: int, lo: int, hi: int, e: float, *, from_high: bool) -> Any:
    """The wipe's coverage (float32, 0..1) along one axis of `size` px: a
    soft edge sweeping [lo, hi) — nothing at e = 0, all of it at e = 1."""
    span = max(1.0, float(hi - lo))
    soft = max(1.0, WIPE_SOFT * span)
    lead = lo - soft + e * (span + 2.0 * soft)
    c = np.arange(size, dtype=np.float64) + 0.5
    if from_high:
        c = lo + hi - c
    return np.clip((lead - c) / soft, 0.0, 1.0).astype(np.float32)


def _over_shift(dst: Any, src: Any, dx: int) -> None:
    """Premultiplied `src` "over" `dst` (same height, float32), moved dx px
    sideways, in place."""
    w = dst.shape[1]
    if abs(dx) >= w:
        return
    if dx >= 0:
        s, d = src[:, :w - dx], dst[:, dx:]
    else:
        s, d = src[:, -dx:], dst[:, :w + dx]
    d *= 1.0 - s[..., 3:4] / 255.0
    d += s


def _glitch_pad(box: tuple[int, int, int, int]) -> int:
    x0, y0, x1, y1 = box
    L = max(1, min(x1 - x0, y1 - y0))
    return int(math.ceil(GLITCH_TEAR * GLITCH_SHIFT * L + GLITCH_SPLIT * L + 2))


def glitched(art: Any, box: tuple[int, int, int, int], *, amount: float, reveal: float, seed: str,
             frame: int) -> Optional[Any]:
    """The layer glitching (icon_render._glitched at layer scale), float32
    premultiplied, or None on a frame it flickers off (or before any slice
    shows). `amount` how strong (1 at the start of an entrance), `reveal`
    how many slices have handed over."""
    if reveal <= 0.0:
        return None
    if amount <= 0.001 and reveal >= 1.0:
        return art
    step = int(frame) // GLITCH_STEP
    if amount > 0.5 and _hash01(seed, "flicker", step) < GLITCH_FLICKER * amount:
        return None
    x0, y0, x1, y1 = box
    L = max(1, min(x1 - x0, y1 - y0))
    shift, split = GLITCH_SHIFT * L, GLITCH_SPLIT * L
    split1 = split * amount * (0.6 + 0.4 * _hash01(seed, "split", step))
    h = max(1, y1 - y0)
    bounds = [y0]
    for k in range(1, GLITCH_SLICES):
        jit = (_hash01(seed, "b", k, step) - 0.5) * 0.7
        bounds.append(y0 + int(round((k + jit) / GLITCH_SLICES * h)))
    bounds.append(y1)
    src = np.asarray(art, dtype=np.float32)
    out = np.zeros_like(src)
    for k in range(GLITCH_SLICES):
        a, b = bounds[k], bounds[k + 1]
        if b <= a or _hash01(seed, "rank", k) >= reveal:
            continue
        tear = GLITCH_TEAR if _hash01(seed, "tear", k, step) < 0.18 else 1.0
        dx = int(round((_hash01(seed, "dx", k, step) - 0.5) * 2.0 * shift * amount * tear))
        piece = src[a:b]
        band = out[a:b]
        if split1 > 0.5:
            s = int(round(split1))
            alpha = piece[..., 3:4] * 0.85
            _over_shift(band, np.concatenate([alpha * _CYAN, alpha], axis=-1), dx - s)
            _over_shift(band, np.concatenate([alpha * _RED, alpha], axis=-1), dx + s)
        _over_shift(band, piece, dx)
    return out


def slide_offset(edge: str, box: tuple[int, int, int, int], margin: tuple[int, int],
                 frame_size: tuple[int, int], hidden: float) -> tuple[float, float]:
    """How far a sliding layer is moved (art px) with `hidden` (0..1) of its
    slide still to go: all of it puts its ink just past the frame's `edge`."""
    x0, y0, x1, y1 = box
    mW, mH = margin
    W, H = frame_size
    if edge == "right":
        return hidden * (W - (x0 - mW)), 0.0
    if edge == "top":
        return 0.0, -hidden * (y1 - mH)
    if edge == "bottom":
        return 0.0, hidden * (H - (y0 - mH))
    return -hidden * (x1 - mW), 0.0


def motion(art: Any, *, ink_box: tuple[int, int, int, int], kind: str, direction: str, q: float,
           seed: str, frame: int, edge: str = "left", margin: tuple[int, int] = (0, 0),
           frame_size: Optional[tuple[int, int]] = None) -> Optional[Moved]:
    """A layer at progress q (0..1) of its entrance (`direction` "in") or
    its exit ("out"), drawn as `kind` (the module doc) — or None when
    nothing of it shows this frame. `art` premultiplied RGBA uint8; the
    result's art is the same array when the motion leaves it as it is."""
    e = shown_share(direction, q)
    box = tuple(int(v) for v in ink_box)
    if kind == "none":
        return Moved(art, 0.0, 0.0, box)
    if kind == "fade":
        if e <= 0.0:
            return None
        return Moved(art if e >= 1.0 else _scaled(art, box, np.float32(e)), 0.0, 0.0, box)
    if kind in WIPES:
        if e <= 0.0:
            return None
        if e >= 1.0:
            return Moved(art, 0.0, 0.0, box)
        axis, high = WIPES[kind]
        h, w = art.shape[:2]
        x0, y0, x1, y1 = box
        if axis == "x":
            ramp = wipe_ramp(w, x0, x1, e, from_high=high)[None, x0:x1, None]
        else:
            ramp = wipe_ramp(h, y0, y1, e, from_high=high)[y0:y1, None, None]
        return Moved(_scaled(art, box, ramp), 0.0, 0.0, box)
    if kind == "glitch":
        # Linear, not eased: the slices hand over one by one across the
        # whole ramp and the jumps calm down evenly (eased out-expo, it
        # would be over in its first third).
        p = min(1.0, max(0.0, float(q))) if direction == "in" else 1.0 - min(1.0, max(0.0, float(q)))
        got = glitched(art, box, amount=1.0 - p, reveal=p, seed=seed, frame=frame)
        if got is None:
            return None
        pad = _glitch_pad(box)
        return Moved(got, 0.0, 0.0, (box[0] - pad, box[1], box[2] + pad, box[3]))
    if kind in ("slide", "slide_out"):
        size = frame_size or (art.shape[1] - 2 * margin[0], art.shape[0] - 2 * margin[1])
        dx, dy = slide_offset(edge if edge in EDGES else "left", box, margin, size, 1.0 - e)
        return Moved(art, dx, dy, box)
    # An entrance this renderer does not know is drawn as a fade (never
    # silently as something else).
    if e <= 0.0:
        return None
    return Moved(art if e >= 1.0 else _scaled(art, box, np.float32(e)), 0.0, 0.0, box)


def _scaled(art: Any, box: tuple[int, int, int, int], k: Any) -> Any:
    """The art (premultiplied) times k — a number, or a ramp over the ink
    box — as float32: worked out inside the ink box only (nothing is
    outside it)."""
    x0, y0, x1, y1 = box
    out = np.zeros(art.shape, dtype=np.float32)
    out[y0:y1, x0:x1] = np.asarray(art[y0:y1, x0:x1], dtype=np.float32) * k
    return out


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _m3(m: Any) -> Any:
    out = np.eye(3, dtype=np.float64)
    if m is not None:
        out[:2] = np.asarray(m, dtype=np.float64)[:2]
    return out


def _shift(dx: float, dy: float) -> Any:
    out = np.eye(3, dtype=np.float64)
    out[0, 2], out[1, 2] = dx, dy
    return out


def _whole_pixel_move(m: Any) -> bool:
    return (m[0, 0] == 1.0 and m[1, 1] == 1.0 and m[0, 1] == 0.0 and m[1, 0] == 0.0
            and abs(m[0, 2] - round(m[0, 2])) < 1e-9 and abs(m[1, 2] - round(m[1, 2])) < 1e-9)


def _union(a: Optional[tuple], b: Optional[tuple]) -> Optional[tuple]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _warp_over(acc: Any, art: Any, m: Any, box: tuple[int, int, int, int]) -> Optional[tuple]:
    """`art` (premultiplied, art px) warped by m (3x3, art px -> frame px)
    and laid "over" `acc` — only inside where its ink box lands. The warp
    is skipped for a whole-pixel move. Returns the frame box drawn in."""
    import cv2

    H, W = acc.shape[:2]
    x0, y0, x1, y1 = box
    pts = np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]], dtype=np.float64)
    mapped = pts @ m[:2, :2].T + m[:2, 2]
    fx0 = max(0, int(math.floor(mapped[:, 0].min())) - 1)
    fy0 = max(0, int(math.floor(mapped[:, 1].min())) - 1)
    fx1 = min(W, int(math.ceil(mapped[:, 0].max())) + 1)
    fy1 = min(H, int(math.ceil(mapped[:, 1].max())) + 1)
    if fx1 <= fx0 or fy1 <= fy0:
        return None
    if _whole_pixel_move(m):
        tx, ty = int(round(m[0, 2])), int(round(m[1, 2]))
        ah, aw = art.shape[:2]
        sx0, sy0 = max(fx0 - tx, 0), max(fy0 - ty, 0)
        sx1, sy1 = min(fx1 - tx, aw), min(fy1 - ty, ah)
        if sx1 <= sx0 or sy1 <= sy0:
            return None
        piece = art[sy0:sy1, sx0:sx1]
        fx0, fy0, fx1, fy1 = sx0 + tx, sy0 + ty, sx1 + tx, sy1 + ty
    else:
        mr = m[:2].copy()
        mr[0, 2] -= fx0
        mr[1, 2] -= fy0
        piece = cv2.warpAffine(art, mr, (fx1 - fx0, fy1 - fy0), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    piece = np.asarray(piece, dtype=np.float32)
    region = acc[fy0:fy1, fx0:fx1]
    region *= 1.0 - piece[..., 3:4] / 255.0
    region += piece
    return fx0, fy0, fx1, fy1


def _over_sprite(acc: Any, rgba: Any, x: int, y: int) -> Optional[tuple]:
    """A straight-alpha RGBA sprite (an icon) "over" `acc` with its
    top-left at (x, y), clipped to the frame."""
    H, W = acc.shape[:2]
    h, w = rgba.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    src = np.asarray(rgba[y0 - y:y1 - y, x0 - x:x1 - x], dtype=np.float32)
    a = src[..., 3:4] / 255.0
    if not a.any():
        return None
    region = acc[y0:y1, x0:x1]
    region *= 1.0 - a
    region[..., :3] += src[..., :3] * a
    region[..., 3:] += src[..., 3:]
    return x0, y0, x1, y1


def _layer_now(layer: LayerSpec, frame: int, t: float, settled: bool, at: Optional[float],
               frame_size: tuple[int, int]) -> Optional[Moved]:
    """The layer as it is drawn this frame, or None when it does not show."""
    tm = layer.timing
    if tm is None:
        return None
    if settled:
        if at is not None and tm.phase(float(at))[0] in ("before", "after"):
            return None
        return Moved(layer.art, 0.0, 0.0, layer.ink_box)
    phase, q = tm.phase(t)
    if phase in ("before", "after"):
        return None
    if phase == "hold":
        return Moved(layer.art, 0.0, 0.0, layer.ink_box)
    kind, direction = (tm.entrance, "in") if phase == "in" else (tm.exit, "out")
    return motion(layer.art, ink_box=layer.ink_box, kind=kind, direction=direction, q=q, seed=layer.seed,
                  frame=frame, edge=layer.edge, margin=layer.margin, frame_size=frame_size)


def _draw_layer(acc: Any, layer: LayerSpec, frame: int, t: float, cam: Any, settled: bool,
                at: Optional[float]) -> Optional[tuple]:
    H, W = acc.shape[:2]
    got = _layer_now(layer, frame, t, settled, at, (W, H))
    if got is None:
        return None
    m = (cam if layer.follow == "room" else np.eye(3)) @ _m3(layer.hand) @ _shift(
        got.dx - layer.margin[0], got.dy - layer.margin[1])
    return _warp_over(acc, got.art, m, got.box)


def _icon_pieces(stamper: Any, t: float, layer: str, settled: bool) -> list:
    """Our icons of `layer` on screen at t: as drawn this frame, or — the
    design still — each at rest."""
    if stamper is None:
        return []
    if not settled:
        return stamper.drawn(t, layer=layer)
    out = []
    for k in stamper.active(t):
        it = stamper.items[k]
        if it.layer != layer:
            continue
        out.append((stamper.sprite_of(k).rgba, it.box[0] - it.pad, it.box[1] - it.pad))
    return out


def plate_at(spec: PlateSpec, i: int, *, camera: Any = None, bank: Any = None, settled: bool = False,
             at: Optional[float] = None) -> Any:
    """Frame i's plate: premultiplied RGBA uint8, spec.height x spec.width
    (the module doc). `camera` a camera.CameraTrack (None: it holds still),
    `bank` the scene's mattes.MatteBank (None, or spec.speaker_front False:
    nothing is cut). `settled` the design still: the layers that show at
    `at` (every one without it) fully in, the icons on screen then at rest
    — the speaker cut at frame i."""
    W, H = int(spec.width), int(spec.height)
    i = min(max(0, int(i)), max(0, int(spec.n) - 1))
    t = i / float(spec.fps)
    when = t if (at is None or not settled) else float(at)
    cam = _m3(camera.at(i)) if camera is not None else np.eye(3)
    acc = np.zeros((H, W, 4), dtype=np.float32)
    drawn: Optional[tuple] = None
    for layer in spec.layers:
        if layer.z != "front":
            drawn = _union(drawn, _draw_layer(acc, layer, i, t, cam, settled, at))
    for rgba, x, y in _icon_pieces(spec.stamper, when, "behind", settled):
        drawn = _union(drawn, _over_sprite(acc, rgba, int(x), int(y)))
    if spec.speaker_front and bank is not None and drawn is not None:
        m = bank.at(i, bars=spec.bars)
        x0, y0, x1, y1 = drawn
        acc[y0:y1, x0:x1] *= (1.0 - m[y0:y1, x0:x1])[..., None]
    for rgba, x, y in _icon_pieces(spec.stamper, when, "front", settled):
        drawn = _union(drawn, _over_sprite(acc, rgba, int(x), int(y)))
    for layer in spec.layers:
        if layer.z == "front":
            drawn = _union(drawn, _draw_layer(acc, layer, i, t, cam, settled, at))
    out = np.zeros((H, W, 4), dtype=np.uint8)
    if drawn is not None:
        x0, y0, x1, y1 = drawn
        out[y0:y1, x0:x1] = np.clip(np.rint(acc[y0:y1, x0:x1]), 0, 255).astype(np.uint8)
    return out


def over_frame(frame_bgr: Any, plate: Any) -> Any:
    """The still: a BGR uint8 frame with a plate (premultiplied RGBA) on it."""
    a = plate[..., 3:4].astype(np.float32) / 255.0
    bgr = plate[..., 2::-1].astype(np.float32)
    return np.clip(np.rint(bgr + frame_bgr.astype(np.float32) * (1.0 - a)), 0, 255).astype(np.uint8)


def straight(plate: Any) -> Any:
    """A premultiplied plate as straight-alpha RGBA uint8 (what ffmpeg's
    overlay is handed: see house.render)."""
    out = np.array(plate, dtype=np.uint8, copy=True)
    a = plate[..., 3]
    part = (a > 0) & (a < 255)                 # clear and solid pixels stay as they are
    if part.any():
        px = plate[part].astype(np.float32)
        px[:, :3] = np.clip(np.rint(px[:, :3] * 255.0 / px[:, 3:4]), 0, 255)
        out[part] = px.astype(np.uint8)
    return out


def premultiplied(rgba: Any) -> Any:
    """Straight RGBA uint8 as premultiplied RGBA uint8."""
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    out = np.empty_like(rgba)
    out[..., :3] = np.clip(np.rint(rgba[..., :3].astype(np.float32) * a), 0, 255).astype(np.uint8)
    out[..., 3] = rgba[..., 3]
    return out


def ink_box(alpha: Any) -> Optional[tuple[int, int, int, int]]:
    """(x0, y0, x1, y1) of an alpha's ink (> 0), or None when it has none."""
    a = np.asarray(alpha)
    rows = np.nonzero(a.any(axis=1))[0]
    cols = np.nonzero(a.any(axis=0))[0]
    if not rows.size or not cols.size:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1
