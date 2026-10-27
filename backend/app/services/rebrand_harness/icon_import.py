"""Your team's own icons, uploaded — an SVG or a picture (PNG, WebP, JPEG)
made into a team glyph: the same one-colour 256 px PNG an AI drawing is
(black ink, alpha = the shape, strokes at Phosphor bold's weight), so the
circle and the chip recolour it like any library icon.

Pure: no database, no model, no network, no clock. The same file in the same
mode is the same PNG bytes on every run and machine, so the upload card's
preview IS the icon that is saved, and the same file uploaded twice is one
icon (upload_id: 'c-' + 12 hex of "upload|" + the PNG's sha256).

  SVG      read through an allow-list, never trusted. A DOCTYPE or ENTITY is
           refused before parsing (no XXE, no billion laughs); the parser
           loads no DTD, resolves no entity and never touches the network.
           Anywhere in the file, even inside <defs>: scripts, event handlers
           (on*), embedded documents, style sheets and any link to another
           file (an href or url() that is not "#...") are refused. In the
           drawing: pictures, reused shapes (<use>), text, links, gradients,
           patterns, masks, clips, filters, markers and animation are
           refused, each in plain words ("export it as plain shapes, or
           upload a PNG"). What is drawn: path, rect, circle, ellipse, line,
           polyline and polygon, in groups, with their transforms, fills
           (nonzero or even-odd) and strokes (round-ended). Every painted
           shape is ink; a white or near-white one drawn over ink knocks it
           out (an SVG painted only in white is white-on-clear: its white is
           the ink); a layer under 35 % opacity (a duotone background) is
           left off. Drawn clipped to its viewBox, as a browser shows it.
           The SVG itself is never served back — only our PNG is.
  picture  real transparency (over 5 % of it clear): the alpha is the
           shape — except a PLATE: one solid shape (a disc, a badge)
           carrying contrasting marks, big and centred (the white "</>" on a
           violet disc), whose marks are the icon ("auto"; mode "shape"
           keeps the plate, "marks" takes the marks off it). No
           transparency: keyed off the border's colour — ink on a light
           page, or light on a dark ground; a busy border is refused.

Then every upload is cleaned like an AI drawing (icon_catalog.clean_glyph:
specks dropped, strokes brought to Phosphor bold, cropped, squared, a 16 px
margin) with the upload rules: ink 1-85 % of the picture (a solid badge is
fine) and at most 3 of its edges touched (4 is a photo or a frame). An SVG
or a clear picture is drawn inside a margin first — its shapes ARE the icon —
so only an opaque picture can touch an edge.
"""
from __future__ import annotations

import hashlib
import io
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from . import icon_catalog as ic

# What an upload may be.
KINDS = ("svg", "png", "webp", "jpeg")
MODES = ("auto", "shape", "marks")
MAX_BYTES = 2 * 1024 * 1024          # a picture, at most
SVG_MAX_BYTES = 512 * 1024           # an SVG, at most
MAX_SIDE = 4096                      # px either way (read from the header, before decoding)
MIN_SIDE = 16
NAME_MAX = 40                        # the name an upload is saved under
TAGS_MAX = 12
TAG_MAX = 24

# SVG: drawn SVG_PX square at SVG_SS x SVG_SS samples a pixel, its viewBox
# fitted (centred) inside SVG_PAD of margin.
SVG_PX = 1024
SVG_SS = 2
SVG_PAD = 0.04
SVG_MAX_NODES = 4000
SVG_MAX_DEPTH = 32
SVG_MAX_D = 300_000                  # one shape's path data, characters
SVG_MAX_POINTS = 2_000_000           # every outline's points together
SVG_FILL_BUDGET = 48                 # the painted shapes' boxes together, in canvases
SVG_MAX_CROSSINGS = 4_000_000        # the fills' edge-row crossings together (what filling them holds in memory)
SVG_STROKE_BUDGET = 2e10             # the strokes' drawn pixels together, at most (each segment ~ (length + width) x width)
SVG_KEEP_BYTES = 64 * 1024           # an SVG this small is kept with its icon (never served)
FAINT = 0.35                         # a layer less opaque than this is left off
LIGHT = 0.9                          # a paint this light (every channel) knocks ink out

# Pictures.
WORK_MAX = 1024                      # read at most this big (the long side)
CLEAR_ALPHA = 250                    # a pixel under this alpha is (partly) clear...
CLEAR_SHARE = 0.05                   # ...and over this share of them is real transparency
MARGIN = 0.04                        # a clear picture's shape is drawn inside this margin
BORDER = 0.03                        # the border read for the page's colour, share of the short side
BORDER_BUSY = 0.10                   # over this share of it off its colour: not a plain background
INK_MIN = 0.12                       # a pixel this far from the page's colour is ink...
INK_FEW = 0.002                      # ...and a picture needs this share of such pixels
# The plate rule (clear pictures): the largest shape fills its hull...
PLATE_SOLID = 0.9
# ...and carries marks this different in brightness, covering this share of
# it, whose box is this share of the plate's and centred within this share.
MARK_CONTRAST = 0.35
MARK_SHARE = (0.03, 0.60)
MARK_BOX = 0.35
MARK_CENTRE = 0.15

