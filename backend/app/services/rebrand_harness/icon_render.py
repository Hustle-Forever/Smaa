"""Topic icons drawn by us, after Omni — the pixels of one stamped icon.

Everything here is pure and deterministic (numpy + PIL, no model, no disk,
no clock): the same item at the same frame is the same bytes on every run and
machine, so the studio's exact still, the run and a retry agree.

  path_alpha(d, px)          a Phosphor "fill" path (SVG path data on a
                             256 x 256 box) as a px x px alpha map. No cairo:
                             fontTools parses the path (arcs become cubics),
                             the curves are flattened, and every contour adds
                             its SIGNED crossings to a per-row winding count
                             (nonzero rule, so the "</>" in `code` and the
                             face of `clock` stay holes), 4x supersampled and
                             box-averaged down (exact pixel coverage).
  badge_sprite(...)          the Remotion IconBadge port (brand42/IconBadge
                             .tsx): a square of side L in the fill colour with
                             its top-right corner cut at 45 degrees (0.24 L),
                             the glyph at 0.6 L in the ink colour. Flat.
  glyph_sprite(...)          "Just the icon": the glyph at 0.8 L in the fill
                             colour over a soft 35 % halo in the ink colour.
  circle_sprite(...)         style E, "Circle": a disc of diameter L in the
                             fill, the (bold) glyph at 0.53 L in the ink, a
                             soft drop shadow (shadowed).
  chip_sprite(...)           style H, "Circle + words": a pill of height h
                             in the fill, the glyph then the words (Jost
                             SemiBold / Noto Kufi Arabic, shaped by HarfBuzz
                             and filled by the rasteriser above: label_font)
                             in the ink, mirrored for Arabic; the same
                             shadow. chip_geometry is its width, shared with
                             the planner so a planned box and the stamped
                             body agree.
  make_sprite(...)           any style as a Sprite (the pixels + where the
                             body sits inside them); sprite(...) its pixels,
                             byte-identical to before for the badge / glyph.
  pick_colours(...)          ONE (fill, ink) per scene from the brand's
                             candidates, checked by CIEDE2000 against every
                             spot's background and against the pattern.
  fx(...)                    the entrance / exit of one frame: glitch (the
                             brand42 Glitch.tsx grammar on the sprite), pop,
                             fade, slide from the nearest edge, none — each
                             icon may leave its own way. fx_progress(...) is
                             its numbers alone (the studio's moving preview
                             ports them frame for frame).
  Stamper                    the icons on screen at a chunk-local time:
                             drawn(t, layer) the pieces (straight RGBA at a
                             place), under() composites the ones BEHIND the
                             speaker (the caller blends the speaker back on
                             top), over() the ones in FRONT of her (after).

Sprites are straight (not premultiplied) RGBA uint8, RGB order. Frames are
OpenCV's BGR uint8.
"""
from __future__ import annotations

import hashlib
import io
import math
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

import numpy as np

VIEWBOX = 256.0                     # Phosphor's path box
SUPERSAMPLE = 4

# Badge geometry (IconBadge.tsx) and the bare-glyph style.
BADGE_CUT = 0.24                    # the 45-degree cut's legs, share of L
BADGE_GLYPH = 0.6                   # glyph box inside the badge, share of L
GLYPH_ONLY = 0.8                    # glyph box of "Just the icon", share of L
HALO_ALPHA = 0.35                   # its halo's strength...
HALO_SIGMA = 0.035                  # ...and softness (Gaussian sigma, share of L)

# The circle (style E) and the chip (style H), from the owner's style sheet
# (make_sheet.py), normalised. L = the icon's size x the frame's short side.
CIRCLE_GLYPH = 0.53                 # glyph box inside the disc, share of L
CHIP_H = 0.74                       # the chip's height h, share of L
CHIP_GLYPH = 0.70                   # its glyph box, share of h...
CHIP_PAD_GLYPH = 0.24               # ...the pad on the glyph's side...
CHIP_GAP = 0.15                     # ...glyph to words...
CHIP_PAD_WORDS = 0.28               # ...and the pad on the words' side
CHIP_EM = 0.37                      # the words' em, share of h; shrunk down to...
CHIP_EM_MIN = 0.30                  # ...this when the chip would be wider than...
CHIP_MAX_W = 0.92                   # ...this share of the frame, then cut with "…"
ELLIPSIS = "…"
SHADOW_SIGMA = 0.08                 # the soft shadow: Gaussian sigma, share of L...
SHADOW_ALPHA = 0.40                 # ...its strength...
SHADOW_DY = 0.05                    # ...and its drop, share of L
LOOK_V = 1                          # the circle / chip sprite recipe (IconResolved.look_v)

# Brand colours (services/remotion/src/brand42/tokens.ts COLOR).
VIOLET = "#7f2bf2"
VIOLET_B = "#8d01fa"
CHARCOAL = "#303030"
WHITE = "#ffffff"
COLOUR_NAMES = {VIOLET: "42 violet", VIOLET_B: "42 purple", CHARCOAL: "charcoal", WHITE: "white"}

# pick_colours thresholds (ICON_SPEC §2).
DE_BACKGROUND = 25.0                # fill vs every spot's background
DE_PATTERN = 12.0                   # fill vs the pattern, where they overlap...
PATTERN_OVERLAP = 0.10              # ...by at least this share of the icon
INK_CONTRAST = 3.0                  # WCAG contrast white ink needs on the fill

# Timing (brand42 tokens ICON: glitch in / out frames).
FPS = 30.0
IN_FRAMES = 8
OUT_FRAMES = 6
ENTRANCES = ("none", "fade", "pop", "slide", "glitch", "grow")
# "grow" (a chip's own entrance or exit): its circle pops over the first
# GROW_POP of the way in, then the pill unfolds to its words, which show
# from GROW_WORDS of the unfold on; going, the mirror. Any other style grows
# as a pop (motion_for).
GROW_POP = 0.4
GROW_WORDS = 0.4

# Glitch (brand42 Glitch.tsx / tokens GLITCH), sized to the sprite: at the
# reels' 720 px reference a 0.16 badge is ~115 px, and the reels' largest
# slice jump (26 px) and channel split (5 px) are these shares of it.
GLITCH_STEP = 2
GLITCH_SLICES = 10
GLITCH_SHIFT = 0.22
GLITCH_SPLIT = 0.045
_RED = np.array([1.0, 0.12, 0.3])
_CYAN = np.array([0.1, 0.9, 1.0])

Glyph = Union[str, bytes, np.ndarray, tuple]      # d | PNG bytes | alpha | ("svg", d) / ("png", bytes)
Colour = Union[str, Sequence[float]]


# ---------------------------------------------------------------------------
# Rasterising
# ---------------------------------------------------------------------------

class _Flatten:
    """A fontTools pen that flattens a path into closed polygons (lists of
    points), curves split finely enough for `scale` output units per path
    unit (a segment is never much longer than half an output pixel)."""

    def __init__(self, scale: float):
        from fontTools.pens.basePen import BasePen

        outer = self

        class Pen(BasePen):
            def _moveTo(self, p):
                outer._start(p)

            def _lineTo(self, p):
                outer._cur.append(p)

            def _curveToOne(self, p1, p2, p3):
                outer._cubic(p1, p2, p3)

            def _qCurveToOne(self, p1, p2):
                p0 = outer._cur[-1]
                # A quadratic is the cubic with these controls.
                c1 = (p0[0] + 2 / 3 * (p1[0] - p0[0]), p0[1] + 2 / 3 * (p1[1] - p0[1]))
                c2 = (p2[0] + 2 / 3 * (p1[0] - p2[0]), p2[1] + 2 / 3 * (p1[1] - p2[1]))
                outer._cubic(c1, c2, p2)

            def _closePath(self):
                pass

            def _endPath(self):
                pass

        self.pen = Pen(None)
        self.scale = scale
        self.contours: list[list[tuple[float, float]]] = []
        self._cur: list[tuple[float, float]] = []

    def _start(self, p):
        self._cur = [p]
        self.contours.append(self._cur)

    def _cubic(self, p1, p2, p3):
        p0 = self._cur[-1]
        span = (math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)) * self.scale
        n = int(min(96, max(2, math.ceil(span / 2.0))))
        t = np.arange(1, n + 1, dtype=np.float64) / n
        mt = 1.0 - t
        a, b, c, d = mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3
        xs = a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0]
        ys = a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]
        self._cur.extend(zip(xs.tolist(), ys.tolist()))


def _contours(d: str, scale: float) -> list[np.ndarray]:
    """Path data -> closed polygons (float64 (n, 2) arrays, path units)."""
    from fontTools.svgLib.path import parse_path

    flat = _Flatten(scale)
    parse_path(d, flat.pen)
    return [np.asarray(c, dtype=np.float64) for c in flat.contours if len(c) >= 3]


