"""Your own icons, uploaded (rebrand_harness/icon_import.py + the team icon
routes): an SVG or a picture made into the one-colour 256 px team glyph,
saved as bytes in the shared table's row (no bucket), renamed, re-tagged or
hidden — and stamped like any icon.

The owner's checks: a safety corpus is refused (scripts, handlers, links
out, XXE, billion laughs, embedded pictures, style sheets, url() paint,
over-limits); the preview's bytes ARE the saved bytes; the same file twice
is one icon; the Stamper draws an upload; the uploaded SVG is never served.

No real model or database is ever touched: an autouse guard makes the paid
Vertex calls raise, the team-glyph store is an in-memory fake (one that even
hands back the kept SVG, so the routes are proven never to pass it on), and
caches go to tmp_path.
"""
from __future__ import annotations

import asyncio
import base64
import io
import re
import struct
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import vertex_client  # noqa: E402
from app.services.rebrand_harness import icon_catalog as ic  # noqa: E402
from app.services.rebrand_harness import icon_import as ii  # noqa: E402
from app.services.rebrand_harness import icon_render as ir  # noqa: E402
from app.services.rebrand_harness import label_font as lf  # noqa: E402
from app.services.rebrand_harness.elements import icon as icon_el  # noqa: E402

SVG_HEAD = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'


_T0 = datetime(2026, 9, 30, tzinfo=timezone.utc)


class FakeStore:
    """yas_rebrand_icons in memory — every column kept, `source` included
    (the real store never reads it back; this one does, the worst case)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.clock = 0
        self.down = False

    def _up(self):
        if self.down:
            raise ConnectionError("the database is down")

    async def by_id(self, icon_id):
        self._up()
        r = self.rows.get(icon_id)
        return dict(r) if r else None

    async def takes(self, name_key, origin):
        self._up()
        return sorted((dict(r) for r in self.rows.values() if r["name_key"] == name_key and r["origin"] == origin),
                      key=lambda r: (r["take"], r["created_at"]))

    async def insert(self, row):
        self._up()
        if row["id"] not in self.rows:
            self.clock += 1
            self.rows[row["id"]] = {"label": "", "tags": [], "origin": "ai", "take": 0, **row,
                                    # A datetime, as the database answers (the routes must encode it).
                                    "created_at": _T0 + timedelta(seconds=self.clock), "hidden_at": None}
        return dict(self.rows[row["id"]])

    async def set_hidden(self, icon_id, hidden):
        self._up()
        r = self.rows.get(icon_id)
        if not r or bool(r["hidden_at"]) == hidden:
            return False
        r["hidden_at"] = "now" if hidden else None
        return True

    async def update(self, icon_id, fields):
        self._up()
        r = self.rows.get(icon_id)
        if not r:
            return None
        r.update({k: (list(v) if k == "tags" else v) for k, v in fields.items() if k in ("name", "label", "tags")})
        return dict(r)

    async def listing(self, limit, include_hidden, origin=None):
        self._up()
        rows = [{k: v for k, v in r.items() if k not in ("png", "prompt", "report", "source")}
                for r in self.rows.values()
                if (include_hidden or not r["hidden_at"]) and (origin is None or r["origin"] == origin)]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    async def count_since(self, origin, hours):
        self._up()
        return sum(1 for r in self.rows.values() if r["origin"] == origin)


@pytest.fixture(autouse=True)
def guard(monkeypatch, tmp_path):
    """Paid calls raise; a fake store; caches in tmp_path."""
    calls: list[str] = []

    def refuse(name):
        async def _refuse(*a, **k):
            calls.append(name)
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    for name in ("gemini_multimodal", "gemini_image", "gemini_text"):
        if hasattr(vertex_client, name):
            monkeypatch.setattr(vertex_client, name, refuse(name))
    monkeypatch.setattr(ic, "CACHE_DIR", tmp_path / "icons_cache")
    store = FakeStore()
    old = ic.set_store(store)
    yield {"calls": calls, "store": store}
    ic.set_store(old)
    assert not calls, f"unexpected paid calls: {calls}"


def run(coro):
    return asyncio.run(coro)


def _png(arr: np.ndarray, fmt: str = "PNG", **kw) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format=fmt, **kw)
    return buf.getvalue()


def _alpha(png: bytes) -> np.ndarray:
    return ir.glyph_alpha(("png", png), ic.GLYPH_PX)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a > 0.5, b > 0.5
    return float((a & b).sum()) / max(1, float((a | b).sum()))


CODE_BOLD = ic.library_d("code", "bold")


def svg(body: str | bytes, head: bytes = SVG_HEAD) -> bytes:
    body = body.encode() if isinstance(body, str) else body
    return head + body + b"</svg>"


def picture(kind: str, px: int = 256) -> bytes:
    """The design probe's pictures: the bold "</>" in several dresses."""
    import cv2

    g = np.asarray(ir.path_alpha(CODE_BOLD, 160), np.float32)
    rgba = np.zeros((px, px, 4), np.uint8)
    if kind == "black_on_white":
        rgba[..., :3] = 255
        rgba[..., 3] = 255
        rgba[48:208, 48:208, :3] = (255 * (1 - g))[..., None].astype(np.uint8)
    elif kind == "white_on_clear":
        rgba[48:208, 48:208, :3] = 255
        rgba[48:208, 48:208, 3] = (255 * g).astype(np.uint8)
    elif kind == "glyph_on_plate":                   # a white "</>" on a violet disc, clear around
        cv2.circle(rgba, (128, 128), 110, (127, 43, 242, 255), -1, cv2.LINE_AA)
        sub = rgba[48:208, 48:208].astype(np.float32)
        sub[..., :3] = sub[..., :3] * (1 - g[..., None]) + 255 * g[..., None]
        rgba[48:208, 48:208] = sub.astype(np.uint8)
    elif kind == "two_colour_heart":                 # a heart with a small highlight: stays a heart
        cv2.circle(rgba, (100, 110), 50, (220, 40, 60, 255), -1)
        cv2.circle(rgba, (156, 110), 50, (220, 40, 60, 255), -1)
        cv2.fillPoly(rgba, [np.array([[55, 130], [201, 130], [128, 215]])], (220, 40, 60, 255))
        cv2.circle(rgba, (90, 95), 14, (255, 150, 170, 255), -1)
    elif kind == "photo":
        rng = np.random.default_rng(1)
        base = cv2.GaussianBlur(rng.integers(0, 255, (px, px, 3)).astype(np.uint8), (0, 0), 6)
        rgba[..., :3] = cv2.normalize(base, None, 0, 255, cv2.NORM_MINMAX)
        rgba[..., 3] = 255
    elif kind == "white_on_violet":                  # opaque: keyed off a dark ground
        rgba[..., :3] = (127, 43, 242)
        rgba[..., 3] = 255
        sub = rgba[48:208, 48:208, :3].astype(np.float32)
        rgba[48:208, 48:208, :3] = (sub * (1 - g[..., None]) + 255 * g[..., None]).astype(np.uint8)
    elif kind == "tight_disc":                       # a clear PNG cropped tight: touches all four edges
        cv2.circle(rgba, (128, 128), 128, (0, 0, 0, 255), -1, cv2.LINE_AA)
    elif kind == "tight_on_white":                   # the same, opaque: the art IS the border
        rgba[..., :3] = 255
        rgba[..., 3] = 255
        cv2.circle(rgba, (128, 128), 128, (0, 0, 0, 255), -1, cv2.LINE_AA)
    else:
        raise ValueError(kind)
    if kind == "white_on_violet":
        return _png(rgba[..., :3].copy(), "JPEG", quality=85)
    return _png(rgba)


