"""The words on a chip icon — shaped and outlined by us, so every machine
draws the same bytes.

  shape(text, key)            the words as runs in VISUAL order: a minimal
                              bidi split (strong right-to-left / left-to-right
                              letters, numbers kept left-to-right, spaces and
                              marks taking the side of the letters around
                              them; the line reads right-to-left when it holds
                              any Arabic), each run shaped by HarfBuzz
                              (uharfbuzz, pinned) with kerning and ligatures,
                              Arabic runs in the Arabic face.
  units(text, key)            the words' advance per px of em (the width at
                              em px is units * em, exactly as text_path lays
                              the glyphs out).
  text_path(text, key, em)    the shaped words as SVG path data in px: the
                              pen starts at x 0 on the baseline, y down —
                              outlines from fontTools, so icon_render fills
                              them with the SAME rasteriser as the glyphs. No
                              FreeType, no Pillow text, no hinting, no system
                              fonts, nothing that differs between the dev PC
                              and the VPS.

The fonts are the TTFs in app/assets/brand42/fonts/, named in fonts.json
(make_brand42_assets.py --label-fonts). A LABEL key ("label-600@37e8bc5a")
names a Latin face and an Arabic face together, and its hash covers both
files' keys, which cover their bytes: a chip records its label key
(IconResolved.font), a key's files are never replaced, so an old version
re-renders with its own fonts. A file whose bytes no longer match fonts.json
is refused, never drawn.
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "brand42" / "fonts"
MANIFEST = FONTS_DIR / "fonts.json"
FEATURES = {"kern": True, "liga": True}

# The Arabic blocks (letters, marks, digits and presentation forms): drawn in
# the Arabic face whatever their bidi class.
_ARABIC = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))

# fontTools' glyph sets decompile lazily: one shaping at a time.
_LOCK = threading.Lock()


class FontError(RuntimeError):
    """A label font that is missing or does not match fonts.json."""


# ---------------------------------------------------------------------------
# The fonts
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def manifest() -> dict:
    """fonts.json: {default, faces: {face key: {file, sha256, family, weight,
    scripts, licence, source}}, labels: {label key: {latin, arabic}}}."""
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FontError(f"the label fonts are missing ({MANIFEST.name}): {e}") from e


def default_key() -> str:
    """The label key new chips are drawn with."""
    return str(manifest()["default"])


def label_keys() -> list[str]:
    return sorted(manifest().get("labels") or {})


def _label(key: str) -> tuple[str, str]:
    """(Latin face key, Arabic face key) of a label key; KeyError if unknown."""
    lab = (manifest().get("labels") or {}).get(str(key or ""))
    if not lab:
        raise KeyError(f"no such label font: {key!r}")
    return str(lab["latin"]), str(lab["arabic"])


def face_file(face_key: str) -> Path:
    """The TTF of a face key (what GET /icons/font/{key}.ttf serves);
    KeyError if unknown."""
    face = (manifest().get("faces") or {}).get(str(face_key or ""))
    if not face:
        raise KeyError(f"no such font: {face_key!r}")
    return FONTS_DIR / str(face["file"])


@dataclass(frozen=True)
class _Face:
    key: str
    upm: int
    glyphs: object                               # fontTools glyph set
    order: list
    cmap: dict
    hb_font: object                              # uharfbuzz Font
    ascent: int                                  # OS/2 typo metrics, font units
    descent: int
    cap: int


@lru_cache(maxsize=8)
def _face(face_key: str) -> _Face:
    import uharfbuzz as hb
    from fontTools.ttLib import TTFont

    path = face_file(face_key)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise FontError(f"the label font {path.name} is missing") from e
    want = manifest()["faces"][face_key]["sha256"]
    if hashlib.sha256(data).hexdigest() != want:
        # A label keyed to these bytes would silently draw differently.
        raise FontError(f"the label font {path.name} does not match fonts.json")
    ttf = TTFont(io.BytesIO(data))
    os2 = ttf["OS/2"]
    return _Face(key=face_key, upm=int(ttf["head"].unitsPerEm), glyphs=ttf.getGlyphSet(),
                 order=ttf.getGlyphOrder(), cmap=ttf.getBestCmap(),
                 hb_font=hb.Font(hb.Face(hb.Blob(data))),
                 ascent=int(os2.sTypoAscender), descent=int(-os2.sTypoDescender),
                 cap=int(getattr(os2, "sCapHeight", 0) or 0.7 * int(ttf["head"].unitsPerEm)))


# ---------------------------------------------------------------------------
# Bidi (minimal) and runs
# ---------------------------------------------------------------------------

def _is_arabic(ch: str) -> bool:
    c = ord(ch)
    return any(lo <= c <= hi for lo, hi in _ARABIC)


def _classes(text: str) -> list[str]:
    """Each character as "L", "R", "EN" (a number) or "N" (neutral): the
    Unicode bidi classes folded, marks taking their base's class, a
    separator between two numbers and a currency / percent sign next to one
    counted in the number (UBA W1, W4, W5)."""
    out: list[str] = []
    for ch in text:
        b = unicodedata.bidirectional(ch)
        if b == "L":
            c = "L"
        elif b in ("R", "AL"):
            c = "R"
        elif b in ("EN", "AN"):
            c = "EN"
        elif b == "NSM":
            c = out[-1] if out else "N"
        elif b in ("ES", "CS", "ET"):
            c = b
        else:
            c = "N"
        out.append(c)
    n = len(out)
    for i in range(1, n - 1):
        if out[i] in ("ES", "CS") and out[i - 1] == "EN" and out[i + 1] == "EN":
            out[i] = "EN"
    i = 0
    while i < n:
        if out[i] != "ET":
            i += 1
            continue
        j = i
        while j < n and out[j] == "ET":
            j += 1
        if (i > 0 and out[i - 1] == "EN") or (j < n and out[j] == "EN"):
            out[i:j] = ["EN"] * (j - i)
        i = j
    return ["N" if c in ("ES", "CS", "ET") else c for c in out]


def _levels(text: str) -> tuple[list[int], bool]:
    """Embedding levels (0 left-to-right line; in a right-to-left line 1 for
    right-to-left, 2 for Latin and numbers) and whether the line reads
    right-to-left (it holds a strong right-to-left letter)."""
    cls = _classes(text)
    if "R" not in cls:
        return [0] * len(text), False
    # W7: a number after Latin letters is Latin. N1 / N2: a run of neutrals
    # between two sides that agree takes that side (numbers count as right-
    # to-left), else the line's; the line's ends count as right-to-left.
    strong: list[str] = []
    last = "R"
    for c in cls:
        if c == "EN" and last == "L":
            c = "L"
        if c in ("L", "R"):
            last = c
        strong.append(c)
    side = ["R" if c == "EN" else c for c in strong]
    n = len(side)
    i = 0
    while i < n:
        if side[i] != "N":
            i += 1
            continue
        j = i
        while j < n and side[j] == "N":
            j += 1
        before = side[i - 1] if i > 0 else "R"
        after = side[j] if j < n else "R"
        fill = before if before == after else "R"
        side[i:j] = [fill] * (j - i)
        i = j
    levels = [2 if (s == "L" or strong[k] == "EN") else 1 for k, s in enumerate(side)]
    return levels, True


@dataclass(frozen=True)
class Run:
    """Shaped words in one face and one direction. `glyphs`: (glyph name,
    x advance, x offset, y offset) in em units (font units / upm), left to
    right as drawn."""
    text: str
    face: str
    rtl: bool
    glyphs: tuple[tuple[str, float, float, float], ...]


@lru_cache(maxsize=128)
def shape(text: str, key: str) -> tuple[tuple[Run, ...], bool]:
    """The words as runs in visual order (left to right) and whether the
    line reads right-to-left. KeyError for an unknown label key."""
    import uharfbuzz as hb

    latin_key, arabic_key = _label(key)
    text = unicodedata.normalize("NFC", str(text or ""))
    if not text:
        return (), False
    latin, arabic = _face(latin_key), _face(arabic_key)
    levels, rtl = _levels(text)

    def face_of(i: int, prev: Optional[str]) -> str:
        ch = text[i]
        if unicodedata.combining(ch) and prev is not None:
            return prev
        if _is_arabic(ch):
            return arabic_key
        if levels[i] % 2 == 1 and ord(ch) in arabic.cmap:
            return arabic_key                    # a space inside Arabic words
        if ord(ch) in latin.cmap:
            return latin_key
        return arabic_key if ord(ch) in arabic.cmap else latin_key

    # Segments of one level and one face, in logical order.
    segs: list[list] = []
    prev = None
    for i in range(len(text)):
        f = face_of(i, prev)
        prev = f
        if segs and segs[-1][0] == levels[i] and segs[-1][1] == f:
            segs[-1][2] += text[i]
        else:
            segs.append([levels[i], f, text[i]])
    # UBA L2: from the highest level down to 1, reverse every stretch at that
    # level or above (a segment's own letters are reversed by shaping it
    # right-to-left when its level is odd).
    top = max(s[0] for s in segs)
    for lvl in range(top, 0, -1):
        i = 0
        while i < len(segs):
            if segs[i][0] < lvl:
                i += 1
                continue
            j = i
            while j < len(segs) and segs[j][0] >= lvl:
                j += 1
            segs[i:j] = segs[i:j][::-1]
            i = j
    runs: list[Run] = []
    with _LOCK:
        for level, fkey, part in segs:
            face = latin if fkey == latin_key else arabic
            buf = hb.Buffer()
            buf.add_str(part)
            buf.direction = "rtl" if level % 2 else "ltr"
            buf.guess_segment_properties()
            hb.shape(face.hb_font, buf, FEATURES)
            u = float(face.upm)
            glyphs = tuple((face.order[info.codepoint], pos.x_advance / u, pos.x_offset / u, pos.y_offset / u)
                           for info, pos in zip(buf.glyph_infos, buf.glyph_positions))
            runs.append(Run(text=part, face=fkey, rtl=bool(level % 2), glyphs=glyphs))
    return tuple(runs), rtl


def units(text: str, key: str) -> float:
    """The words' advance in em units: the width at em px is units * em."""
    runs, _ = shape(text, key)
    total = 0.0
    for run in runs:
        for _name, adv, _dx, _dy in run.glyphs:
            total += adv
    return total