def _winding_fill(contours: Iterable[np.ndarray], w: int, h: int) -> np.ndarray:
    """Nonzero-rule coverage (bool h x w) of closed polygons already in pixel
    units, sampled at pixel centres. Every edge adds its direction (+1 down,
    -1 up) to the rows it crosses at the first pixel centre right of the
    crossing; a running sum along each row is then the winding number."""
    x0s, y0s, x1s, y1s = [], [], [], []
    for c in contours:
        nxt = np.roll(c, -1, axis=0)
        x0s.append(c[:, 0]); y0s.append(c[:, 1]); x1s.append(nxt[:, 0]); y1s.append(nxt[:, 1])
    if not x0s:
        return np.zeros((h, w), bool)
    x0, y0 = np.concatenate(x0s), np.concatenate(y0s)
    x1, y1 = np.concatenate(x1s), np.concatenate(y1s)
    keep = y0 != y1
    x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]
    direction = np.where(y1 > y0, 1, -1).astype(np.int32)
    ylo, yhi = np.minimum(y0, y1), np.maximum(y0, y1)
    # Rows whose centre r + 0.5 lies in [ylo, yhi).
    r0 = np.clip(np.ceil(ylo - 0.5), 0, h).astype(np.int64)
    r1 = np.clip(np.ceil(yhi - 0.5), 0, h).astype(np.int64)
    counts = np.maximum(r1 - r0, 0)
    total = int(counts.sum())
    delta = np.zeros((h, w + 1), np.int32)
    if total:
        edge = np.repeat(np.arange(len(counts)), counts)
        start = np.repeat(np.cumsum(counts) - counts, counts)
        rows = r0[edge] + (np.arange(total) - start)
        yc = rows + 0.5
        t = (yc - y0[edge]) / (y1[edge] - y0[edge])
        xc = x0[edge] + t * (x1[edge] - x0[edge])
        cols = np.clip(np.floor(xc + 0.5), 0, w).astype(np.int64)
        np.add.at(delta, (rows, cols), direction[edge])
    return np.cumsum(delta, axis=1)[:, :w] != 0


def _supersampled(contours: list[np.ndarray], w: int, h: int, ss: int) -> np.ndarray:
    """Coverage (float32 h x w, 0..1): the winding fill at ss x ss samples a
    pixel, box-averaged."""
    big = _winding_fill([c * ss for c in contours], w * ss, h * ss)
    return big.reshape(h, ss, w, ss).mean(axis=(1, 3), dtype=np.float32)


@lru_cache(maxsize=512)
def _path_alpha_cached(d: str, px: int, ss: int) -> np.ndarray:
    scale = px / VIEWBOX
    contours = [c * scale for c in _contours(d, scale * ss)]
    out = _supersampled(contours, px, px, ss)
    out.flags.writeable = False
    return out


def path_alpha(d: str, px: int, *, ss: int = SUPERSAMPLE) -> np.ndarray:
    """Alpha (float32 px x px, 0..1, read-only) of SVG path data drawn on a
    256 x 256 box scaled to px x px, nonzero winding, `ss` x supersampled.
    Cached by (d, px, ss)."""
    if px < 1:
        raise ValueError("px must be >= 1")
    return _path_alpha_cached(str(d), int(px), int(ss))