# The clean-up's rules for an upload (an AI drawing's are stricter).
UPLOAD_INK = (0.01, 0.85)
UPLOAD_EDGES = 3

SVG_NS = "http://www.w3.org/2000/svg"
XINCLUDE_NS = "http://www.w3.org/2001/XInclude"
AS_SHAPES = "export it as plain shapes, or upload a PNG"


class UploadError(ValueError):
    """An upload that can't be an icon; the message is fit for the UI.
    `status`: 413 too large, 415 not a kind we read, 422 anything else."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = int(status)


@dataclass(frozen=True)
class Prepared:
    """An upload made into a team glyph: `png` (the stored bytes), `report`
    (what the clean-up did), `name_guess` (from the file name), `kind`
    ("svg" | "png" | "webp" | "jpeg") and `source_sha` (sha256 of the file)."""
    png: bytes
    report: dict = field(hash=False, compare=False)
    name_guess: str = ""
    kind: str = "png"
    source_sha: str = ""

    @property
    def id(self) -> str:
        return upload_id(self.png)


def upload_id(png: bytes) -> str:
    """An uploaded glyph's id: 'c-' + 12 hex of "upload|" + its PNG's
    sha256 — content-addressed, so the same picture is always one icon
    (and the same id space as an AI drawing's: no recipe change)."""
    return "c-" + hashlib.sha256(("upload|" + hashlib.sha256(png).hexdigest()).encode()).hexdigest()[:12]


def sniff(data: bytes) -> Optional[str]:
    """What a file is, from its first bytes (never its name): "png",
    "jpeg", "webp", "svg" (anything that opens like XML, checked when read)
    or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "svg"                                     # UTF-16: refused as such when read
    head = data[:512].lstrip(b"\xef\xbb\xbf").lstrip()
    return "svg" if head[:1] == b"<" else None


_NAME_NOISE = frozenset("icon icons ic fill filled bold regular outline outlined line light thin duotone "
                        "solid glyph vector svg png jpg jpeg webp final copy export".split())


def name_guess(filename: str) -> str:
    """A name from the file's name: "graduation-cap_bold.svg" -> "Graduation
    cap", "GraduationCap.png" -> "Graduation cap"; sizes, versions and words
    like "icon" / "fill" left out. "" when nothing is left."""
    base = re.split(r"[\\/]", str(filename or ""))[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base)
    stem = re.sub(r"@\d(?:\.\d)?x$", "", stem)                     # "logo@2x"
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    words = []
    for w in re.split(r"[\s_\-.,()\[\]]+", stem):
        low = w.lower()
        if not w or low in _NAME_NOISE or re.fullmatch(r"\d+(px)?|\d+x\d+|@\dx|v\d+|\(?\d+\)?", low):
            continue
        words.append(w if (w.isupper() and len(w) > 1) else low)
    name = " ".join(words)
    name = name[:1].upper() + name[1:]
    return name[:NAME_MAX].strip()


def import_upload(data: bytes, filename: str = "", *, mode: str = "auto") -> Prepared:
    """An uploaded file made into a team glyph (module docstring). Raises
    UploadError with the reason, in the owner's words."""
    data = bytes(data or b"")
    if mode not in MODES:
        raise UploadError(f"Unknown mode {mode!r}")
    if not data:
        raise UploadError("The file is empty")
    kind = sniff(data)
    if kind is None:
        raise UploadError("Upload an SVG, PNG, WebP or JPEG", status=415)
    limit = SVG_MAX_BYTES if kind == "svg" else MAX_BYTES
    if len(data) > limit:
        raise UploadError(f"The file is larger than {limit // 1024} KB" if limit < 1024 * 1024 else
                          f"The file is larger than {limit // (1024 * 1024)} MB", status=413)
    if kind == "svg":
        if mode == "marks":
            raise UploadError("Taking the marks off a plate works on a PNG with a clear background")
        alpha, report = _svg_alpha(data)
    else:
        alpha, report = _picture_alpha(_picture(data, kind), mode)
    try:
        png, cleaned = ic.clean_glyph(alpha, ink_range=UPLOAD_INK, max_edges=UPLOAD_EDGES, what="icon")
    except ic.DrawError as e:
        raise UploadError(str(e)) from e
    return Prepared(png=png, report={"kind": kind, **report, **cleaned}, name_guess=name_guess(filename),
                    kind=kind, source_sha=hashlib.sha256(data).hexdigest())


def kept_source(data: bytes, kind: str) -> Optional[bytes]:
    """The file kept with its icon (only to make it again, never served):
    an SVG of at most SVG_KEEP_BYTES; nothing for a picture."""
    return bytes(data) if kind == "svg" and len(data) <= SVG_KEEP_BYTES else None


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

_SHAPES = frozenset("path rect circle ellipse line polyline polygon".split())
_QUIET = frozenset("title desc metadata defs".split())          # skipped whole
# Never read, wherever they are (even inside <defs>).
_DANGER = {
    "script": "The SVG has a script in it",
    "handler": "The SVG has a script in it",
    "listener": "The SVG has a script in it",
    "foreignObject": "The SVG has other content embedded in it",
    "iframe": "The SVG has other content embedded in it",
    "embed": "The SVG has other content embedded in it",
    "object": "The SVG has other content embedded in it",
    "style": "The SVG uses a style sheet — export it with presentation attributes, or upload a PNG",
}
# Nothing we can draw faithfully, in the drawing.
_UNDRAWN = {
    "image": "The SVG has a picture inside it — upload that picture instead",
    "use": f"The SVG reuses shapes (<use>) — {AS_SHAPES}",
    "symbol": f"The SVG reuses shapes (<symbol>) — {AS_SHAPES}",
    "text": "The SVG has text in it — turn the text into outlines, or upload a PNG",
    "tspan": "The SVG has text in it — turn the text into outlines, or upload a PNG",
    "textPath": "The SVG has text in it — turn the text into outlines, or upload a PNG",
    "a": f"The SVG has a link in it — {AS_SHAPES}",
    "linearGradient": f"The SVG uses a gradient — {AS_SHAPES}",
    "radialGradient": f"The SVG uses a gradient — {AS_SHAPES}",
    "meshgradient": f"The SVG uses a gradient — {AS_SHAPES}",
    "pattern": f"The SVG uses a pattern fill — {AS_SHAPES}",
    "mask": f"The SVG uses a mask — {AS_SHAPES}",
    "clipPath": f"The SVG uses a clip — {AS_SHAPES}",
    "filter": f"The SVG uses a filter — {AS_SHAPES}",
    "marker": f"The SVG uses markers — {AS_SHAPES}",
    "switch": f"The SVG uses <switch> — {AS_SHAPES}",
    "svg": f"The SVG has another SVG inside it — {AS_SHAPES}",
    "animate": "The SVG is animated — export one still frame, or upload a PNG",
    "animateMotion": "The SVG is animated — export one still frame, or upload a PNG",
    "animateTransform": "The SVG is animated — export one still frame, or upload a PNG",
    "animateColor": "The SVG is animated — export one still frame, or upload a PNG",
    "set": "The SVG is animated — export one still frame, or upload a PNG",
}
_ANIMATION = frozenset(("animate", "animateMotion", "animateTransform", "animateColor", "set"))
LINKS_OUT = "The SVG links to another file"
HANDLERS = "The SVG has a script in it (an event handler)"
URL_PAINT = f"The SVG uses a gradient or pattern paint — {AS_SHAPES}"
TOO_COMPLEX = f"The SVG is too complex — {AS_SHAPES}"
UNREADABLE = "The SVG could not be read"

# Presentation properties read (an attribute, or the style attribute's,
# which wins); the inherited ones pass down the tree.
_PROPS = ("fill", "stroke", "stroke-width", "fill-rule", "opacity", "fill-opacity", "stroke-opacity",
          "display", "visibility")
_INHERITED = ("fill", "stroke", "stroke-width", "fill-rule", "fill-opacity", "stroke-opacity", "visibility")
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_URL = re.compile(r"url\(\s*['\"]?\s*([^'\")\s]*)", re.I)
_TRANSFORM = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")
# Named colours that are white or near it (knock-outs); every other name is ink.
_LIGHT_NAMES = frozenset("white snow ivory ghostwhite whitesmoke seashell floralwhite mintcream azure aliceblue "
                         "honeydew lavenderblush".split())

Paint = Optional[tuple[tuple[float, float, float], float]]      # (rgb 0..1, alpha) or None


def _local(tag: Any) -> str:
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _ns(tag: Any) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _num(v: Any, default: float = 0.0) -> float:
    m = _NUM.match(str(v if v is not None else "").strip())
    if not m:
        return default
    x = float(m.group())
    return x if math.isfinite(x) else default


def _unit(v: Any, default: float = 1.0) -> float:
    """An opacity: "0.4" or "40%", clamped to 0..1."""
    s = str(v if v is not None else "").strip()
    if not s:
        return default
    x = _num(s, default)
    if s.endswith("%"):
        x /= 100.0
    return min(1.0, max(0.0, x))


def _transform(tf: Optional[str]) -> np.ndarray:
    """An SVG transform list as a 3 x 3 matrix (left to right, as SVG
    applies them)."""
    M = np.eye(3)
    for name, args in _TRANSFORM.findall(tf or ""):
        a = [float(x) for x in _NUM.findall(args)]
        T = None
        if name == "matrix" and len(a) == 6:
            T = np.array([[a[0], a[2], a[4]], [a[1], a[3], a[5]], [0, 0, 1]])
        elif name == "translate" and a:
            T = np.array([[1, 0, a[0]], [0, 1, a[1] if len(a) > 1 else 0.0], [0, 0, 1]])
        elif name == "scale" and a:
            T = np.diag([a[0], a[1] if len(a) > 1 else a[0], 1.0])
        elif name == "rotate" and a:
            r = math.radians(a[0])
            c, s = math.cos(r), math.sin(r)
            T = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
            if len(a) == 3:
                T = (np.array([[1, 0, a[1]], [0, 1, a[2]], [0, 0, 1]]) @ T
                     @ np.array([[1, 0, -a[1]], [0, 1, -a[2]], [0, 0, 1]]))
        elif name == "skewX" and a:
            T = np.array([[1, math.tan(math.radians(a[0])), 0], [0, 1, 0], [0, 0, 1]])
        elif name == "skewY" and a:
            T = np.array([[1, 0, 0], [math.tan(math.radians(a[0])), 1, 0], [0, 0, 1]])
        if T is not None:
            M = M @ T
    if not np.isfinite(M).all():
        raise UploadError(UNREADABLE)
    return M


def _paint(v: str) -> Paint:
    """A fill or stroke value: (rgb 0..1, alpha), or None for no paint.
    currentColor and unknown names are ink (black); a url() paint (a
    gradient, a pattern) is refused."""
    s = re.sub(r"!important\s*$", "", str(v or "")).strip().lower()
    if not s or s in ("none", "transparent"):
        return None
    if "url(" in s:
        raise UploadError(URL_PAINT)
    if s.startswith("#"):
        h = s[1:]
        if re.fullmatch(r"[0-9a-f]{3,4}", h):
            h = "".join(ch * 2 for ch in h)
        if re.fullmatch(r"[0-9a-f]{6}([0-9a-f]{2})?", h):
            rgb = tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
            return rgb, (int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0)
        return (0.0, 0.0, 0.0), 1.0
    m = re.fullmatch(r"rgba?\(([^)]*)\)", s)
    if m:
        parts = [p for p in re.split(r"[\s,/]+", m.group(1).strip()) if p]
        rgb = tuple(min(1.0, max(0.0, _num(p) / (100.0 if p.endswith("%") else 255.0))) for p in parts[:3])
        if len(rgb) != 3:
            return (0.0, 0.0, 0.0), 1.0
        return rgb, (_unit(parts[3]) if len(parts) > 3 else 1.0)
    if s in _LIGHT_NAMES:
        return (1.0, 1.0, 1.0), 1.0
    return (0.0, 0.0, 0.0), 1.0


def _light(p: Paint) -> bool:
    return p is not None and min(p[0]) >= LIGHT


def _style(el: Any, inherited: dict) -> dict:
    """The element's presentation properties: the inherited ones, then its
    attributes, then its style attribute (which wins)."""
    st = {k: v for k, v in inherited.items() if k in _INHERITED}
    for k in _PROPS:
        v = el.get(k)
        if v is not None:
            st[k] = v.strip()
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = (s.strip() for s in part.split(":", 1))
            if k.lower() in _PROPS:
                st[k.lower()] = v
    for k in ("fill", "stroke"):
        if st.get(k, "").strip().lower() == "inherit":
            st[k] = inherited.get(k, "black" if k == "fill" else "none")
    return st


def _shape_d(tag: str, el: Any) -> str:
    """A basic shape as path data (a path's own `d`)."""
    g = lambda k: _num(el.get(k))                    # noqa: E731
    if tag == "path":
        return el.get("d") or ""
    if tag == "rect":
        x, y, w, h = g("x"), g("y"), g("width"), g("height")
        if w <= 0 or h <= 0:
            return ""
        rx_s, ry_s = el.get("rx"), el.get("ry")
        rx = _num(rx_s if rx_s is not None else ry_s)
        ry = _num(ry_s if ry_s is not None else rx_s)
        rx, ry = min(max(0.0, rx), w / 2), min(max(0.0, ry), h / 2)
        if rx > 0 and ry > 0:
            return (f"M{x + rx},{y}H{x + w - rx}A{rx},{ry} 0 0 1 {x + w},{y + ry}V{y + h - ry}"
                    f"A{rx},{ry} 0 0 1 {x + w - rx},{y + h}H{x + rx}A{rx},{ry} 0 0 1 {x},{y + h - ry}"
                    f"V{y + ry}A{rx},{ry} 0 0 1 {x + rx},{y}Z")
        return f"M{x},{y}H{x + w}V{y + h}H{x}Z"
    if tag in ("circle", "ellipse"):
        cx, cy = g("cx"), g("cy")
        rx = g("r") if tag == "circle" else g("rx")
        ry = g("r") if tag == "circle" else g("ry")
        if tag == "ellipse" and el.get("ry") is None:
            ry = rx
        if tag == "ellipse" and el.get("rx") is None:
            rx = ry
        if rx <= 0 or ry <= 0:
            return ""
        return f"M{cx - rx},{cy}A{rx},{ry} 0 1 0 {cx + rx},{cy}A{rx},{ry} 0 1 0 {cx - rx},{cy}Z"
    if tag == "line":
        return f"M{g('x1')},{g('y1')}L{g('x2')},{g('y2')}"
    pts = _NUM.findall(el.get("points") or "")
    pairs = [f"{pts[i]},{pts[i + 1]}" for i in range(0, len(pts) - 1, 2)]
    if not pairs:
        return ""
    return "M" + "L".join(pairs) + ("Z" if tag == "polygon" else "")


class _Outline:
    """A fontTools pen flattening path data into polylines (float64 (n, 2),
    path units), each with whether it was closed; a curve is split finely
    enough for `scale` output px per path unit."""

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

            def _closePath(self):
                if outer.closed:
                    outer.closed[-1] = True

            def _endPath(self):
                pass

        self.pen = Pen(None)
        self.scale = float(scale)
        self.lines: list[list[tuple[float, float]]] = []
        self.closed: list[bool] = []
        self.points = 0
        self._cur: list[tuple[float, float]] = []

    def _start(self, p):
        self._cur = [p]
        self.lines.append(self._cur)
        self.closed.append(False)

    def _cubic(self, p1, p2, p3):
        p0 = self._cur[-1]
        span = (math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)) * self.scale
        n = int(min(96, max(2, math.ceil(span / 2.0)))) if math.isfinite(span) else 2
        t = np.arange(1, n + 1, dtype=np.float64) / n
        mt = 1.0 - t
        a, b, c, d = mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3
        xs = a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0]
        ys = a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]
        self._cur.extend(zip(xs.tolist(), ys.tolist()))