def metrics(key: str) -> dict:
    """The Latin face's vertical metrics in em units: {ascent, descent, cap}
    (the chip centres the cap height on its middle)."""
    face = _face(_label(key)[0])
    u = float(face.upm)
    return {"ascent": face.ascent / u, "descent": face.descent / u, "cap": face.cap / u}


def _num(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


@dataclass(frozen=True)
class TextPath:
    """Shaped words as SVG path data in px, the pen starting at x 0 on the
    baseline (y down). `advance` = units(text) * em."""
    d: str
    advance: float
    ascent: float
    descent: float
    cap: float
    rtl: bool


@lru_cache(maxsize=128)
def text_path(text: str, key: str, em: float) -> TextPath:
    """The words at an em of `em` px as path data (see TextPath). KeyError
    for an unknown label key, FontError for a missing or altered font."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen

    em = float(em)
    runs, rtl = shape(text, key)
    faces = {r.face: _face(r.face) for r in runs}
    pen_x = 0.0                                   # em units
    parts: list[str] = []
    with _LOCK:
        for run in runs:
            face = faces[run.face]
            k = em / face.upm
            for name, adv, dx, dy in run.glyphs:
                pen = SVGPathPen(face.glyphs, ntos=_num)
                face.glyphs[name].draw(TransformPen(pen, (k, 0, 0, -k, (pen_x + dx) * em, -dy * em)))
                d = pen.getCommands()
                if d:
                    parts.append(d)
                pen_x += adv
    m = metrics(key)
    return TextPath(d=" ".join(parts), advance=pen_x * em, ascent=m["ascent"] * em,
                    descent=m["descent"] * em, cap=m["cap"] * em, rtl=rtl)