def _png_alpha(data: bytes) -> np.ndarray:
    """A custom glyph PNG's shape: its alpha channel when it has one that is
    not flat, else 1 - luma (black ink on white)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            a = np.asarray(im.convert("RGBA"), dtype=np.float32)[..., 3] / 255.0
            if a.min() < 0.99:
                return a
        luma = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
    return 1.0 - luma


def _resize_alpha(a: np.ndarray, px: int) -> np.ndarray:
    from PIL import Image

    a = np.asarray(a, dtype=np.float32)
    if a.max() > 1.0:
        a = a / 255.0
    if a.shape == (px, px):
        return a
    img = Image.fromarray(np.clip(np.rint(a * 255.0), 0, 255).astype(np.uint8), "L")
    method = Image.BOX if img.width >= px else Image.LANCZOS
    return np.asarray(img.resize((px, px), method), dtype=np.float32) / 255.0


def glyph_alpha(glyph: Glyph, px: int) -> np.ndarray:
    """Any glyph form as a px x px alpha map (float32 0..1):
    SVG path data (str), ("svg", d), ("png", bytes), PNG bytes, or an alpha
    array (float 0..1 or uint8), resampled to px."""
    if isinstance(glyph, tuple) and len(glyph) == 2 and glyph[0] in ("svg", "png"):
        glyph = glyph[1]
    if isinstance(glyph, str):
        return np.array(path_alpha(glyph, px))
    if isinstance(glyph, (bytes, bytearray, memoryview)):
        return _resize_alpha(_png_alpha(bytes(glyph)), px)
    if isinstance(glyph, np.ndarray) and glyph.ndim == 2:
        return _resize_alpha(glyph, px)
    raise TypeError(f"not a glyph: {type(glyph).__name__}")


def _glyph_in_box(glyph: Glyph, L: int, inner: float) -> np.ndarray:
    """The glyph's alpha on an L x L canvas, drawn into the centred square of
    side `inner` (fractional offsets kept exactly for path data)."""
    off = (L - inner) / 2.0
    if isinstance(glyph, tuple) and len(glyph) == 2 and glyph[0] in ("svg", "png"):
        glyph = glyph[1]
    if isinstance(glyph, str):
        k = inner / VIEWBOX
        contours = [c * k + off for c in _contours(glyph, k * SUPERSAMPLE)]
        return _supersampled(contours, L, L, SUPERSAMPLE)
    n = max(1, int(round(inner)))
    a = glyph_alpha(glyph, n)
    out = np.zeros((L, L), np.float32)
    o = int(round(off))
    out[o:o + n, o:o + n] = a[: max(0, L - o), : max(0, L - o)]
    return out


def _glyph_at(glyph: Glyph, w: int, h: int, x: float, y: float, side: float) -> np.ndarray:
    """The glyph's alpha on a w x h canvas, drawn into the square of `side`
    with its top-left at (x, y) — fractional places kept exactly for path
    data, rounded for a picture glyph."""
    if isinstance(glyph, tuple) and len(glyph) == 2 and glyph[0] in ("svg", "png"):
        glyph = glyph[1]
    if isinstance(glyph, str):
        k = side / VIEWBOX
        contours = [c * k + np.array([x, y]) for c in _contours(glyph, k * SUPERSAMPLE)]
        return _supersampled(contours, w, h, SUPERSAMPLE)
    n = max(1, int(round(side)))
    a = glyph_alpha(glyph, n)
    out = np.zeros((h, w), np.float32)
    ox, oy = int(round(x)), int(round(y))
    x0, y0, x1, y1 = max(0, ox), max(0, oy), min(w, ox + n), min(h, oy + n)
    if x1 > x0 and y1 > y0:
        out[y0:y1, x0:x1] = a[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
    return out


# ---------------------------------------------------------------------------
# Sprites
# ---------------------------------------------------------------------------

def _rgb(colour: Colour) -> np.ndarray:
    """'#rrggbb' / (r, g, b) 0..255 -> float64 0..255."""
    if isinstance(colour, str):
        h = colour.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) != 6:
            raise ValueError(f"not a colour: {colour!r}")
        return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64)
    arr = np.asarray(colour, dtype=np.float64).reshape(-1)[:3]
    if arr.shape != (3,):
        raise ValueError(f"not a colour: {colour!r}")
    return arr


def hex_colour(colour: Colour) -> str:
    """Any colour form as lower-case '#rrggbb'."""
    r, g, b = (int(round(v)) for v in np.clip(_rgb(colour), 0, 255))
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgba(alpha: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    out = np.empty(alpha.shape + (4,), np.uint8)
    out[..., :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    out[..., 3] = np.clip(np.rint(alpha * 255.0), 0, 255).astype(np.uint8)
    return out


def badge_polygon(L: float) -> np.ndarray:
    """The badge outline (IconBadge.tsx), clockwise from the top-left."""
    cut = BADGE_CUT * L
    return np.array([(0.0, 0.0), (L - cut, 0.0), (L, cut), (L, L), (0.0, L)])


def badge_sprite(glyph: Glyph, L: int, fill: Colour, ink: Colour) -> np.ndarray:
    """The 42 badge (RGBA uint8, L x L): fill-coloured square, top-right
    corner cut at 45 degrees with legs of 0.24 L, the glyph centred at 0.6 L
    in the ink colour. `glyph` is any form glyph_alpha takes."""
    L = int(L)
    if L < 4:
        raise ValueError("badge side must be >= 4 px")
    body = _supersampled([badge_polygon(L)], L, L, SUPERSAMPLE)
    g = np.minimum(_glyph_in_box(glyph, L, BADGE_GLYPH * L), body)[..., None]
    rgb = _rgb(fill) * (1.0 - g) + _rgb(ink) * g
    return _rgba(body, rgb)


def glyph_sprite(glyph: Glyph, L: int, fill: Colour, ink: Colour) -> np.ndarray:
    """"Just the icon" (RGBA uint8, L x L): the glyph at 0.8 L in the FILL
    colour over a soft halo in the ink colour (35 % of a Gaussian of the
    glyph's own alpha).

    The glyph takes the fill because the fill is the colour pick_colours
    checked against the background: with no badge, the glyph itself is the
    mass that must stand out. (Measured on scene 815's bright wall: a white
    glyph with a violet halo all but vanished; the violet glyph read.)"""
    from PIL import Image, ImageFilter

    L = int(L)
    if L < 4:
        raise ValueError("glyph side must be >= 4 px")
    g = _glyph_in_box(glyph, L, GLYPH_ONLY * L)
    img = Image.fromarray(np.clip(np.rint(g * 255.0), 0, 255).astype(np.uint8), "L")
    blur = np.asarray(img.filter(ImageFilter.GaussianBlur(radius=max(0.5, HALO_SIGMA * L))),
                      dtype=np.float32) / 255.0
    halo = HALO_ALPHA * np.clip(blur * 2.0, 0.0, 1.0)
    alpha = g + halo * (1.0 - g)
    ga = g[..., None]
    ha = (halo * (1.0 - g))[..., None]
    with np.errstate(invalid="ignore", divide="ignore"):
        rgb = (_rgb(fill) * ga + _rgb(ink) * ha) / np.maximum(alpha[..., None], 1e-6)
    rgb = np.where(alpha[..., None] > 0, rgb, _rgb(ink))
    return _rgba(alpha, rgb)


@dataclass(frozen=True)
class Sprite:
    """A settled icon: `rgba` (straight RGBA uint8) holds the body — its
    top-left at (pad, pad), `w` x `h` px — and the soft shadow around it
    (pad 0 and rgba == the body for the badge and the bare glyph)."""
    rgba: np.ndarray
    pad: int
    w: int
    h: int


def shadow_pad(L: float) -> int:
    """The margin the soft shadow of an icon of size L needs on every side
    (3 sigma past the drop): the body stays centred, so a pop scales it
    about its own middle."""
    return int(math.ceil(3 * SHADOW_SIGMA * L + SHADOW_DY * L))


def _gaussian(a: np.ndarray, sigma: float, dy: float) -> np.ndarray:
    """`a` blurred by a Gaussian of `sigma` px and moved down by `dy` px
    (fractional: the vertical kernel is centred on dy), kernels cut at 3
    sigma, float64, a fixed order of sums — the same bytes everywhere."""
    H, W = a.shape
    a = a.astype(np.float64)
    r = int(math.ceil(3 * sigma))
    xs = np.arange(-r, r + 1)
    kx = np.exp(-(xs.astype(np.float64) ** 2) / (2 * sigma * sigma))
    kx /= kx.sum()
    ys = np.arange(int(math.floor(dy - 3 * sigma)), int(math.ceil(dy + 3 * sigma)) + 1)
    ky = np.exp(-((ys - dy) ** 2) / (2 * sigma * sigma))
    ky /= ky.sum()
    tmp = np.zeros_like(a)
    for w, o in zip(kx, xs):                    # tmp[:, x] += w * a[:, x - o]
        if o >= 0:
            tmp[:, o:] += w * a[:, :W - o]
        else:
            tmp[:, :W + o] += w * a[:, -o:]
    out = np.zeros_like(a)
    for w, o in zip(ky, ys):                    # out[y] += w * tmp[y - o]
        if o >= 0:
            out[o:, :] += w * tmp[:H - o, :]
        else:
            out[:H + o, :] += w * tmp[-o:, :]
    return out


def shadowed(rgba: np.ndarray, L: float) -> tuple[np.ndarray, int]:
    """A body (straight RGBA uint8) on a canvas grown by shadow_pad(L) on
    every side, over a soft black shadow of its own alpha: Gaussian sigma
    0.08 L, 40 %, dropped 0.05 L. Returns (rgba, pad)."""
    pad = shadow_pad(L)
    h, w = rgba.shape[:2]
    ab = np.zeros((h + 2 * pad, w + 2 * pad), np.float64)
    ab[pad:pad + h, pad:pad + w] = rgba[..., 3].astype(np.float64) / 255.0
    rgb = np.zeros((h + 2 * pad, w + 2 * pad, 3), np.float64)
    rgb[pad:pad + h, pad:pad + w] = rgba[..., :3].astype(np.float64)
    shadow = SHADOW_ALPHA * np.clip(_gaussian(ab, SHADOW_SIGMA * L, SHADOW_DY * L), 0.0, 1.0)
    alpha = ab + shadow * (1.0 - ab)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(alpha[..., None] > 0, rgb * ab[..., None] / np.maximum(alpha[..., None], 1e-9), 0.0)
    return _rgba(alpha, out), pad


def _disc(L: int) -> np.ndarray:
    """A disc of diameter L filling an L x L canvas (coverage 0..1)."""
    r = L / 2.0
    d = f"M0,{r}A{r},{r} 0 1 0 {L},{r}A{r},{r} 0 1 0 0,{r}Z"
    return _supersampled(_contours(d, SUPERSAMPLE), L, L, SUPERSAMPLE)


def _pill(w: int, h: int) -> np.ndarray:
    """A pill (a rectangle with half-circle ends) filling a w x h canvas."""
    r = h / 2.0
    d = f"M{r},0H{w - r}A{r},{r} 0 0 1 {w - r},{h}H{r}A{r},{r} 0 0 1 {r},0Z"
    return _supersampled(_contours(d, SUPERSAMPLE), w, h, SUPERSAMPLE)


def _tinted(body: np.ndarray, ink: np.ndarray, fill: Colour, ink_colour: Colour) -> np.ndarray:
    """The body's alpha in the fill, the ink (clipped to the body) in the
    ink colour: straight RGBA uint8."""
    g = np.minimum(ink, body)[..., None]
    return _rgba(body, _rgb(fill) * (1.0 - g) + _rgb(ink_colour) * g)


def circle_sprite(glyph: Glyph, L: int, fill: Colour, ink: Colour) -> Sprite:
    """Style E, "Circle": a disc of diameter L in the fill, the glyph (the
    bold weight, for a library icon) centred at 0.53 L in the ink, and the
    soft shadow."""
    L = int(L)
    if L < 4:
        raise ValueError("circle size must be >= 4 px")
    body = _disc(L)
    rgba, pad = shadowed(_tinted(body, _glyph_in_box(glyph, L, CIRCLE_GLYPH * L), fill, ink), L)
    return Sprite(rgba, pad, L, L)


@dataclass(frozen=True)
class ChipGeometry:
    """A chip's body: w x h px, the words as drawn (cut with "…" when they
    do not fit), their em in px, and whether they read right-to-left (then
    the glyph sits on the right)."""
    w: int
    h: int
    text: str
    em: float
    rtl: bool
    shrunk: bool                                  # the em is below CHIP_EM
    cut: bool                                     # the words were cut


def _chip_fixed(h: int) -> float:
    return (CHIP_PAD_GLYPH + CHIP_GLYPH + CHIP_GAP + CHIP_PAD_WORDS) * h


def _chip_em(units: float, h: int, max_w: Optional[float]) -> float:
    """The words' em: CHIP_EM h, or smaller (down to CHIP_EM_MIN h) so the
    chip fits max_w."""
    em = CHIP_EM * h
    if max_w is None or units <= 0:
        return em
    return min(em, max(CHIP_EM_MIN * h, (float(max_w) - _chip_fixed(h)) / units))


def chip_words(text: Any) -> str:
    """A chip's words as they are laid out: spaces collapsed, trimmed."""
    return " ".join(str(text or "").split())


def chip_geometry(text: str, h: int, font: str, *, max_w: Optional[float] = None) -> ChipGeometry:
    """How wide a chip of height h is with these words in this label font
    (label_font key), no wider than max_w px when given: the words' em
    shrinks from 0.37 h down to 0.30 h, then they are cut with "…". Pure,
    and its own fixed point (`.text` measures the same w, em and words
    again), so a planner that stores `.text` and boxes `w` agrees with the
    Stamper, which measures that text again."""
    from . import label_font

    h = int(h)
    words = chip_words(text)
    fixed = _chip_fixed(h)
    units = label_font.units(words, font)
    em = _chip_em(units, h, max_w)
    cut = False
    if max_w is not None and fixed + units * em > float(max_w) + 1e-6:
        # Too wide even at the smallest em: the longest start that fits
        # with "…" (never ending on a space or before one of its marks).
        room = float(max_w) + 1e-6
        best = ELLIPSIS
        for n in range(len(words) - 1, 0, -1):
            if unicodedata.combining(words[n]):
                continue
            cand = words[:n].rstrip() + ELLIPSIS
            if fixed + label_font.units(cand, font) * CHIP_EM_MIN * h <= room:
                best = cand
                break
        words, cut = best, True
        units = label_font.units(words, font)
        em = _chip_em(units, h, max_w)
    w = int(round(fixed + units * em))
    return ChipGeometry(w=max(h, w), h=h, text=words, em=em, rtl=label_font.shape(words, font)[1],
                        shrunk=em < CHIP_EM * h - 1e-9, cut=cut)