@dataclass
class _Shape:
    lines: list                                  # canvas px (supersampled), float64 (n, 2)
    closed: list
    fill: Paint
    stroke: Paint
    stroke_px: float
    evenodd: bool
    fill_alpha: float                            # opacity x fill-opacity x the paint's own alpha
    stroke_alpha: float


def _check_tree(root: Any) -> int:
    """Whatever the file carries, even where nothing is drawn: no scripts,
    handlers, embedded documents, style sheets or links out. Returns the
    number of elements."""
    n = 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        n += 1
        if n > SVG_MAX_NODES:
            raise UploadError(TOO_COMPLEX)
        tag = _local(el.tag)
        if tag in _DANGER:
            raise UploadError(_DANGER[tag])
        if tag == "image" or tag in _ANIMATION:
            raise UploadError(_UNDRAWN[tag])        # wherever it is (inside a shape too)
        if _ns(el.tag) == XINCLUDE_NS:
            raise UploadError(LINKS_OUT)
        for k, v in el.attrib.items():
            name = _local(k).lower()
            if name.startswith("on"):
                raise UploadError(HANDLERS)
            if name == "href" and not str(v).strip().startswith("#"):
                raise UploadError(LINKS_OUT)
            low = str(v).lower()
            if "javascript:" in low:
                raise UploadError(HANDLERS)
            if "url(" in low and any(not u.startswith("#") for u in _URL.findall(str(v))):
                raise UploadError(LINKS_OUT)
    return n