def _png_claiming(w: int, h: int) -> bytes:
    """A tiny PNG whose header claims w x h pixels (a decompression bomb's
    shape: it is refused from the header, never decoded)."""
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# The safety corpus: refused, every one, before anything is drawn
# ---------------------------------------------------------------------------

LAUGHS = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
          + b"".join(b'<!ENTITY lol%d "%s">' % (i, (b"&lol%d;" % (i - 1) if i > 1 else b"&lol;") * 10)
                     for i in range(1, 10))
          + b']><svg xmlns="http://www.w3.org/2000/svg"><text>&lol9;</text></svg>')

SAFETY = {
    "script": (svg("<script>alert(1)</script><rect width='10' height='10'/>"), "script"),
    "script in defs": (svg("<defs><script>alert(1)</script></defs><rect width='10' height='10'/>"), "script"),
    "onload": (SVG_HEAD.replace(b">", b' onload="alert(1)">', 1) + b"<rect width='9' height='9'/></svg>",
               "event handler"),
    "onclick on a shape": (svg("<rect width='9' height='9' onclick='alert(1)'/>"), "event handler"),
    "xxe": (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///c:/windows/win.ini">]>'
            b'<svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>', "DOCTYPE"),
    "xxe, lower case": (b'<!doctype svg><svg xmlns="http://www.w3.org/2000/svg"/>', "DOCTYPE"),
    "billion laughs": (LAUGHS, "DOCTYPE"),
    "external image": (svg("<image xmlns:xlink='http://www.w3.org/1999/xlink' "
                           "xlink:href='http://evil.example/x.png' width='9' height='9'/>"), "picture inside"),
    "data image": (svg("<image href='data:image/png;base64,iVBORw0KGgo=' width='9' height='9'/>"),
                   "picture inside"),
    "javascript link": (svg("<a href='javascript:alert(1)'><rect width='9' height='9'/></a>"), "links to"),
    "local link": (svg("<a href='#top'><rect width='9' height='9'/></a>"), "link in it"),
    "use": (svg("<defs><rect id='r' width='9' height='9'/></defs><use href='#r'/>"), "reuses shapes"),
    "foreignObject": (svg("<foreignObject width='9' height='9'><div xmlns='http://www.w3.org/1999/xhtml'>"
                          "x</div></foreignObject>"), "embedded"),
    "style sheet": (svg("<defs><style>@import url(http://evil.example/x.css);</style></defs>"
                        "<rect width='9' height='9'/>"), "style sheet"),
    "url() paint": (svg("<rect width='9' height='9' style='fill:url(#g)'/>"), "gradient or pattern paint"),
    "url() paint attribute": (svg("<rect width='9' height='9' fill='url(#g)'/>"), "gradient or pattern paint"),
    "external url()": (svg("<rect width='9' height='9' clip-path='url(http://evil.example/c.svg#c)'/>"),
                       "links to"),
    "xinclude": (svg("<xi:include xmlns:xi='http://www.w3.org/2001/XInclude' href='file:///etc/passwd'/>"),
                 "links to"),
    "text": (svg("<text x='1' y='9'>Hi</text>"), "text in it"),
    "animation": (svg("<rect width='9' height='9'><animate attributeName='x' to='5' dur='1s'/></rect>"),
                  "animated"),
    "a gradient drawn": (svg("<linearGradient id='g'/><rect width='9' height='9'/>"), "gradient"),
    "a mask drawn": (svg("<mask id='m'/><rect width='9' height='9'/>"), "mask"),
    "nested svg": (svg("<svg viewBox='0 0 9 9'><rect width='9' height='9'/></svg>"), "another SVG"),
    "too many elements": (svg("<rect width='1' height='1'/>" * (ii.SVG_MAX_NODES + 1)), "too complex"),
    "too deep": (svg("<g>" * (ii.SVG_MAX_DEPTH + 2) + "<rect width='9' height='9'/>" + "</g>" * (ii.SVG_MAX_DEPTH + 2)),
                 "too complex"),
    "too much path": (svg("<path d='M0 0" + " L1 1" * 61000 + "'/>"), "too complex"),
    "utf-16": ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 9 9'/>".encode("utf-16"), "UTF-8"),
    "not xml": (b"<svg xmlns='http://www.w3.org/2000/svg'><rect", "could not be read"),
    "not an svg": (b"<html><body>hi</body></html>", "isn't an SVG"),
    "no size": (b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='9' height='9'/></svg>", "no size"),
    "empty": (svg(""), "empty"),
    "a photo": (picture("photo"), "background isn't plain"),
    "art on the border": (picture("tight_on_white"), "background isn't plain"),
    "not a picture": (b"GIF89a" + b"\x00" * 64, "Upload an SVG, PNG, WebP or JPEG"),
    "a disguised file": (b"\x89PNG\r\n\x1a\n" + b"nonsense" * 20, "could not be read"),
    "nothing": (b"", "empty"),
}


@pytest.mark.parametrize("name", list(SAFETY))
def test_the_safety_corpus_is_refused(name):
    data, why = SAFETY[name]
    with pytest.raises(ii.UploadError, match=re.escape(why)) as e:
        ii.import_upload(data, "x.svg")
    assert e.value.status in (413, 415, 422)


def test_over_the_limits_is_413_before_anything_is_read():
    big_svg = svg(b" " * (ii.SVG_MAX_BYTES + 1))
    with pytest.raises(ii.UploadError, match="512 KB") as e:
        ii.import_upload(big_svg, "big.svg")
    assert e.value.status == 413
    with pytest.raises(ii.UploadError, match="2 MB") as e:
        ii.import_upload(b"\x89PNG\r\n\x1a\n" + b"\x00" * ii.MAX_BYTES, "big.png")
    assert e.value.status == 413
    # A header claiming more pixels than allowed is refused from the header:
    # a decompression bomb is never decoded (whatever PIL's own guard says).
    for w, h in ((ii.MAX_SIDE + 1, 64), (50_000, 50_000)):
        with pytest.raises(ii.UploadError, match="larger than") as e:
            ii.import_upload(_png_claiming(w, h), "bomb.png")
        assert e.value.status == 413
    with pytest.raises(ii.UploadError, match="too small"):
        ii.import_upload(_png(np.zeros((8, 8, 4), np.uint8)), "tiny.png")


def test_a_small_svg_that_would_cost_gigabytes_to_draw_is_refused_before_it_is(monkeypatch):
    """Under 300 KB of path data can ask for hundreds of millions of fill
    crossings (a zig-zag of full-height edges: ~9 GB of arrays) or hours of
    thick strokes (a long run of tiny segments drawn wider than the icon).
    Both are refused as too complex from their count — the fillers and
    OpenCV are never reached."""
    from app.services.rebrand_harness import icon_render as ir

    def never(*a, **k):
        raise AssertionError("drawn before its cost was checked")

    monkeypatch.setattr(ir, "_winding_fill", never)
    monkeypatch.setattr(ii, "_evenodd", never)
    zigzag = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><path d="M0 0'
              + b"v1v-1" * 59_000 + b'z"/></svg>')
    with pytest.raises(ii.UploadError, match="too complex"):
        ii.import_upload(zigzag, "zigzag.svg")
    thick = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><path fill="none" stroke="#000" '
             b'stroke-width="40" d="M0 0' + b"l.001 0" * 30_000 + b'"/></svg>')
    import cv2

    monkeypatch.setattr(cv2, "polylines", never)
    with pytest.raises(ii.UploadError, match="too complex"):
        ii.import_upload(thick, "thick.svg")