def chip_grown(glyph: Glyph, h: int, text: str, font: str, fill: Colour, ink: Colour, *,
               max_w: Optional[float] = None, unfold: float = 1.0, words: float = 1.0) -> Sprite:
    """A chip on its way in or out ("grow"): the pill `unfold` of the way
    from a disc of its height (at its glyph's end) to its full width, the
    glyph moving from the disc's middle to its place in the chip, its words
    at `words` of their strength and never past the pill. The canvas, the
    pad and the shadow are chip_sprite's (only the body's width moves), so
    each frame rests where the settled chip does."""
    from . import label_font

    h = int(h)
    if h < 4:
        raise ValueError("chip height must be >= 4 px")
    unfold, words = _clamp01(float(unfold)), _clamp01(float(words))
    geo = chip_geometry(text, h, font, max_w=max_w)
    w = geo.w
    width = h + (w - h) * unfold
    x0 = w - width if geo.rtl else 0.0
    r = h / 2.0
    d = (f"M{x0 + r},0H{x0 + width - r}A{r},{r} 0 0 1 {x0 + width - r},{h}"
         f"H{x0 + r}A{r},{r} 0 0 1 {x0 + r},0Z")
    body = _supersampled(_contours(d, SUPERSAMPLE), w, h, SUPERSAMPLE)
    side = CHIP_GLYPH * h
    gy = (h - side) / 2.0
    if geo.rtl:
        g_full, g_disc, tx = w - CHIP_PAD_GLYPH * h - side, w - h + (h - side) / 2.0, CHIP_PAD_WORDS * h
    else:
        g_full, g_disc, tx = CHIP_PAD_GLYPH * h, (h - side) / 2.0, CHIP_PAD_GLYPH * h + side + CHIP_GAP * h
    ink_a = _glyph_at(glyph, w, h, g_disc + (g_full - g_disc) * unfold, gy, side)
    if geo.text and words > 0:
        tp = label_font.text_path(geo.text, font, geo.em)
        baseline = h / 2.0 + tp.cap / 2.0
        marks = [c + np.array([tx, baseline]) for c in _contours(tp.d, SUPERSAMPLE)] if tp.d else []
        ink_a = np.maximum(ink_a, _supersampled(marks, w, h, SUPERSAMPLE) * words)
    rgba, pad = shadowed(_tinted(body, ink_a, fill, ink), h / CHIP_H)
    return Sprite(rgba, pad, w, h)


def chip_sprite(glyph: Glyph, h: int, text: str, font: str, fill: Colour, ink: Colour, *,
                max_w: Optional[float] = None) -> Sprite:
    """Style H, "Circle + words": a pill of height h in the fill; in the ink
    the glyph (0.70 h, 0.24 h from the edge) and then the words (0.15 h
    after it, em 0.37 h, cap height centred on the pill's middle, 0.28 h
    from the far edge); mirrored when the words read right-to-left; the soft
    shadow of an icon of size h / 0.74. Width: chip_geometry."""
    from . import label_font

    h = int(h)
    if h < 4:
        raise ValueError("chip height must be >= 4 px")
    geo = chip_geometry(text, h, font, max_w=max_w)
    w = geo.w
    body = _pill(w, h)
    side = CHIP_GLYPH * h
    gy = (h - side) / 2.0
    if geo.rtl:
        gx, tx = w - CHIP_PAD_GLYPH * h - side, CHIP_PAD_WORDS * h
    else:
        gx, tx = CHIP_PAD_GLYPH * h, CHIP_PAD_GLYPH * h + side + CHIP_GAP * h
    ink_a = _glyph_at(glyph, w, h, gx, gy, side)
    if geo.text:
        tp = label_font.text_path(geo.text, font, geo.em)
        baseline = h / 2.0 + tp.cap / 2.0
        words = [c + np.array([tx, baseline]) for c in _contours(tp.d, SUPERSAMPLE)] if tp.d else []
        ink_a = np.maximum(ink_a, _supersampled(words, w, h, SUPERSAMPLE))
    rgba, pad = shadowed(_tinted(body, ink_a, fill, ink), h / CHIP_H)
    return Sprite(rgba, pad, w, h)


def make_sprite(style: str, glyph: Glyph, size: int, fill: Colour, ink: Colour, *,
                text: Optional[str] = None, font: Optional[str] = None, look_v: int = 0,
                max_w: Optional[float] = None) -> Sprite:
    """Any style as a Sprite. `size` is L (the badge, the bare glyph, the
    circle) or the chip's height h. The circle and the chip are look_v 1
    (the only recipe so far; a later one is refused, never guessed); the
    chip needs its words and label font. Anything else is the badge, as
    always."""
    if style in ("circle", "chip"):
        if int(look_v or LOOK_V) > LOOK_V:
            raise ValueError(f"a {style} of look_v {look_v} is drawn by a newer version")
        if style == "circle":
            return circle_sprite(glyph, size, fill, ink)
        if not font:
            raise ValueError("a chip needs its label font")
        return chip_sprite(glyph, size, text or "", font, fill, ink, max_w=max_w)
    rgba = (glyph_sprite if style == "glyph" else badge_sprite)(glyph, size, fill, ink)
    return Sprite(rgba, 0, int(size), int(size))