def _viewbox(root: Any) -> tuple[float, float, float, float]:
    vb = [float(x) for x in _NUM.findall(root.get("viewBox") or "")]
    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0 and all(math.isfinite(x) for x in vb):
        return vb[0], vb[1], vb[2], vb[3]
    w, h = root.get("width"), root.get("height")
    if w and h and "%" not in w + h and _num(w) > 0 and _num(h) > 0:
        return 0.0, 0.0, _num(w), _num(h)
    raise UploadError("The SVG has no size (a viewBox) — export it again with one, or upload a PNG")


def _svg_alpha(data: bytes) -> tuple[Any, dict]:
    """An SVG drawn as one colour: (alpha float32 SVG_PX square, report
    {"via": "shapes", "svg": {fills, strokes, knockouts, faint, skipped}})."""
    from lxml import etree

    if b"\x00" in data:
        raise UploadError("Save the SVG as UTF-8 and upload it again")
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise UploadError("The SVG declares a DOCTYPE or entities — export it again as a plain SVG, or upload a PNG")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
                             huge_tree=False, remove_comments=True, remove_pis=True)
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        raise UploadError(UNREADABLE) from e
    docinfo = root.getroottree().docinfo
    if docinfo.doctype or docinfo.internalDTD is not None:
        raise UploadError("The SVG declares a DOCTYPE or entities — export it again as a plain SVG, or upload a PNG")
    if _local(root.tag) != "svg" or _ns(root.tag) not in ("", SVG_NS):
        raise UploadError("This isn't an SVG")
    _check_tree(root)

    vx, vy, vw, vh = _viewbox(root)
    N = SVG_PX * SVG_SS
    k = N * (1.0 - 2.0 * SVG_PAD) / max(vw, vh)
    ox, oy = (N - vw * k) / 2.0, (N - vh * k) / 2.0
    base = np.array([[k, 0.0, ox - vx * k], [0.0, k, oy - vy * k], [0.0, 0.0, 1.0]])
    shapes: list[_Shape] = []
    skipped: set[str] = set()
    budget = {"points": 0}

    def walk(el: Any, M: np.ndarray, st: dict, opacity: float, depth: int, top: bool = False) -> None:
        if not isinstance(el.tag, str):
            return
        if depth > SVG_MAX_DEPTH:
            raise UploadError(TOO_COMPLEX)
        tag = _local(el.tag)
        if _ns(el.tag) not in ("", SVG_NS):
            skipped.add(tag)                        # another program's notes (Inkscape, Sketch...)
            return
        if tag in _QUIET:
            return
        if not top and tag in _UNDRAWN:
            raise UploadError(_UNDRAWN[tag])
        if not top and tag not in _SHAPES and tag != "g":
            skipped.add(tag)
            return
        if any(_local(a).lower() == "href" for a in el.attrib):
            raise UploadError(f"The SVG has a link in it — {AS_SHAPES}")
        st = _style(el, st)
        if st.get("display", "").lower() == "none":
            return
        opacity *= _unit(st.get("opacity"))
        M = M @ _transform(el.get("transform"))
        if top or tag == "g":
            for ch in el:
                walk(ch, M, st, opacity, depth + 1)
            return
        if st.get("visibility", "").lower() in ("hidden", "collapse"):
            return
        d = _shape_d(tag, el)
        if not d:
            return
        if len(d) > SVG_MAX_D:
            raise UploadError(TOO_COMPLEX)
        scale = math.sqrt(abs(float(np.linalg.det(M[:2, :2]))))
        flat = _Outline(scale)
        try:
            from fontTools.svgLib.path import parse_path

            parse_path(d, flat.pen)
        except Exception as e:  # noqa: BLE001 — a malformed path
            raise UploadError(f"A shape in the SVG could not be read — {AS_SHAPES}") from e
        lines, closed = [], []
        for line, cl in zip(flat.lines, flat.closed):
            if not line:
                continue
            a = np.asarray(line, dtype=np.float64)
            a = (np.c_[a, np.ones(len(a))] @ M.T)[:, :2]
            if not np.isfinite(a).all():
                raise UploadError(UNREADABLE)
            lines.append(a)
            closed.append(cl)
            budget["points"] += len(a)
        if budget["points"] > SVG_MAX_POINTS:
            raise UploadError(TOO_COMPLEX)
        if not lines:
            return
        fill = _paint(st.get("fill", "black"))
        stroke = _paint(st.get("stroke", "none"))
        if tag in ("line", "polyline"):
            fill = None                              # an open shape has no inside
        shapes.append(_Shape(
            lines=lines, closed=closed, fill=fill, stroke=stroke,
            stroke_px=max(0.0, _num(st.get("stroke-width"), 1.0)) * scale,
            evenodd=st.get("fill-rule", "").lower() == "evenodd",
            fill_alpha=opacity * _unit(st.get("fill-opacity")) * (fill[1] if fill else 0.0),
            stroke_alpha=opacity * _unit(st.get("stroke-opacity")) * (stroke[1] if stroke else 0.0)))

    walk(root, base @ _transform(root.get("transform")), {"fill": "black"}, 1.0, 0, top=True)

    # Painted only in white (an icon made for dark backgrounds): its white
    # is the ink, not a knock-out.
    visible = [(p, a) for s in shapes for p, a in ((s.fill, s.fill_alpha), (s.stroke, s.stroke_alpha))
               if p is not None and a >= FAINT]
    white_ink = bool(visible) and all(_light(p) for p, _ in visible)
    ink = np.zeros((N, N), bool)
    counts = {"fills": 0, "strokes": 0, "knockouts": 0, "faint": 0}
    area = [0, 0, 0.0]                           # [box pixels, fill crossings, stroke pixels] so far
    for s in shapes:
        for part in ("fill", "stroke"):
            p = getattr(s, part)
            if p is None:
                continue
            if (s.fill_alpha if part == "fill" else s.stroke_alpha) < FAINT:
                counts["faint"] += 1
                continue
            got = (_fill(s.lines, N, s.evenodd, area) if part == "fill"
                   else _stroke(s.lines, s.closed, s.stroke_px, N, area))
            if area[0] > SVG_FILL_BUDGET * N * N:
                raise UploadError(TOO_COMPLEX)
            if got is None:
                continue
            (y0, x0), cov = got
            region = ink[y0:y0 + cov.shape[0], x0:x0 + cov.shape[1]]
            if _light(p) and not white_ink:
                region &= ~cov
                counts["knockouts"] += 1
            else:
                region |= cov
                counts["fills" if part == "fill" else "strokes"] += 1
    # Clipped to the viewBox, as a browser shows it: the pixels whose centre
    # is inside it (the fills' own rule).
    x0, x1 = int(math.ceil(ox - 0.5)), int(math.ceil(N - ox - 0.5))
    y0, y1 = int(math.ceil(oy - 0.5)), int(math.ceil(N - oy - 0.5))
    ink[:, :max(0, x0)] = False
    ink[:, max(0, x1):] = False
    ink[:max(0, y0), :] = False
    ink[max(0, y1):, :] = False
    alpha = ink.reshape(SVG_PX, SVG_SS, SVG_PX, SVG_SS).mean(axis=(1, 3), dtype=np.float32)
    return alpha, {"via": "shapes", "plate": None,
                   "svg": {**counts, "white_ink": white_ink, "skipped": sorted(skipped)[:8]}}