def test_a_stroke_wider_than_opencv_draws_is_drawn_not_a_crash():
    """One short stroke wider than OpenCV's 32767 px limit covers the whole
    viewBox, as a browser shows it: drawn at that limit (one solid shape),
    never an OpenCV error (a 500)."""
    wide = svg("<path fill='none' stroke='#000' stroke-width='100000' d='M40 50h20'/>")
    got = ii.import_upload(wide, "wide.svg")
    assert got.report["svg"]["strokes"] == 1 and got.report["shapes"] == 1
    assert got.report["weight"] == "solid"


def test_the_parser_never_reads_a_dtd_or_the_network(monkeypatch):
    """Belt and braces: even if the DOCTYPE check were bypassed, lxml is
    asked for no DTD, no entities and no network."""
    from lxml import etree

    seen = {}
    real = etree.XMLParser

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(etree, "XMLParser", spy)
    ii.import_upload(svg("<rect x='10' y='10' width='80' height='40'/>"), "r.svg")
    assert seen["resolve_entities"] is False and seen["no_network"] is True and seen["load_dtd"] is False
    assert seen["huge_tree"] is False


# ---------------------------------------------------------------------------
# What an SVG draws
# ---------------------------------------------------------------------------

def test_phosphor_bold_comes_out_as_the_library_draws_it():
    got = ii.import_upload(svg(f'<path d="{CODE_BOLD}"/>',
                               b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">'), "code-bold.svg")
    lib = ic._squared(np.asarray(ir.path_alpha(CODE_BOLD, 1024), np.float32), ic.GLYPH_PX, ic.GLYPH_MARGIN)
    assert _iou(_alpha(got.png), lib) > 0.93
    assert got.report["weight"] == "kept" and got.report["shapes"] == 3 and got.kind == "svg"
    assert got.report["svg"]["fills"] == 1 and got.report["via"] == "shapes"


def test_a_lucide_stroke_icon_is_drawn_with_its_strokes():
    lucide = (b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
              b'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
              b'<path d="M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08a1 1 0 0 0 0 '
              b'1.832l8.57 3.908a2 2 0 0 0 1.66 0z"/><path d="M22 10v6"/><path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5"/>'
              b'</svg>')
    got = ii.import_upload(lucide, "graduation-cap.svg")
    assert got.report["svg"]["strokes"] == 3 and got.report["svg"]["fills"] == 0
    assert got.report["shapes"] == 1 and 0.05 < got.report["ink"] < 0.5
    assert got.name_guess == "Graduation cap"


def test_transforms_and_a_knock_out():
    """A rotated rounded square with a white disc drawn over it: the disc is
    a hole (one colour), and the square stands on its corner."""
    got = ii.import_upload(svg('<g transform="translate(50 50) rotate(45)"><rect x="-30" y="-30" width="60" '
                               'height="60" rx="8" fill="#222"/><circle r="14" fill="#fff"/></g>'), "k.svg")
    a = _alpha(got.png)
    assert a[128, 128] < 0.05                                         # the knock-out
    assert a[128, 40] > 0.9 and a[40, 128] > 0.9                      # the diamond's own ink
    assert a[40, 40] < 0.05                                           # its corners are the points
    assert got.report["svg"]["knockouts"] == 1


def test_a_filled_icon_keeps_the_knock_out_near_its_edge():
    """A filled folder with a star knocked out near its bottom edge: made to
    Phosphor bold's weight it is thinned, but the star never breaks through
    the folder's edge (one piece of ink, one hole, as drawn)."""
    folder = ('<path d="M8 18a4 4 0 0 1 4-4h14l6 6h20a4 4 0 0 1 4 4v24a4 4 0 0 1-4 4H12a4 4 0 0 1-4-4z"/>'
              '<path fill="#fff" d="M32 26l3.1 6.3 6.9 1-5 4.9 1.2 6.8L32 41.8 25.8 45l1.2-6.8-5-4.9 6.9-1z"/>')
    got = ii.import_upload(svg(folder, b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'), "f.svg")
    assert got.report["svg"]["knockouts"] == 1 and got.report["weight"] == "thinner"
    assert ic._pieces(_alpha(got.png)) == (1, 1)


def test_even_odd_and_nonzero_fills():
    ring = "M10 10H90V90H10Z M30 30H70V70H30Z"                        # both squares turn the same way
    eo = _alpha(ii.import_upload(svg(f'<path fill-rule="evenodd" d="{ring}"/>'), "eo.svg").png)
    nz = _alpha(ii.import_upload(svg(f'<path d="{ring}"/>'), "nz.svg").png)
    assert eo[128, 128] < 0.05 and eo[128, 30] > 0.9                  # a frame
    assert nz[128, 128] > 0.9                                         # a filled square


def test_an_svg_painted_only_in_white_is_white_on_clear():
    got = ii.import_upload(svg('<path fill="#fff" d="M10 10h80v20H10zM10 50h50v20H10z"/>'), "w.svg")
    assert got.report["svg"]["white_ink"] is True and got.report["shapes"] == 2


def test_faint_layers_are_left_off_and_hidden_ones_skipped():
    plain = ii.import_upload(svg('<path d="M20 20h60v60H20z"/>'), "a.svg")
    duotone = ii.import_upload(svg('<rect width="100" height="100" opacity="0.2"/><path d="M20 20h60v60H20z"/>'
                                   '<rect width="10" height="10" display="none"/>'
                                   '<rect x="90" width="10" height="10" visibility="hidden"/>'), "b.svg")
    assert duotone.png == plain.png and duotone.report["svg"]["faint"] == 1
    # Group opacity multiplies down the tree.
    grouped = ii.import_upload(svg('<g opacity="0.5"><rect width="100" height="100" fill-opacity="0.5"/></g>'
                                   '<path d="M20 20h60v60H20z"/>'), "c.svg")
    assert grouped.png == plain.png


def test_it_is_clipped_to_its_viewbox():
    inside = ii.import_upload(svg('<rect x="20" y="20" width="80" height="60"/>'), "in.svg")
    spills = ii.import_upload(svg('<rect x="20" y="20" width="400" height="60"/>'), "out.svg")
    assert spills.png == inside.png


def test_other_programs_notes_and_figma_clips_are_harmless():
    """Inkscape's namespaced notes are skipped; a Figma export's frame clip
    (clip-path="url(#...)" to a clipPath in <defs>) is ignored, not refused;
    a gradient only defined, never painted, is fine."""
    figma = (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/'
             b'sodipodi-0.dtd" width="24" height="24" viewBox="0 0 24 24" fill="none">'
             b'<sodipodi:namedview pagecolor="#ffffff"/><g clip-path="url(#clip0)">'
             b'<path d="M4 4h16v16H4z" fill="#303030"/></g><defs><clipPath id="clip0">'
             b'<rect width="24" height="24" fill="white"/></clipPath><linearGradient id="unused"/></defs></svg>')
    got = ii.import_upload(figma, "frame.svg")
    assert got.report["svg"]["fills"] == 1 and "namedview" in got.report["svg"]["skipped"]


def test_the_same_file_is_the_same_bytes_and_id():
    data = svg('<circle cx="50" cy="50" r="30"/>')
    a, b = ii.import_upload(data, "a.svg"), ii.import_upload(data, "b.svg")
    assert a.png == b.png and a.id == b.id == ii.upload_id(a.png)
    assert re.fullmatch(r"c-[0-9a-f]{12}", a.id) and ic.is_custom_id(a.id)
    import hashlib

    assert a.id == "c-" + hashlib.sha256(("upload|" + hashlib.sha256(a.png).hexdigest()).encode()).hexdigest()[:12]
    assert a.source_sha == hashlib.sha256(data).hexdigest()
    # The one glyph format: a GLYPH_PX RGBA PNG, black ink, alpha = shape.
    from PIL import Image

    im = Image.open(io.BytesIO(a.png))
    assert im.size == (ic.GLYPH_PX, ic.GLYPH_PX) and im.mode == "RGBA"
    assert np.asarray(im)[..., :3].max() == 0


@pytest.mark.parametrize("filename, want", [
    ("graduation-cap_bold.svg", "Graduation cap"), ("GraduationCap.png", "Graduation cap"),
    ("ic_rocket_24px.svg", "Rocket"), ("42 Piscine logo@2x.png", "Piscine logo"),
    ("C:\\Users\\me\\Desktop\\UAE-flag.svg", "UAE flag"), ("icon.svg", ""), ("", ""),
])
def test_the_name_is_guessed_from_the_file_name(filename, want):
    assert ii.name_guess(filename) == want


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

def test_pictures_are_keyed_by_transparency_or_their_background():
    ref = _alpha(ii.import_upload(picture("black_on_white"), "a.png").png)
    for kind, via in (("black_on_white", "light page"), ("white_on_clear", "transparency"),
                      ("white_on_violet", "dark ground")):
        got = ii.import_upload(picture(kind), kind + ".png")
        assert got.report["via"] == via and got.report["plate"] is None, kind
        assert _iou(_alpha(got.png), ref) > 0.9, kind
    assert ii.import_upload(picture("white_on_violet"), "x.jpg").kind == "jpeg"


def test_the_plate_rule():
    """A white "</>" on a violet disc: the marks are the icon (auto), the
    disc is kept on "shape", and a heart with a small highlight keeps its
    highlight as part of the heart."""
    ref = _alpha(ii.import_upload(picture("black_on_white"), "a.png").png)
    auto = ii.import_upload(picture("glyph_on_plate"), "p.png")
    assert auto.report["plate"] == "dropped" and _iou(_alpha(auto.png), ref) > 0.9
    marks = ii.import_upload(picture("glyph_on_plate"), "p.png", mode="marks")
    assert marks.png == auto.png
    shape = ii.import_upload(picture("glyph_on_plate"), "p.png", mode="shape")
    assert shape.report["plate"] == "kept" and shape.png != auto.png
    a = _alpha(shape.png)
    assert a[128, 128] > 0.9 and shape.report["weight"] == "solid"     # the whole disc
    heart = ii.import_upload(picture("two_colour_heart"), "h.png")
    assert heart.report["plate"] == "kept"                             # offered, not done
    with pytest.raises(ii.UploadError, match="no marks"):
        ii.import_upload(picture("white_on_clear"), "w.png", mode="marks")
    with pytest.raises(ii.UploadError, match="clear background"):
        ii.import_upload(picture("black_on_white"), "b.png", mode="marks")


def test_a_clear_picture_may_touch_its_edges():
    """A clear PNG cropped tight to its art is the art: never "edge to edge"."""
    got = ii.import_upload(picture("tight_disc"), "dot.png")
    assert got.report["via"] == "transparency" and got.report["edges"] == 0


def test_a_big_picture_is_read_small():
    import cv2

    big = np.full((3000, 3000, 3), 255, np.uint8)
    cv2.circle(big, (1500, 1500), 900, (0, 0, 0), 120)
    got = ii.import_upload(_png(big), "ring.png")
    assert got.report["shapes"] == 1 and got.report["via"] == "light page"
    assert _alpha(got.png)[128, 128] < 0.05 and _alpha(got.png)[128, 20] > 0.9


# ---------------------------------------------------------------------------
# The team's icons: preview, save, rename, re-tag, hide — and stamp
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app)


URL = "/api/rebrand/harness/icons"
MARKER = "i11-secret-marker"
SECRET_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><title>' + MARKER.encode()
              + b'</title><g id="' + MARKER.encode() + b'"><path d="M15 50 L50 15 L85 50 L50 85 Z"/></g></svg>')


def _data_png(url: str) -> bytes:
    assert url.startswith("data:image/png;base64,")
    return base64.b64decode(url.split(",", 1)[1])


def test_the_preview_is_free_and_is_the_saved_icon(client, guard):
    files = {"file": ("piscine-badge.svg", SECRET_SVG, "image/svg+xml")}
    got = client.post(f"{URL}/upload/preview", files=files)
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == {"id", "png_url", "preview", "report", "name_guess", "kind", "duplicate_of"}
    assert body["kind"] == "svg" and body["name_guess"] == "Piscine badge" and body["duplicate_of"] is None
    assert body["preview"]["circle"].startswith("data:image/png") and body["preview"]["chip"].startswith("data:")
    assert guard["store"].rows == {}                                   # nothing saved
    shown = _data_png(body["png_url"])
    saved = client.post(f"{URL}/upload", files=files, data={"name": "Piscine", "tags": "pool, Swim ,pool"})
    assert saved.status_code == 201, saved.text
    row = saved.json()
    assert row["id"] == body["id"] and row["origin"] == "upload" and row["duplicate"] is False
    assert row["name"] == "Piscine" and row["label"] == "Piscine" and row["tags"] == ["pool", "swim"]
    assert row["png_url"] == f"{URL}/glyph/{row['id']}" and row["source"] == "custom"
    # Preview bytes == saved bytes == what every machine draws.
    stored = guard["store"].rows[row["id"]]
    assert stored["png"] == shown and client.get(row["png_url"]).content == shown
    assert stored["source_kind"] == "svg" and stored["source"] == SECRET_SVG and stored["created_by"] is not None
    # Previewed again: it is your team's already.
    again = client.post(f"{URL}/upload/preview", files=files).json()
    assert again["duplicate_of"] == {"id": row["id"], "name": "Piscine", "hidden": False}
    # The chip carries the words asked for.
    worded = client.post(f"{URL}/upload/preview", files=files, data={"label": "Piscine"}).json()
    assert worded["preview"]["chip"] != body["preview"]["chip"] and worded["png_url"] == body["png_url"]
    chip = ir.make_sprite("chip", ("png", shown), icon_el.chip_height(ic.PREVIEW_PX), "#7f2bf2", "#ffffff",
                          text="Piscine", font=lf.default_key(), look_v=1).rgba
    from PIL import Image

    assert np.array_equal(np.asarray(Image.open(io.BytesIO(_data_png(worded["preview"]["chip"]))).convert("RGBA")),
                          chip)


def test_one_file_is_made_into_its_glyph_once(client, guard, monkeypatch):
    """The upload card asks again each time the chip's words pause, and Save
    sends the same file: it is made into its glyph once (the importer is
    pure) — another mode is another picture, made again."""
    monkeypatch.setattr(ic, "_IMPORTS", {})
    made: list[str] = []
    real = ii.import_upload

    def counting(data, filename="", **kw):
        made.append(filename)
        return real(data, filename, **kw)

    monkeypatch.setattr(ii, "import_upload", counting)
    files = {"file": ("pool.svg", SECRET_SVG, "image/svg+xml")}
    shown = [client.post(f"{URL}/upload/preview", files=files, data={"label": w}).json() for w in ("P", "Po", "Pool")]
    saved = client.post(f"{URL}/upload", files=files, data={"name": "Pool"})
    assert saved.status_code == 201 and saved.json()["id"] == shown[0]["id"]
    assert made == ["pool.svg"] and len({s["png_url"] for s in shown}) == 1
    assert len({s["preview"]["chip"] for s in shown}) == 3, "each with its own words"
    client.post(f"{URL}/upload/preview", files=files, data={"mode": "shape"})
    assert len(made) == 2


def test_the_same_file_twice_is_one_icon(client, guard):
    files = {"file": ("x.png", picture("black_on_white"), "image/png")}
    first = client.post(f"{URL}/upload", files=files, data={"name": "Code"})
    second = client.post(f"{URL}/upload", files={"file": ("other-name.png", picture("black_on_white"), "image/png")},
                         data={"name": "Something else", "tags": "x"})
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["id"] == first.json()["id"] and second.json()["duplicate"] is True
    assert second.json()["name"] == "Code"                            # its own name kept
    assert len(guard["store"].rows) == 1
    # Hidden, then uploaded again: shown again, still one row.
    assert client.delete(f"{URL}/custom/{first.json()['id']}").status_code == 200
    assert client.get(f"{URL}/team").json()["total"] == 0
    back = client.post(f"{URL}/upload", files=files, data={"name": "Code"})
    assert back.status_code == 200 and back.json()["hidden"] is False and len(guard["store"].rows) == 1
    assert client.get(f"{URL}/team").json()["total"] == 1
    # A different mode is a different picture, so a different icon.
    plate = client.post(f"{URL}/upload", files={"file": ("p.png", picture("glyph_on_plate"), "image/png")},
                        data={"name": "Plate", "mode": "shape"})
    assert plate.status_code == 201 and plate.json()["id"] != first.json()["id"]


def test_rename_retag_and_hide_keep_the_id_and_pixels(client, guard):
    row = client.post(f"{URL}/upload", files={"file": ("x.svg", SECRET_SVG, "image/svg+xml")},
                      data={"name": "Pool"}).json()
    gid, png = row["id"], guard["store"].rows[row["id"]]["png"]
    renamed = client.patch(f"{URL}/custom/{gid}", json={"name": "  The   piscine  "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["id"] == gid and renamed.json()["name"] == "The piscine"
    assert renamed.json()["label"] == "The piscine"
    tagged = client.patch(f"{URL}/custom/{gid}", json={"tags": ["Moulinette", "42 exam", "moulinette", ""]}).json()
    assert tagged["tags"] == ["moulinette", "42 exam"] and tagged["name"] == "The piscine"
    labelled = client.patch(f"{URL}/custom/{gid}", json={"label": "Piscine"}).json()
    assert labelled["label"] == "Piscine" and labelled["name"] == "The piscine"
    assert guard["store"].rows[gid]["png"] == png and client.get(f"{URL}/glyph/{gid}").content == png
    assert guard["store"].rows[gid]["name_key"] == "pool"             # what it was made as never moves
    # Found by its new name and by a tag, on this machine at once.
    for q in ("the piscine", "moulinette"):
        found = client.get(f"{URL}/search", params={"q": q}).json()
        assert found["items"][0]["id"] == gid, q
        assert found["items"][0]["origin"] == "upload" and found["items"][0]["png_url"] == f"{URL}/glyph/{gid}"
    # Hidden and shown again, for everyone.
    assert client.patch(f"{URL}/custom/{gid}", json={"hidden": True}).json()["hidden"] is True
    assert client.get(f"{URL}/team").json()["total"] == 0
    assert client.get(f"{URL}/glyph/{gid}").status_code == 200        # versions still draw it
    assert client.patch(f"{URL}/custom/{gid}", json={"hidden": False}).json()["hidden"] is False
    assert [i["id"] for i in client.get(f"{URL}/team").json()["items"]] == [gid]
    # Refusals.
    assert client.patch(f"{URL}/custom/{gid}", json={"name": "   "}).status_code == 422
    assert client.patch(f"{URL}/custom/c-000000000000", json={"name": "x"}).status_code == 404
    assert client.patch(f"{URL}/custom/code", json={"name": "x"}).status_code == 404
    guard["store"].down = True
    assert client.patch(f"{URL}/custom/{gid}", json={"name": "x"}).status_code == 503


def test_upload_refusals(client, guard):
    bad = client.post(f"{URL}/upload/preview", files={"file": ("x.svg", SAFETY["script"][0], "image/svg+xml")})
    assert bad.status_code == 422 and "script" in bad.json()["detail"]
    gif = client.post(f"{URL}/upload", files={"file": ("x.gif", b"GIF89a" + b"\x00" * 64, "image/gif")},
                      data={"name": "x"})
    assert gif.status_code == 415
    big = client.post(f"{URL}/upload/preview",
                      files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * ii.MAX_BYTES, "image/png")})
    assert big.status_code == 413
    unnamed = client.post(f"{URL}/upload", files={"file": ("x.png", picture("black_on_white"), "image/png")},
                          data={"name": "   "})
    assert unnamed.status_code == 422 and "name" in unnamed.json()["detail"]
    assert client.post(f"{URL}/upload", files={"file": ("x.png", picture("black_on_white"), "image/png")}
                       ).status_code == 422                            # no name at all
    assert client.post(f"{URL}/upload/preview", files={"file": ("x.png", picture("black_on_white"), "image/png")},
                       data={"mode": "nonsense"}).status_code == 422
    assert guard["store"].rows == {}
    # The table down: the preview still works (no duplicate check), the save says so.
    guard["store"].down = True
    ok = client.post(f"{URL}/upload/preview", files={"file": ("x.png", picture("black_on_white"), "image/png")})
    assert ok.status_code == 200 and ok.json()["duplicate_of"] is None
    down = client.post(f"{URL}/upload", files={"file": ("x.png", picture("black_on_white"), "image/png")},
                       data={"name": "x"})
    assert down.status_code == 503 and down.headers["retry-after"] == str(ic.DRAW_DOWN_SEC)


def test_the_uploaded_svg_is_never_served(client, guard):
    """Every route a studio reads, after an SVG is uploaded, previewed,
    renamed and searched: none carries the file (its marker, or any SVG) —
    only our PNG is served, and no store read ever selects `source`."""
    files = {"file": ("secret.svg", SECRET_SVG, "image/svg+xml")}
    answers = [client.post(f"{URL}/upload/preview", files=files),
               client.post(f"{URL}/upload", files=files, data={"name": "Secret", "tags": "hidden"})]
    gid = answers[-1].json()["id"]
    answers += [client.post(f"{URL}/upload/preview", files=files),
                client.patch(f"{URL}/custom/{gid}", json={"name": "Renamed"}),
                client.get(f"{URL}/team", params={"include_hidden": True}),
                client.get(f"{URL}/search", params={"q": "renamed"}),
                client.get(f"{URL}/search", params={"q": ""}),
                client.get(f"{URL}/catalogue"),
                client.get(f"{URL}/glyph/{gid}"),
                client.get(f"{URL}/glyph/{gid}", params={"weight": "bold"})]
    for r in answers:
        assert r.status_code in (200, 201), r.text
        raw = r.content
        assert MARKER.encode() not in raw and b"<svg" not in raw and b"M15 50" not in raw, r.url
    glyph = answers[-2]
    assert glyph.headers["content-type"] == "image/png" and glyph.content[:4] == b"\x89PNG"
    assert guard["store"].rows[gid]["source"] == SECRET_SVG            # kept, to make it again
    for cols in (ic._COLS, ic._PUB_COLS):
        assert not re.search(r"\bsource\b(?!_)", cols)
    # A picture's own file is not kept at all.
    pic = client.post(f"{URL}/upload", files={"file": ("x.png", picture("black_on_white"), "image/png")},
                      data={"name": "Pic"}).json()
    assert guard["store"].rows[pic["id"]]["source"] is None and guard["store"].rows[pic["id"]]["source_sha"]


def test_a_big_svg_is_not_kept_with_its_icon():
    small = svg('<circle cx="50" cy="50" r="30"/>')
    assert ii.kept_source(small, "svg") == small
    padded = svg(b'<circle cx="50" cy="50" r="30"/>' + b" " * ii.SVG_KEEP_BYTES)
    assert ii.kept_source(padded, "svg") is None and ii.kept_source(small, "png") is None


def test_the_stamper_draws_an_upload(client, guard):
    """Saved on one machine, stamped by the run: glyphs_for reads the row's
    bytes (not a bucket), and the circle is exactly the sprite made from the
    very PNG the preview showed."""
    row = client.post(f"{URL}/upload", files={"file": ("x.svg", SECRET_SVG, "image/svg+xml")},
                      data={"name": "Diamond"}).json()
    gid, png = row["id"], guard["store"].rows[row["id"]]["png"]
    for p in (ic.CACHE_DIR / "custom").glob("*.png"):
        p.unlink()                                                     # another machine: nothing cached
    glyphs = run(ic.glyphs_for([{"glyph": {"source": "custom", "id": gid}}]))
    assert glyphs == {gid: ("png", png)}
    item = {"glyph": {"source": "custom", "id": gid}, "from_sec": 0, "settle_sec": 0.27, "to_sec": 1,
            "fill": "#7f2bf2", "ink": "#ffffff", "box": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5},
            "style": "circle", "weight": "bold", "look_v": 1, "fx": {"entrance": "none", "seed": "s"}}
    st = ir.Stamper([item], frame_size=(200, 200), glyphs=glyphs)
    (rgba, x, y), = st.drawn(0.5)
    want = ir.make_sprite("circle", ("png", png), 100, "#7f2bf2", "#ffffff", look_v=1)
    assert np.array_equal(rgba, want.rgba) and (x, y) == (50 - want.pad, 50 - want.pad)
    frame = np.zeros((200, 200, 3), np.uint8)
    stamped = st.under(frame, 0.5)
    assert not np.array_equal(stamped, frame)
    # Hidden for everyone: still stamped (versions that use it).
    client.delete(f"{URL}/custom/{gid}")
    assert run(ic.glyphs_for([item])) == {gid: ("png", png)}


def test_the_catalogue_says_what_can_be_uploaded(client):
    cat = client.get(f"{URL}/catalogue").json()
    assert cat["upload"] == {"max_bytes": ii.MAX_BYTES, "svg_max_bytes": ii.SVG_MAX_BYTES,
                             "kinds": ["svg", "png", "webp", "jpeg"], "name_max": 40, "tags_max": 12,
                             "tag_max": 24}


def test_uploads_do_not_count_against_the_days_drawings(client, guard):
    client.post(f"{URL}/upload", files={"file": ("x.png", picture("black_on_white"), "image/png")},
                data={"name": "Code"})
    rows = list(guard["store"].rows.values())
    assert [r["origin"] for r in rows] == ["upload"]
    # The day's cap counts origin 'ai' only (the store is asked for 'ai').
    assert run(ic.draws_left()) == ic.draws_per_day()


def test_the_postgres_store_updates_and_keeps_the_file(monkeypatch):
    """PgStore.update and insert against a recording fake connection (never
    the real database): only name / label / tags are ever set, with matching
    placeholders; the upload's file hash and SVG ride on the insert; no
    SELECT ever reads `source`."""
    from contextlib import asynccontextmanager

    from app import db

    log: list[tuple] = []
    row = {"id": "c-aaaaaaaaaaaa", "name": "Pool", "name_key": "pool", "label": "Pool", "tags": [],
           "origin": "upload", "take": 0, "png": b"\x89PNG", "prompt": "", "report": None, "source_kind": "svg",
           "created_by": "pc", "created_at": None, "updated_at": None, "hidden_at": None}

    class Cur:
        rowcount = 1

        def __init__(self):
            self._got = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, args=()):
            assert sql.count("%s") == len(args), sql
            log.append((" ".join(sql.split()), tuple(args)))
            self._got = [dict(row)]

        async def fetchall(self):
            return list(self._got)

    class Conn:
        def cursor(self):
            return Cur()

    @asynccontextmanager
    async def fake_get_conn(*, autocommit=False):
        yield Conn()

    monkeypatch.setattr(db, "get_conn", fake_get_conn)
    store = ic.PgStore()
    store._ready = True

    async def go():
        await store.insert({"id": "c-aaaaaaaaaaaa", "name": "Pool", "name_key": "pool", "png": b"x", "tags": ["a"],
                            "origin": "upload", "source_kind": "svg", "source_sha": "ab" * 32, "source": b"<svg/>"})
        await store.update("c-aaaaaaaaaaaa", {"name": "Piscine", "label": "Piscine", "tags": ("swim",),
                                              "png": b"never", "id": "c-bbbbbbbbbbbb", "name_key": "never"})
        return await store.update("c-aaaaaaaaaaaa", {})

    assert run(go())["id"] == "c-aaaaaaaaaaaa"
    inserted = next(a for s, a in log if s.startswith("INSERT"))
    assert inserted[-2:] == ("ab" * 32, b"<svg/>") and inserted[5] == "upload"
    updates = [(s, a) for s, a in log if s.startswith("UPDATE")]
    assert len(updates) == 1
    sql, args = updates[0]
    assert sql == ("UPDATE yas_rebrand_icons SET name = %s, label = %s, tags = %s, updated_at = now() "
                   "WHERE id = %s")
    assert args == ("Piscine", "Piscine", ["swim"], "c-aaaaaaaaaaaa")
    assert all(not re.search(r"\bsource\b(?!_)", s.split("FROM")[0]) for s, _ in log if s.startswith("SELECT"))