def sprite(style: str, glyph: Glyph, L: int, fill: Colour, ink: Colour, **kw: Any) -> np.ndarray:
    """The pixels of make_sprite: badge_sprite or glyph_sprite by `style`
    ("badge" | "glyph", byte-identical to before), or the circle / chip
    with its shadow."""
    return make_sprite(style, glyph, L, fill, ink, **kw).rgba


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def srgb_to_lab(rgb255: Any) -> np.ndarray:
    """CIELAB (D65, L 0..100) of sRGB 0..255 values, shape (..., 3)."""
    c = np.asarray(rgb255, dtype=np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = (c @ m.T) / np.array([0.95047, 1.0, 1.08883])
    e, k = 216 / 24389, 24389 / 27
    f = np.where(xyz > e, np.cbrt(xyz), (k * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def delta_e2000(lab1: Any, lab2: Any) -> np.ndarray:
    """CIEDE2000 colour difference (kL = kC = kH = 1), broadcasting over
    (..., 3) Lab arrays (Sharma, Wu & Dalal 2005)."""
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cm = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cm ** 7 / (Cm ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0.0, dh)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lpm = (L1 + L2) / 2
    Cpm = (C1p + C2p) / 2
    hsum = h1p + h2p
    hpm = np.where(np.abs(h1p - h2p) > 180,
                   np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2), hsum / 2)
    hpm = np.where(C1p * C2p == 0, hsum, hpm)
    T = (1 - 0.17 * np.cos(np.radians(hpm - 30)) + 0.24 * np.cos(np.radians(2 * hpm))
         + 0.32 * np.cos(np.radians(3 * hpm + 6)) - 0.20 * np.cos(np.radians(4 * hpm - 63)))
    dtheta = 30 * np.exp(-(((hpm - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cpm ** 7 / (Cpm ** 7 + 25.0 ** 7))
    Sl = 1 + (0.015 * (Lpm - 50) ** 2) / np.sqrt(20 + (Lpm - 50) ** 2)
    Sc = 1 + 0.045 * Cpm
    Sh = 1 + 0.015 * Cpm * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
                   + Rt * (dCp / Sc) * (dHp / Sh))


def relative_luminance(colour: Colour) -> float:
    """WCAG 2 relative luminance of an sRGB colour."""
    c = _rgb(colour) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return float(0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2])


def contrast_ratio(a: Colour, b: Colour) -> float:
    """WCAG 2 contrast ratio (1..21)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _labs(samples: Any, space: str) -> np.ndarray:
    """Background samples as an (n, 3) Lab array. Hex strings are sRGB;
    tuples / arrays are read in `space` ("rgb" 0..255 or "lab"); a mapping
    {"lab": ...} or {"rgb": ...} says which itself."""
    if samples is None:
        return np.zeros((0, 3))
    if isinstance(samples, (str, Mapping)) or (
            not isinstance(samples, np.ndarray) and len(samples) == 3
            and all(isinstance(v, (int, float, np.number)) for v in samples)):
        samples = [samples]                              # one bare colour
    if isinstance(samples, np.ndarray) and samples.ndim == 1 and samples.shape[0] == 3:
        samples = samples[None]
    if isinstance(samples, np.ndarray) and samples.ndim == 2 and samples.shape[-1] == 3:
        arr = samples.astype(np.float64)
        return arr if space == "lab" else srgb_to_lab(arr)
    out = []
    for s in samples:
        if isinstance(s, Mapping):
            if "lab" in s:
                out.append(np.asarray(s["lab"], dtype=np.float64))
            else:
                out.append(srgb_to_lab(_rgb(s["rgb"])))
        elif isinstance(s, str):
            out.append(srgb_to_lab(_rgb(s)))
        else:
            v = np.asarray(s, dtype=np.float64).reshape(-1)[:3]
            out.append(v if space == "lab" else srgb_to_lab(v))
    return np.asarray(out, dtype=np.float64).reshape(-1, 3)


def _overlaps(pattern_overlap: Any, n: int) -> list[float]:
    if pattern_overlap is None:
        return [0.0] * max(1, n)
    if isinstance(pattern_overlap, (int, float)):
        return [float(pattern_overlap)] * max(1, n)
    return [float(v) for v in pattern_overlap]


def _ink_for(fill: str, pattern_colour: Optional[str]) -> str:
    """White ink, or charcoal where white would not read (WCAG < 3). On a
    white fill the ink is the pattern's colour (42 violet without one), or
    charcoal if that is too light to read on white."""
    if fill == WHITE:
        ink = pattern_colour or VIOLET
        return ink if contrast_ratio(ink, WHITE) >= INK_CONTRAST else CHARCOAL
    return WHITE if contrast_ratio(WHITE, fill) >= INK_CONTRAST else CHARCOAL


def ink_for(fill: Colour, pattern_colour: Optional[Colour] = None) -> str:
    """The ink (glyph and words) for a fill, as pick_colours chooses it:
    white, else charcoal where white would not read; on a white fill the
    pattern's colour (42 violet without one)."""
    return _ink_for(hex_colour(fill), hex_colour(pattern_colour) if pattern_colour else None)


def _name(colour: str, pattern_colour: Optional[str]) -> str:
    if pattern_colour and colour == pattern_colour:
        return "the pattern's colour" if colour not in COLOUR_NAMES else COLOUR_NAMES[colour]
    return COLOUR_NAMES.get(colour, colour)


def _stands_out(c: str, labs: np.ndarray, pc: Optional[str], on_pattern: bool) -> tuple[float, Optional[str]]:
    """(the worst CIEDE2000 difference, where `c` blends in: "pattern",
    "room" or None) against the backgrounds `labs` and, where it covers the
    icon, the pattern `pc`."""
    lab = srgb_to_lab(_rgb(c))
    de_bg = float(delta_e2000(labs, lab).min()) if len(labs) else 100.0
    de = de_bg
    if on_pattern:
        de_p = float(delta_e2000(srgb_to_lab(_rgb(pc)), lab))
        de = min(de, de_p * (DE_BACKGROUND / DE_PATTERN))
        if de_p < DE_PATTERN:
            return de, "pattern"
    if de_bg < DE_BACKGROUND:
        return de, "room"
    return de, None


def hard_to_see(colour: Colour, slot_bgs: Any, pattern_colour: Optional[Colour] = None,
                pattern_overlap: Any = 0.0, *, space: str = "rgb") -> Optional[str]:
    """Where one icon's own colour would blend in: "pattern", "room" (the
    background) or None — pick_colours' own test, for one icon's samples."""
    pc = hex_colour(pattern_colour) if pattern_colour is not None else None
    labs = _labs(slot_bgs, space)
    over = _overlaps(pattern_overlap, len(labs))
    on_pattern = pc is not None and any(o >= PATTERN_OVERLAP for o in over)
    return _stands_out(hex_colour(colour), labs, pc, on_pattern)[1]


def pick_colours(
    slot_bgs: Any,
    pattern_colour: Optional[Colour] = None,
    pattern_overlap: Any = 0.0,
    *,
    space: str = "rgb",
    colour: str = "auto",
) -> dict:
    """ONE (fill, ink) for every icon in a scene.

    slot_bgs         the background under the icons: one sample per spot and
                     moment (hex, (r, g, b) 0..255, or Lab with space="lab",
                     or {"lab": ...} / {"rgb": ...}).
    pattern_colour   the first pattern's resolved colour (None = no pattern).
    pattern_overlap  how much of the icon box the pattern's ink covers: one
                     share for all samples, or one per sample.
    colour           "auto", or a hex the user picked (kept, with a warning
                     when it does not stand out).

    Candidates, in order: the pattern's colour -> 42 violet -> 42 purple ->
    charcoal -> white. The first that is CIEDE2000 >= 25 from every
    background, and >= 12 from the pattern wherever the pattern covers >= 10 %
    of the icon, wins. When none passes, the one with the largest worst-case
    difference is used and `warning` says so. Ink: white, else charcoal
    (WCAG < 3); on a white fill the pattern's colour.

    Returns {"fill": "#rrggbb", "ink": "#rrggbb", "why": str,
             "warning": str | None, "min_de": float}."""
    pc = hex_colour(pattern_colour) if pattern_colour is not None else None
    labs = _labs(slot_bgs, space)
    over = _overlaps(pattern_overlap, len(labs))
    on_pattern = pc is not None and any(o >= PATTERN_OVERLAP for o in over)

    def check(c: str) -> tuple[float, Optional[str]]:
        return _stands_out(c, labs, pc, on_pattern)

    if colour and colour != "auto":
        fill = hex_colour(colour)
        de, fails = check(fill)
        warning = None
        if fails == "pattern":
            warning = f"{_name(fill, pc).capitalize()} is hard to see on the pattern"
        elif fails == "room":
            warning = f"{_name(fill, pc).capitalize()} is hard to see on the background"
        return {"fill": fill, "ink": _ink_for(fill, pc), "why": "Your colour",
                "warning": warning, "min_de": round(de, 1)}

    candidates: list[str] = []
    for c in ([pc] if pc else []) + [VIOLET, VIOLET_B, CHARCOAL, WHITE]:
        if c not in candidates:
            candidates.append(c)
    rejected: list[tuple[str, str]] = []
    scored: list[tuple[float, str]] = []
    for c in candidates:
        de, fails = check(c)
        scored.append((de, c))
        if fails is None:
            name = _name(c, pc)
            if not rejected:
                why = f"{name.capitalize()} stands out on every spot"
            else:
                first, where = rejected[0]
                blend = "the pattern" if where == "pattern" else "the background"
                why = f"{name.capitalize()}: {_name(first, pc)} would blend into {blend}"
            return {"fill": c, "ink": _ink_for(c, pc), "why": why, "warning": None,
                    "min_de": round(de, 1)}
        rejected.append((c, fails))
    de, best = max(scored, key=lambda s: (s[0], -candidates.index(s[1])))
    return {"fill": best, "ink": _ink_for(best, pc),
            "why": f"{_name(best, pc).capitalize()} is the clearest here",
            "warning": "No colour stands out everywhere", "min_de": round(de, 1)}


# ---------------------------------------------------------------------------
# Entrances
# ---------------------------------------------------------------------------

def _hash01(*key: object) -> float:
    """Deterministic 0..1 from a key (brand_overlay._hash01)."""
    h = hashlib.blake2b("|".join(map(str, key)).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2 ** 64


def _in_out_cubic(t: float) -> float:
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def _out_back(t: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _out_expo(t: float) -> float:
    """easeOutExpo renormalised so e(0) = 0 and e(1) = 1 exactly (easing.ts)."""
    return (1 - 2 ** (-10 * t)) / (1 - 2 ** -10)


def _clamp01(t: float) -> float:
    return 0.0 if t < 0 else 1.0 if t > 1 else t


@dataclass(frozen=True)
class FxFrame:
    """One frame of an icon: `rgba` pasted with its top-left at the icon's
    box corner + (dx, dy) px."""
    rgba: np.ndarray
    dx: int
    dy: int


def _scaled(rgba: np.ndarray, s: float, alpha: float) -> FxFrame:
    """The sprite scaled by `s` about its centre (canvas grown to fit an
    overshoot), alpha multiplied by `alpha`."""
    from PIL import Image

    h, w = rgba.shape[:2]
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    if (nw, nh) != (w, h):
        # Premultiply so the resampling does not pull in the transparent
        # pixels' colour.
        pre = rgba.astype(np.float32)
        pre[..., :3] *= pre[..., 3:4] / 255.0
        big = Image.fromarray(np.clip(np.rint(pre), 0, 255).astype(np.uint8), "RGBA")
        res = np.asarray(big.resize((nw, nh), Image.BICUBIC), dtype=np.float32)
        a = np.clip(res[..., 3:4], 0, 255)
        with np.errstate(invalid="ignore", divide="ignore"):
            rgb = np.where(a > 0, res[..., :3] * 255.0 / np.maximum(a, 1e-6), 0)
        out = np.concatenate([np.clip(rgb, 0, 255), a], axis=-1)
    else:
        out = rgba.astype(np.float32)
    out[..., 3] *= alpha
    out = np.clip(np.rint(out), 0, 255).astype(np.uint8)
    return FxFrame(out, (w - nw) // 2, (h - nh) // 2)


def _faded(rgba: np.ndarray, alpha: float) -> FxFrame:
    out = rgba.copy()
    out[..., 3] = np.clip(np.rint(out[..., 3].astype(np.float32) * alpha), 0, 255).astype(np.uint8)
    return FxFrame(out, 0, 0)


def _glitched(rgba: np.ndarray, frame: int, amount: float, reveal: float, seed: str,
              tear: Optional[int] = None) -> Optional[FxFrame]:
    """The brand42 GlitchWrap on one sprite: horizontal slices (jittered
    bounds, new every GLITCH_STEP frames) jump sideways, a few tear 2.6x
    further; red / cyan copies split under it; slices hand over one by one
    (`reveal`); a strong glitch flickers the whole icon off now and then.
    `tear`: the size the jumps are shares of (the body's shorter side; the
    sprite's longer side when not given, which is the same for a square)."""
    if reveal <= 0:
        return None
    h, w = rgba.shape[:2]
    if amount <= 0.001 and reveal >= 1:
        return FxFrame(rgba, 0, 0)
    step = frame // GLITCH_STEP
    if amount > 0.5 and _hash01(seed, "flicker", step) < 0.16 * amount:
        return None
    L = int(tear) if tear else max(h, w)
    shift, split = GLITCH_SHIFT * L, GLITCH_SPLIT * L
    split1 = split * amount * (0.6 + 0.4 * _hash01(seed, "split", step))
    pad = int(math.ceil(2.6 * shift + split + 2))
    W = w + 2 * pad
    bounds = [0]
    for i in range(1, GLITCH_SLICES):
        jit = (_hash01(seed, "b", i, step) - 0.5) * 0.7
        bounds.append(int(round((i + jit) / GLITCH_SLICES * h)))
    bounds.append(h)
    src = rgba.astype(np.float32) / 255.0
    acc = np.zeros((h, W, 4), np.float32)            # premultiplied

    def over(dst: np.ndarray, rgb: np.ndarray, a: np.ndarray, x: int) -> None:
        x0, x1 = max(0, x), min(W, x + w)
        if x1 <= x0:
            return
        sx0 = x0 - x
        sl = slice(sx0, sx0 + (x1 - x0))
        aa = a[:, sl, None]
        dst[:, x0:x1, :3] = rgb[:, sl] * aa + dst[:, x0:x1, :3] * (1 - aa)
        dst[:, x0:x1, 3:] = aa + dst[:, x0:x1, 3:] * (1 - aa)

    for i in range(GLITCH_SLICES):
        y0, y1 = bounds[i], bounds[i + 1]
        if y1 <= y0 or _hash01(seed, "rank", i) >= reveal:
            continue
        tear = 2.6 if _hash01(seed, "tear", i, step) < 0.18 else 1.0
        dx = int(round((_hash01(seed, "dx", i, step) - 0.5) * 2 * shift * amount * tear))
        piece = src[y0:y1]
        a = piece[..., 3]
        band = acc[y0:y1]
        if split1 > 0.5:
            s = int(round(split1))
            ca, ra = a * 0.85, a * 0.85
            over(band, np.broadcast_to(_CYAN, piece.shape[:2] + (3,)), ca, pad + dx - s)
            over(band, np.broadcast_to(_RED, piece.shape[:2] + (3,)), ra, pad + dx + s)
        over(band, piece[..., :3], a, pad + dx)
    a = acc[..., 3:]
    with np.errstate(invalid="ignore", divide="ignore"):
        rgb = np.where(a > 0, acc[..., :3] / np.maximum(a, 1e-6), 0)
    out = np.concatenate([rgb, a], axis=-1)
    return FxFrame(np.clip(np.rint(out * 255.0), 0, 255).astype(np.uint8), -pad, 0)


def nearest_edge(box_px: tuple[int, int, int, int], frame_size: tuple[int, int]) -> tuple[str, int]:
    """(edge, travel px) for a slide: the frame edge closest to the box
    (x, y, w, h) and how far the box must move to be fully outside it."""
    x, y, w, h = box_px
    W, H = frame_size
    gaps = {"left": x + w, "right": W - x, "top": y + h, "bottom": H - y}
    near = {"left": x, "right": W - (x + w), "top": y, "bottom": H - (y + h)}
    edge = min(("left", "right", "top", "bottom"), key=lambda e: (near[e], e))
    return edge, int(gaps[edge])


@dataclass(frozen=True)
class FxProgress:
    """Where one frame is in an icon's life, as numbers (fx draws them; the
    studio's moving preview ports them frame for frame).

    kind     the motion this frame is drawn with: the entrance while it comes
             in (and holds), the exit while it goes
    phase    "in" | "hold" | "out"
    alpha    fade / pop / grow's circle: its opacity
    scale    pop / grow's circle: its size, about its centre
    slide    slide: the share of the travel still to go (0 = at rest)
    amount, reveal   glitch: how torn it is, and the share of its slices in
    unfold, words    grow: how far the pill is open from its circle (0 = the
             circle alone), and the words' strength"""
    kind: str
    phase: str
    alpha: float = 1.0
    scale: float = 1.0
    slide: float = 0.0
    amount: float = 0.0
    reveal: float = 1.0
    unfold: float = 1.0
    words: float = 1.0


def _motion(kind: Optional[str]) -> str:
    kind = "glitch" if kind in (None, "", "auto") else kind
    if kind not in ENTRANCES:
        raise ValueError(f"unknown entrance {kind!r}")
    return kind


def motion_for(style: str, kind: Optional[str]) -> Optional[str]:
    """The motion an icon of `style` is drawn with: "grow" is a chip's (its
    circle pops, then the pill unfolds); any other style grows as a pop."""
    return "pop" if kind == "grow" and style != "chip" else kind


def fx_progress(entrance: str, f: int, *, end: int, fin: int, fout: int,
                exit: Optional[str] = None) -> Optional[FxProgress]:
    """Frame `f` (since the icon's first frame) of an icon on screen for
    `end` frames, coming in over `fin` and going over `fout`: None when it
    is not on screen, else where it is (FxProgress). `exit`: the motion it
    leaves with (None: the entrance mirrored, as every version before
    per-icon exits). Pure: the numbers fx() draws."""
    entrance = _motion(entrance)
    leave = entrance if exit is None else _motion(exit)
    if f < 0 or f >= end:
        return None
    phase = "in" if f < fin else "out" if f >= end - fout else "hold"
    kind = leave if phase == "out" else entrance
    if kind == "none" or phase == "hold":
        return FxProgress(kind, phase)
    if kind == "glitch":
        t = (f + 1) / fin if phase == "in" else (end - f) / fout
        amount, reveal = 1 - _in_out_cubic(_clamp01(t)), _clamp01(t / 0.6)
        if t >= 1:
            amount, reveal = 0.0, 1.0
        return FxProgress(kind, phase, amount=amount, reveal=reveal)
    # Progress p: 0 -> 1 over the entrance, 1 -> 0 over the exit.
    leaving = phase == "out"
    p = _clamp01((end - f - 1) / fout if leaving else (f + 1) / fin)
    if kind == "grow":
        # The circle pops, then the pill unfolds and its words come in.
        if p <= GROW_POP:
            q = p / GROW_POP
            return FxProgress(kind, phase, alpha=_clamp01(q * 2.0),
                              scale=0.7 + 0.3 * (_out_back(q) if not leaving else q), unfold=0.0, words=0.0)
        u = (p - GROW_POP) / (1.0 - GROW_POP)
        return FxProgress(kind, phase, unfold=_out_expo(u) if not leaving else u,
                          words=_clamp01((u - GROW_WORDS) / (1.0 - GROW_WORDS)))
    if kind == "fade":
        return FxProgress(kind, phase, alpha=p)
    if kind == "pop":
        return FxProgress(kind, phase, alpha=_clamp01(p * 2.0),
                          scale=0.7 + 0.3 * (_out_back(p) if not leaving else p))
    e = _out_expo(p) if not leaving else p ** 3
    return FxProgress(kind, phase, slide=1.0 - e)


def fx(
    entrance: str,
    t_rel: float,
    sprite: np.ndarray,
    seed: str,
    *,
    span: float,
    in_sec: float = IN_FRAMES / FPS,
    out_sec: float = OUT_FRAMES / FPS,
    fps: float = FPS,
    edge: str = "left",
    travel: int = 0,
    tear: Optional[int] = None,
    exit: Optional[str] = None,
) -> Optional[FxFrame]:
    """One frame of an icon's life.

    entrance   "glitch" | "pop" | "fade" | "slide" | "none" ("auto" = glitch)
    t_rel      seconds since the icon's from_sec (the entrance's start)
    sprite     the settled sprite (RGBA uint8)
    seed       its glitch seed (the same seed = the same frames)
    span       to_sec - from_sec (on screen until then, exclusive)
    in_sec     entrance length (settle_sec - from_sec); out_sec exit length
    edge, travel   for "slide": the edge it comes from and how far (px)
    tear       for "glitch": the body's shorter side, which sizes the tear
               (default the sprite's longer side: the same for a square
               with no shadow, so a wide chip does not tear too far)
    exit       the motion it leaves with (None: the entrance mirrored)

    Times are quantised to frames (round(t * fps)), so a frame's look never
    depends on float noise. Returns None when nothing shows this frame
    (before the start, from the end, or a glitch flicker), else an FxFrame.

    In / out:  glitch  8 f in / 6 f out, the Glitch.tsx grammar
               pop     scale 0.7 -> 1 easeOutBack + alpha; out mirrors it
               fade    alpha; slide  from the nearest edge, easeOutExpo
               grow    a chip's: its circle pops, then the pill unfolds
                       (Stamper.frame_of draws it; a lone sprite pops)
               none    on for the whole span (its whole way in or out)"""
    f = int(round(t_rel * fps))
    end = int(round(span * fps))
    fin = max(1, int(round(in_sec * fps)))
    fout = max(1, int(round(out_sec * fps)))
    pr = fx_progress(entrance, f, end=end, fin=fin, fout=fout, exit=exit)
    if pr is None:
        return None
    if pr.kind == "glitch" and pr.phase != "hold":
        return _glitched(sprite, f, pr.amount, pr.reveal, seed, tear)
    if pr.kind in ("none", "glitch") or pr.phase == "hold":
        return FxFrame(sprite, 0, 0)
    if pr.kind == "fade":
        return _faded(sprite, pr.alpha)
    if pr.kind == "pop" or (pr.kind == "grow" and pr.unfold <= 0.0):
        return _scaled(sprite, pr.scale, pr.alpha)
    if pr.kind == "grow":
        # A sprite alone can't unfold (the Stamper draws a chip's own):
        # whole once its circle has popped.
        return FxFrame(sprite, 0, 0)
    off = int(round(pr.slide * travel))
    dx, dy = {"left": (-off, 0), "right": (off, 0), "top": (0, -off), "bottom": (0, off)}[edge]
    return FxFrame(sprite, dx, dy)


# ---------------------------------------------------------------------------
# Stamping
# ---------------------------------------------------------------------------

_SPRITES: dict[tuple, Sprite] = {}
_SPRITES_MAX = 256
_SPRITES_LOCK = threading.Lock()        # restores run in worker threads


def _sprite_cached(key: tuple, glyph: Glyph) -> Sprite:
    """Settled sprites by (style, glyph key, weight, size, fill, ink, text,
    font, look_v, max_w), read-only."""
    with _SPRITES_LOCK:
        got = _SPRITES.get(key)
    if got is None:
        style, _gkey, _weight, size, fill, ink, text, font, look_v, max_w = key
        got = make_sprite(style, glyph, size, fill, ink, text=text, font=font, look_v=look_v, max_w=max_w)
        got.rgba.flags.writeable = False
        with _SPRITES_LOCK:
            if len(_SPRITES) >= _SPRITES_MAX:
                _SPRITES.pop(next(iter(_SPRITES)))
            got = _SPRITES.setdefault(key, got)
    return got


_GROWN: dict[tuple, np.ndarray] = {}
_GROWN_MAX = 64


def _grown_cached(it: "_Item", unfold: float, words: float) -> np.ndarray:
    """chip_grown's pixels for a chip item, by its sprite key and the two
    numbers (a grow is the same 14 frames every time it plays), read-only."""
    key = (it.key(), round(float(unfold), 9), round(float(words), 9))
    with _SPRITES_LOCK:
        got = _GROWN.get(key)
    if got is None:
        got = chip_grown(it.glyph, it.size, it.text or "", it.font, it.fill, it.ink, max_w=it.max_w,
                         unfold=unfold, words=words).rgba
        got.flags.writeable = False
        with _SPRITES_LOCK:
            if len(_GROWN) >= _GROWN_MAX:
                _GROWN.pop(next(iter(_GROWN)))
            got = _GROWN.setdefault(key, got)
    return got


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _plain(v: Any) -> Any:
    if hasattr(v, "model_dump"):
        return v.model_dump()
    return v


def library_glyph(source: str, gid: str, weight: str = "fill") -> Glyph:
    """Default glyph lookup: library ids from the baked Phosphor catalogue,
    in `weight` ("fill" | "bold"; a bold shape the bold file lacks is its
    fill shape). Custom (AI-drawn) glyphs live in the database, so a
    Stamper that has any must be given them (icon_catalog.glyphs_for(items))."""
    if source == "library":
        from . import icon_catalog

        d = icon_catalog.library_d(gid, weight)
        if d:
            return d
    raise KeyError(f"no glyph for {source}:{gid} (fetch custom glyphs first)")


@dataclass
class _Item:
    gkey: str
    glyph: Glyph
    style: str
    fill: str
    ink: str
    box: tuple[int, int, int, int]            # the body: x, y, w, h in px
    f_from: int
    f_settle: int
    f_to: int
    entrance: str
    seed: str
    edge: str
    travel: int
    weight: str = "fill"
    text: Optional[str] = None                # a chip's words, as given
    font: Optional[str] = None                # a chip's label font key
    layer: str = "behind"                     # "behind" | "front" (the speaker)
    look_v: int = 0
    size: int = 0                             # the sprite's size: L, or a chip's h
    max_w: Optional[float] = None             # a chip's widest body, px
    pad: int = 0                              # the shadow's margin around the body
    exit: Optional[str] = None                # how it leaves (None: the entrance mirrored)

    def key(self) -> tuple:
        return (self.style, self.gkey, self.weight, self.size, self.fill, self.ink, self.text,
                self.font, self.look_v, self.max_w)


def _pad_of(style: str, size: int) -> int:
    if style == "circle":
        return shadow_pad(size)
    if style == "chip":
        return shadow_pad(size / CHIP_H)
    return 0


class Stamper:
    """Composites resolved icons onto frames of ONE chunk.

    resolved     IconResolved dicts (or models): glyph {source, id}, from_sec,
                 settle_sec, to_sec (scene seconds), box {x, y, w, h} (frame
                 fractions: where the body sits), fill, ink, fx {entrance,
                 seed, exit?}, style; and for the circle and the chip weight, text,
                 font, layer, look_v (their defaults draw what every earlier
                 version drew).
    fps          the chunk's frame rate.
    offset_sec   where the chunk starts in the scene: frame i of the chunk is
                 scene time offset_sec + i / fps.
    frame_size   (W, H) of the frames `under` gets.
    glyphs       {glyph id: glyph} for ids the default lookup cannot read
                 (custom glyphs); library ids load from the catalogue.

    The body: a badge / bare glyph / circle is the square of side
    min(w, h) centred in the box (as always); a chip is the box's height
    tall and chip_geometry wide, centred in the box. A circle or a chip's
    shadow reaches past its body (Sprite.pad).

    drawn(t, layer=) the pieces on screen at chunk-local t: [(rgba, x, y)],
    straight RGBA with its top-left at (x, y) px, in item order.
    under(frame, t) draws the "behind" pieces onto a copy of `frame` (BGR
    uint8) and returns it; with none it returns `frame` itself (the caller
    can test `is`). The speaker is blended back on top by the caller, so
    those sit BEHIND the speaker. over(frame, t) then draws the "front"
    pieces the same way, after the speaker."""

    def __init__(
        self,
        resolved: Sequence[Any],
        fps: float = FPS,
        offset_sec: float = 0.0,
        frame_size: tuple[int, int] = (720, 1280),
        *,
        glyphs: Optional[Mapping[str, Glyph]] = None,
        lookup: Callable[..., Glyph] = library_glyph,
    ):
        self.fps = float(fps)
        self.offset_sec = float(offset_sec)
        self.frame_size = (int(frame_size[0]), int(frame_size[1]))
        W, H = self.frame_size
        glyphs = dict(glyphs or {})
        self.items: list[_Item] = []
        for it in resolved:
            g = _plain(_get(it, "glyph")) or {}
            source, gid = str(g.get("source") or "library"), str(g.get("id") or "")
            style = str(_get(it, "style") or "badge")
            weight = str(_get(it, "weight") or "fill")
            glyph = glyphs.get(gid)
            if glyph is None:
                # Only a bold one asks the lookup for a weight (a lookup
                # written for fill alone still works).
                glyph = lookup(source, gid) if weight == "fill" else lookup(source, gid, weight)
            box = _plain(_get(it, "box")) or {}
            bw, bh = float(box.get("w", 0)) * W, float(box.get("h", 0)) * H
            text = _get(it, "text")
            font = _get(it, "font")
            look_v = int(_get(it, "look_v") or 0)
            max_w = None
            if style == "chip":
                size = max(4, int(round(bh)))
                max_w = CHIP_MAX_W * W
                look_v = look_v or LOOK_V
                body_w, body_h = chip_geometry(text or "", size, font, max_w=max_w).w, size
            else:
                size = max(4, int(round(min(bw, bh))))
                if style == "circle":
                    look_v = look_v or LOOK_V
                body_w = body_h = size
            x = int(round(float(box.get("x", 0)) * W + (bw - body_w) / 2))
            y = int(round(float(box.get("y", 0)) * H + (bh - body_h) / 2))
            pad = _pad_of(style, size)
            fxd = _plain(_get(it, "fx")) or {}
            entrance = str(fxd.get("entrance") or "glitch")
            entrance = motion_for(style, "glitch" if entrance == "auto" else entrance)
            leave = motion_for(style, fxd.get("exit") or None)
            f_from = int(round(float(_get(it, "from_sec")) * self.fps))
            settle = _get(it, "settle_sec")
            f_settle = int(round(float(settle) * self.fps)) if settle is not None else f_from + IN_FRAMES
            f_to = int(round(float(_get(it, "to_sec")) * self.fps))
            edge, travel = nearest_edge((x, y, body_w, body_h), self.frame_size)
            self.items.append(_Item(
                gkey=f"{source}:{gid}", glyph=glyph, style=style,
                fill=hex_colour(_get(it, "fill") or VIOLET), ink=hex_colour(_get(it, "ink") or WHITE),
                box=(x, y, body_w, body_h), f_from=f_from, f_settle=max(f_from + 1, f_settle),
                f_to=max(f_from + 1, f_to), entrance=entrance,
                seed=str(fxd.get("seed") or f"{source}:{gid}"), edge=edge, travel=travel + pad,
                weight=weight, text=text, font=font, layer=str(_get(it, "layer") or "behind"),
                look_v=look_v, size=size, max_w=max_w, pad=pad, exit=leave,
            ))

    def _frame(self, t: float) -> int:
        """Chunk-local seconds -> the scene frame index."""
        return int(round((self.offset_sec + float(t)) * self.fps))

    def _on(self, t: float, layer: Optional[str] = None) -> list[_Item]:
        f = self._frame(t)
        return [it for it in self.items if it.f_from <= f < it.f_to and layer in (None, it.layer)]

    def active(self, t: float) -> list[int]:
        """Indices (into `resolved`) of the icons on screen at chunk-local t
        (entrance and exit frames included), in every layer."""
        f = self._frame(t)
        return [i for i, it in enumerate(self.items) if it.f_from <= f < it.f_to]

    def boxes(self, t: float, *, drawn: bool = False,
              layer: Optional[str] = "behind") -> list[tuple[int, int, int, int]]:
        """Pixel boxes (x0, y0, x1, y1) of the icons of `layer` ("behind",
        "front", or None for both) on screen at chunk-local t: where each
        body settles, or with drawn=True where its pixels are on this very
        frame (a slide, a glitch's tear or a shadow reaches outside the
        body; a frame it flickers off is left out), clipped to the frame."""
        if not drawn:
            return [(it.box[0], it.box[1], it.box[0] + it.box[2], it.box[1] + it.box[3])
                    for it in self._on(t, layer)]
        W, H = self.frame_size
        out = []
        for rgba, x, y in self.drawn(t, layer=layer):
            h, w = rgba.shape[:2]
            x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
            if x1 > x0 and y1 > y0:
                out.append((x0, y0, x1, y1))
        return out

    def sprite_of(self, index: int) -> Sprite:
        """The settled Sprite of item `index` (pixels + where its body is)."""
        it = self.items[index]
        return _sprite_cached(it.key(), it.glyph)

    def sprite_for(self, index: int) -> np.ndarray:
        """The settled sprite (RGBA uint8) of item `index`."""
        return self.sprite_of(index).rgba

    def frame_of(self, index: int, t: float) -> Optional[FxFrame]:
        """Item `index` at chunk-local t after its entrance / exit, or None.
        (dx, dy) move the SPRITE's top-left from where it rests: the body's
        corner less the shadow's pad. A chip's grow is drawn here (_grown)."""
        it = self.items[index]
        f = self._frame(t)
        if not it.f_from <= f < it.f_to:
            return None
        if it.style == "chip" and "grow" in (it.entrance, it.exit):
            pr = fx_progress(it.entrance, f - it.f_from, end=it.f_to - it.f_from,
                             fin=max(1, it.f_settle - it.f_from), fout=OUT_FRAMES, exit=it.exit)
            if pr is not None and pr.kind == "grow" and pr.phase != "hold":
                return self._grown(index, pr)
        return fx(it.entrance, (f - it.f_from) / self.fps, self.sprite_for(index), it.seed,
                  span=(it.f_to - it.f_from) / self.fps, in_sec=(it.f_settle - it.f_from) / self.fps,
                  out_sec=OUT_FRAMES / self.fps, fps=self.fps, edge=it.edge, travel=it.travel,
                  tear=min(it.box[2], it.box[3]), exit=it.exit)

    def _grown(self, index: int, pr: FxProgress) -> FxFrame:
        """A chip's grow frame (chip_grown, on the settled chip's canvas):
        its circle — the body's height, at the glyph's end, with its shadow —
        popping about its own middle, then the pill unfolding with its
        words coming in. Fully open, it is the settled sprite itself."""
        it = self.items[index]
        sp = self.sprite_of(index)
        if pr.unfold >= 1.0 and pr.words >= 1.0:
            return FxFrame(sp.rgba, 0, 0)
        rgba = _grown_cached(it, pr.unfold, pr.words)
        if pr.unfold > 0.0:
            return FxFrame(rgba, 0, 0)
        rtl = chip_geometry(it.text or "", it.size, it.font, max_w=it.max_w).rtl
        x0 = sp.w - sp.h if rtl else 0
        got = _scaled(np.ascontiguousarray(rgba[:, x0:x0 + sp.h + 2 * sp.pad]), pr.scale, pr.alpha)
        return FxFrame(got.rgba, got.dx + x0, got.dy)

    def drawn(self, t: float, *, layer: Optional[str] = "behind") -> list[tuple[np.ndarray, int, int]]:
        """The icons of `layer` ("behind", "front", or None for both) on
        screen at chunk-local t, as they are drawn this frame: [(rgba, x,
        y)] — straight RGBA uint8 with its top-left at (x, y) px (it may
        reach past the frame), in item order. The primitive under / over
        (and a plate) are built on."""
        out = []
        for i in self.active(t):
            it = self.items[i]
            if layer is not None and it.layer != layer:
                continue
            got = self.frame_of(i, t)
            if got is None:
                continue
            out.append((got.rgba, it.box[0] - it.pad + got.dx, it.box[1] - it.pad + got.dy))
        return out

    def _paint(self, frame_bgr: np.ndarray, pieces: list[tuple[np.ndarray, int, int]]) -> np.ndarray:
        if not pieces:
            return frame_bgr
        out = np.array(frame_bgr, dtype=np.uint8, copy=True)
        for rgba, x, y in pieces:
            composite(out, rgba, x, y)
        return out

    def under(self, frame_bgr: np.ndarray, t: float) -> np.ndarray:
        """`frame_bgr` (H x W x 3 uint8) with the icons BEHIND the speaker
        on screen at chunk-local t drawn on it — a new array, or `frame_bgr`
        itself when none is drawn."""
        return self._paint(frame_bgr, self.drawn(t, layer="behind"))

    def over(self, frame_bgr: np.ndarray, t: float) -> np.ndarray:
        """The same for the icons IN FRONT of the speaker (drawn after her):
        a new array, or `frame_bgr` itself when none is drawn."""
        return self._paint(frame_bgr, self.drawn(t, layer="front"))


def composite(dst_bgr: np.ndarray, rgba: np.ndarray, x: int, y: int) -> None:
    """Paint straight-alpha RGBA onto a BGR uint8 frame in place, top-left at
    (x, y), clipped to the frame."""
    H, W = dst_bgr.shape[:2]
    h, w = rgba.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    src = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    a = src[..., 3:4].astype(np.float32) / 255.0
    if not a.any():
        return
    bgr = src[..., 2::-1].astype(np.float32)
    region = dst_bgr[y0:y1, x0:x1].astype(np.float32)
    dst_bgr[y0:y1, x0:x1] = np.clip(np.rint(bgr * a + region * (1.0 - a)), 0, 255).astype(np.uint8)