def _box(lines: list, n: int, pad: float = 0.0) -> Optional[tuple[int, int, int, int]]:
    pts = np.concatenate(lines)
    x0 = max(0, int(math.floor(pts[:, 0].min() - pad)))
    y0 = max(0, int(math.floor(pts[:, 1].min() - pad)))
    x1 = min(n, int(math.ceil(pts[:, 0].max() + pad)) + 1)
    y1 = min(n, int(math.ceil(pts[:, 1].max() + pad)) + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _evenodd(contours: list, w: int, h: int) -> np.ndarray:
    """Even-odd coverage (bool h x w) of closed polygons in pixel units at
    pixel centres: icon_render._winding_fill's crossings, counted by parity."""
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
    ylo, yhi = np.minimum(y0, y1), np.maximum(y0, y1)
    r0 = np.clip(np.ceil(ylo - 0.5), 0, h).astype(np.int64)
    r1 = np.clip(np.ceil(yhi - 0.5), 0, h).astype(np.int64)
    counts = np.maximum(r1 - r0, 0)
    total = int(counts.sum())
    delta = np.zeros((h, w + 1), np.int32)
    if total:
        edge = np.repeat(np.arange(len(counts)), counts)
        start = np.repeat(np.cumsum(counts) - counts, counts)
        rows = r0[edge] + (np.arange(total) - start)
        t = (rows + 0.5 - y0[edge]) / (y1[edge] - y0[edge])
        cols = np.clip(np.floor(x0[edge] + t * (x1[edge] - x0[edge]) + 0.5), 0, w).astype(np.int64)
        np.add.at(delta, (rows, cols), 1)
    return (np.cumsum(delta, axis=1)[:, :w] % 2) == 1


def _crossings(contours: list, h: int) -> int:
    """How many (edge, pixel row) crossings filling these closed polygons
    takes: the rows whose centre each edge spans, as the fillers count
    them (icon_render._winding_fill, _evenodd)."""
    total = 0
    for c in contours:
        ya = c[:, 1]
        yb = np.roll(ya, -1)
        r0 = np.clip(np.ceil(np.minimum(ya, yb) - 0.5), 0, h)
        r1 = np.clip(np.ceil(np.maximum(ya, yb) - 0.5), 0, h)
        total += int(np.maximum(r1 - r0, 0).sum())
    return total


def _fill(lines: list, n: int, evenodd: bool, area: list) -> Optional[tuple[tuple[int, int], np.ndarray]]:
    """A shape's inside on the n x n canvas, drawn in its own box only:
    ((y0, x0), bool coverage) or None when it is off the canvas."""
    from . import icon_render as ir

    polys = [a for a in lines if len(a) >= 3]
    if not polys:
        return None
    box = _box(polys, n)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    area[0] += (x1 - x0) * (y1 - y0)
    sub = [a - (x0, y0) for a in polys]
    # The filler holds one entry per row each edge crosses: counted first,
    # so a zig-zag of full-height edges is refused, never allocated.
    area[1] += _crossings(sub, y1 - y0)
    if area[1] > SVG_MAX_CROSSINGS:
        raise UploadError(TOO_COMPLEX)
    cov = _evenodd(sub, x1 - x0, y1 - y0) if evenodd else ir._winding_fill(sub, x1 - x0, y1 - y0)
    return (y0, x0), cov


def _stroke(lines: list, closed: list, width: float, n: int,
            area: list) -> Optional[tuple[tuple[int, int], np.ndarray]]:
    """A shape's outline on the n x n canvas, `width` px wide with round
    ends and joins (OpenCV's thick lines): ((y0, x0), bool coverage)."""
    import cv2

    if width <= 0:
        return None
    box = _box(lines, n, width / 2.0 + 2.0)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    area[0] += (x1 - x0) * (y1 - y0)
    # OpenCV draws at most 32767 px wide; and every segment costs about
    # (its length + the width) x the width to draw — too much of it (a
    # long run of tiny segments drawn very thick) is refused, never drawn.
    t = max(1, int(round(min(width, 32767.0))))
    area[2] += sum(float(np.hypot(*np.diff(a, axis=0).T).sum()) + t * len(a) for a in lines) * t
    if area[2] > SVG_STROKE_BUDGET:
        raise UploadError(TOO_COMPLEX)
    layer = np.zeros((y1 - y0, x1 - x0), np.uint8)
    lim = 4 * n
    for a, cl in zip(lines, closed):
        # 2 bits of sub-pixel position (shift=2); far-off points clamped.
        q = np.rint(np.clip(a - (x0, y0), -lim, lim) * 4).astype(np.int32)
        if len(q) == 1 or not np.any(q != q[0]):
            cv2.circle(layer, (int(q[0][0]), int(q[0][1])), max(1, t // 2) * 4, 255, -1, cv2.LINE_8, 2)
            continue
        cv2.polylines(layer, [q.reshape(-1, 1, 2)], bool(cl), 255, thickness=t, lineType=cv2.LINE_8, shift=2)
    return (y0, x0), layer > 0


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

_PIL_FORMATS = {"png": "PNG", "webp": "WEBP", "jpeg": "JPEG"}


def _picture(data: bytes, kind: str) -> Any:
    """A PNG / WebP / JPEG as RGBA uint8, its long side at most WORK_MAX. The
    size is read from the header and checked BEFORE the pixels are decoded
    (a decompression bomb never is); only that one decoder is allowed."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data), formats=[_PIL_FORMATS[kind]]) as im:
            w, h = im.size
            if max(w, h) > MAX_SIDE:
                raise UploadError(f"The picture is larger than {MAX_SIDE} x {MAX_SIDE} px", status=413)
            if min(w, h) < MIN_SIDE:
                raise UploadError(f"The picture is too small — use one at least {MIN_SIDE} px across")
            rgba = im.convert("RGBA")
    except UploadError:
        raise
    except Image.DecompressionBombError as e:
        # PIL's own guard (when on): a header claiming billions of pixels.
        raise UploadError(f"The picture is larger than {MAX_SIDE} x {MAX_SIDE} px", status=413) from e
    except Exception as e:  # noqa: BLE001 — a damaged or disguised file
        raise UploadError("The picture could not be read") from e
    if max(w, h) > WORK_MAX:
        f = WORK_MAX / max(w, h)
        rgba = rgba.resize((max(1, round(w * f)), max(1, round(h * f))), Image.BOX)
    return np.asarray(rgba)


def _luma(rgb: Any) -> Any:
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def _picture_alpha(rgba: Any, mode: str) -> tuple[Any, dict]:
    """A picture's shape (alpha float32 0..1) and how it was found (module
    docstring): {"via": "transparency" | "light page" | "dark ground",
    "plate": None | "kept" | "dropped"}."""
    a = rgba[..., 3]
    rgb = rgba[..., :3].astype(np.float32) / 255.0
    if float((a < CLEAR_ALPHA).mean()) > CLEAR_SHARE:
        alpha, plate = _plate(a.astype(np.float32) / 255.0, _luma(rgb), mode)
        return _margined(alpha), {"via": "transparency", "plate": plate}
    if mode == "marks":
        raise UploadError("Taking the marks off a plate works on a PNG with a clear background")
    h, w = a.shape
    r = max(1, int(min(h, w) * BORDER))
    border = np.concatenate([rgb[:r].reshape(-1, 3), rgb[-r:].reshape(-1, 3),
                             rgb[:, :r].reshape(-1, 3), rgb[:, -r:].reshape(-1, 3)])
    paper = np.median(border, axis=0)

    def far(x: Any) -> Any:
        return np.sqrt(((x - paper) ** 2).sum(axis=-1) / 3.0)

    if float((far(border) > INK_MIN).mean()) > BORDER_BUSY:
        raise UploadError("The background isn't plain all round — use a PNG with a clear background, "
                          "or leave a plain margin around the icon")
    d = far(rgb).astype(np.float32)
    strong = d[d > INK_MIN]
    if strong.size < INK_FEW * d.size:
        raise UploadError("Nothing stands out from the background")
    level = float(np.percentile(strong, 90))
    alpha = np.clip((d / level - 0.3) / 0.5, 0.0, 1.0).astype(np.float32)
    return alpha, {"via": "light page" if float(_luma(paper)) > 0.5 else "dark ground", "plate": None}


def _margined(alpha: Any) -> Any:
    """A clear picture's shape inside a MARGIN of clear: its shapes are the
    icon, so they never count as touching the picture's edges."""
    h, w = alpha.shape
    m = max(1, int(round(MARGIN * max(h, w))))
    return np.pad(alpha, m).astype(np.float32)


def _plate(alpha: Any, luma: Any, mode: str) -> tuple[Any, Optional[str]]:
    """The plate rule on a clear picture: (alpha, None when there is no
    plate | "kept" | "dropped"). A plate is the largest shape filling its
    hull (PLATE_SOLID) with marks MARK_CONTRAST brighter or darker than it
    covering MARK_SHARE of it; "auto" takes the marks off only when their
    box is MARK_BOX of the plate's and centred (MARK_CENTRE) — a two-colour
    heart keeps its highlight — "marks" whenever there is a plate, "shape"
    never. Raises UploadError for "marks" with no plate."""
    import cv2

    solid = (alpha > 0.5).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
    plate = None
    if n > 1:
        big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        blob = labels == big
        hull = cv2.convexHull(cv2.findNonZero(blob.astype(np.uint8)))
        solidity = float(blob.sum()) / max(1.0, float(cv2.contourArea(hull)))
        if solidity > PLATE_SOLID:
            tone = float(np.median(luma[blob]))
            marks_a = (np.clip((np.abs(luma - tone) - (MARK_CONTRAST - 0.15)) / 0.3, 0.0, 1.0)
                       * blob * alpha).astype(np.float32)
            marks = marks_a > 0.5
            share = float(marks.sum()) / float(blob.sum())
            if MARK_SHARE[0] <= share <= MARK_SHARE[1]:
                bx, by, bw, bh = cv2.boundingRect(blob.astype(np.uint8))
                mx, my, mw, mh = cv2.boundingRect(marks.astype(np.uint8))
                big_enough = (mw * mh) / float(bw * bh) >= MARK_BOX
                centred = (abs((mx + mw / 2) - (bx + bw / 2)) < MARK_CENTRE * bw
                           and abs((my + mh / 2) - (by + bh / 2)) < MARK_CENTRE * bh)
                plate = (marks_a, big_enough and centred)
    if plate is None:
        if mode == "marks":
            raise UploadError("There are no marks on a plate to take off here — keep the whole shape")
        return alpha, None
    marks_a, fits = plate
    if mode == "marks" or (mode == "auto" and fits):
        return marks_a, "dropped"
    return alpha, "kept"


def clean_name(name: Any) -> str:
    """A team icon's name as it is saved: spaces collapsed, at most NAME_MAX."""
    s = unicodedata.normalize("NFC", re.sub(r"\s+", " ", str(name or ""))).strip()
    return s[:NAME_MAX].strip()


def clean_tags(tags: Any) -> list[str]:
    """Tags as they are saved: from a list or "a, b" — each name_key'd (so a
    search finds it), at most TAG_MAX characters, no repeats, at most
    TAGS_MAX."""
    raw = tags if isinstance(tags, (list, tuple)) else re.split(r"[,;\n]+", str(tags or ""))
    out: list[str] = []
    for t in raw:
        k = ic.name_key(str(t))[:TAG_MAX].strip()
        if k and k not in out:
            out.append(k)
    return out[:TAGS_MAX]
