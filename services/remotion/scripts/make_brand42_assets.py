"""
Build the static assets the `brand-overlay-42` composition loads with staticFile().

    python services/remotion/scripts/make_brand42_assets.py \
        [--logo <stacked white lockup PNG>] \
        [--vg5000 <VG5000-Regular_web.woff2> --vg5000-licence <LICENSE.txt>] \
        [--jost <Jost[wght].ttf> --jost-licence <OFL.txt>] \
        [--patterns <pattern kit dir, e.g. backend/data/branding/patterns>] \
        [--icons]      (downloads Phosphor Icons from cdn.jsdelivr.net)
        [--icon-catalog]  (the whole Phosphor fill set, for the backend)
        [--icon-catalog-bold]  (the same ids in Phosphor's bold weight)
        [--label-fonts]   (Jost + Noto Kufi Arabic SemiBold for icon chips)

Everything lands in services/remotion/public/brand42/ and IS committed: the
sources are not (backend/data/ and the research kit are git-ignored), so a
fresh checkout must be able to render without re-running this. A render
never touches the network: every font and image it draws ships here.

BEADS (nothing to build)
    42's reels string pattern 1 ("beads_row") glyphs along their 45-degree slab
    edges. They used to be sprites cut from patterns/1.png, but pattern 1 is
    pixel art: a 160x80 sprite shrunk ~8x under a 45-degree rotation came out
    with stair-stepped, speckled outlines, where r128's glyphs are smooth. They
    are now SVG vectors drawn by src/brand42/beads.ts (glyphPath), sized and
    shaped from r128's own glyphs; the string layout is data in the same file.

LOGO (--logo)
    The reels use the STACKED lockup. The repo only has the horizontal one
    (backend/app/assets/branding/42_logo_white.png); the stacked PNG was recomposed
    from it during the 2026-09-23 brand research (IoU 0.91 vs the reel's).
    This step only forces its colour to pure #FFFFFF (the recomposition left
    240-254 greys inside the ink) while keeping its alpha.

PATTERNS (--patterns)
    The owner's five pattern sheets (patterns/1-5.png) slide left to right
    through the letterbox bars and over the picture's left edge. The source
    art is 8k-16k px; this reads the pattern kit's 2400 px masks (built from
    those sheets by backend/app/services/pattern_kit.py: white RGB, alpha =
    the shape) and writes one TILE per pattern and colour that repeats with no
    seam in BOTH directions, so the composition can scroll it for any length
    of reel and stack it for any bar height:
      "mirror"  the art, its mirror image to the right, and both flipped
                below: every line meets its own reflection at the joins.
                For line art and bead rows, which read the same mirrored.
      "spaced"  the art alone with a transparent gap: copies pass by one after
                another. For "structure", whose Arabic letterforms must never
                be mirrored.
    Each tile is ONE colour, so it is stored as an 8-bit palette PNG whose 256
    entries are that colour at alpha 0..255 (lossless for this art, a fraction
    of the RGBA size). Sizes are in output px at 1080 wide; the composition
    scales them with the frame. Tile sizes go to src/brand42/patternTiles.ts.

ICONS (--icons)
    Topic icons are Phosphor Icons, "fill" weight (MIT, (c) 2023 Phosphor
    Icons), downloaded once from the npm package on jsdelivr. Only the SVG path
    data of the icons in ICON_IDS is kept, in src/brand42/icons.ts, with the
    licence in public/brand42/ICONS-LICENSE.txt. The backend's keyword table
    (backend/app/services/brand_icons.py) may only name ids from ICON_IDS; a
    test checks it.

ICON CATALOGUE (--icon-catalog)
    The Rebrand studio's icon library: EVERY Phosphor "fill" icon of the same
    npm release (~1,430 after the cut below), with its name, tags and
    categories from the package's own metadata (dist/index.mjs), written to
    backend/app/assets/brand42/phosphor_fill.json (+ ICONS-LICENSE.txt there)
    for backend/app/services/rebrand_harness/icon_catalog.py. It is committed:
    the backend never downloads icons at run time. Brand logos (the "brands"
    category and every *-logo id) are left out, so a third-party trademark
    can never reach a 42 reel. Shapes other than <path> are converted to path
    data; the result is rasterised once with the backend's own renderer
    (icon_render.path_alpha) as a self-test. If the CDN cannot be reached it
    falls back to what is vendored (src/brand42/icons.ts, plus the metadata of
    a local node_modules/@phosphor-icons/core when there is one) and says so.

BOLD ICON CATALOGUE (--icon-catalog-bold)
    The studio's circle and chip icons draw Phosphor's BOLD weight (thick
    strokes, the "</>" of the owner's style sheet). The same ids as
    phosphor_fill.json, bold path data only (tags stay in the fill file),
    written to backend/app/assets/brand42/phosphor_bold.json. It is a
    SEPARATE file on purpose: phosphor_fill.json is never touched, because
    its hash keys the icon director's paid answers (a new fill file would
    ask the AI again for every scene). Self-tested with the backend's
    renderer; an id whose bold SVG cannot be read is listed as missing and
    the backend draws its fill shape instead.

LABEL FONTS (--label-fonts)
    The words on a chip icon: Jost SemiBold (a static wght 600 instance of
    the brand's own Jost[wght]) for Latin, and Noto Kufi Arabic SemiBold
    (wght 600) for Arabic, both SIL OFL 1.1, downloaded from google/fonts,
    instanced with fontTools and written as TTF to
    backend/app/assets/brand42/fonts/ with their OFL texts and fonts.json:
    each face keyed by its file's sha8 ("jost-600@1a2b3c4d") and the label
    face (a Latin face + an Arabic one) keyed by both ("label-600@..."). A
    chip records its label key, so a new font file never changes an old
    version: old files are never deleted. The backend shapes the words with
    HarfBuzz and draws the outlines with its own rasteriser (label_font.py),
    so no system font library is needed anywhere. Coverage is self-tested
    through each font's cmap.

FONTS (--vg5000 / --jost, each with its licence)
    VG5000 Regular (Justin Bihan / Velvetyne, SIL OFL 1.1), the headings, is
    copied as-is. Jost (indestructible type*, SIL OFL 1.1), the sub-line and a
    stand-in for Futura PT Book, is Google's variable Jost[wght].ttf pinned to
    a STATIC wght=400 instance and written as WOFF2 (fontTools; the WOFF2 step
    needs the `brotli` module). It used to load from fonts.googleapis.com at
    render time. The OFL text is written next to each font, as the licence
    requires.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
REMOTION = HERE.parent
OUT = REMOTION / "public" / "brand42"

def make_logo(src: Path) -> None:
    im = np.array(Image.open(src).convert("RGBA"))
    im[..., :3] = 255  # pure white ink; the alpha carries the shape and its AA
    OUT.mkdir(parents=True, exist_ok=True)
    Image.fromarray(im, "RGBA").save(OUT / "logo_stacked_white.png", optimize=True)
    print(f"logo_stacked_white.png  {im.shape[1]}x{im.shape[0]}")


OFL_HEADERS = {
    "VG5000": (
        "VG5000 Regular - Copyright (c) 2015-2018, Justin Bihan <justin.bihan@gmail.com>\n"
        "Velvetyne Type Foundry, http://velvetyne.fr/fonts/vg5000/\n\n"
    ),
    "Jost": (
        "Jost - Copyright 2020 The Jost Project Authors (https://github.com/indestructible-type/Jost)\n"
        "Shipped here as a static Regular (wght 400) instance of Jost[wght], WOFF2.\n\n"
    ),
}


def make_jost(variable_ttf: Path, licence: Path) -> None:
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    font = TTFont(variable_ttf)
    static = instantiateVariableFont(font, {"wght": 400}, updateFontNames=True)
    static.flavor = "woff2"  # needs the brotli module
    fonts = OUT / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    static.save(fonts / "Jost-Regular.woff2")
    text = licence.read_text(encoding="utf-8")
    (fonts / "Jost-OFL.txt").write_text(OFL_HEADERS["Jost"] + text, encoding="utf-8")
    print(f"fonts/Jost-Regular.woff2 ({(fonts / 'Jost-Regular.woff2').stat().st_size} B) + fonts/Jost-OFL.txt")


def copy_font(woff2: Path, licence: Path) -> None:
    fonts = OUT / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(woff2, fonts / "VG5000-Regular.woff2")
    text = licence.read_text(encoding="utf-8")
    (fonts / "VG5000-OFL.txt").write_text(OFL_HEADERS["VG5000"] + text, encoding="utf-8")
    print("fonts/VG5000-Regular.woff2 + fonts/VG5000-OFL.txt")


# ------------------------------------------------------------------ patterns --

COLOURS = {"white": (255, 255, 255), "violet": (0x7F, 0x2B, 0xF2)}

# slug -> how its kit mask becomes a tile. `scale` maps the 2400 px kit mask to
# output px at 1080 wide (chosen so strokes land at ~3 px and bead glyphs at
# ~20-25 px); `crop_x0` drops an empty margin; `gap` is the spaced mode's
# transparent gap (x, y) in output px.
PATTERN_TILES: dict[str, dict] = {
    "blueprint": {"scale": 0.5, "mode": "mirror", "colours": ("violet",)},
    "beads_field": {"scale": 0.5, "mode": "mirror", "colours": ("white", "violet")},
    "beads_row": {"scale": 0.42, "mode": "mirror", "crop_x0": 324, "colours": ("white", "violet")},
    "structure": {"scale": 0.6, "mode": "spaced", "gap": (240, 140), "colours": ("white", "violet")},
}


def _save_one_colour(alpha: np.ndarray, rgb: tuple[int, int, int], path: Path) -> None:
    """8-bit palette PNG: entry k = `rgb` at alpha k, each pixel = its alpha."""
    im = Image.fromarray(alpha.astype(np.uint8), "P")
    im.putpalette(list(rgb) * 256)
    im.save(path, transparency=bytes(range(256)), optimize=True)


def make_patterns(kit: Path) -> None:
    out = OUT / "patterns"
    out.mkdir(parents=True, exist_ok=True)
    meta: dict[str, dict] = {}
    for slug, spec in PATTERN_TILES.items():
        a = np.asarray(Image.open(kit / f"{slug}.mask.png").convert("RGBA"))[..., 3]
        a = a[:, int(spec.get("crop_x0", 0)):]
        h, w = a.shape
        size = (max(1, round(w * spec["scale"])), max(1, round(h * spec["scale"])))
        a = np.asarray(Image.fromarray(a).resize(size, Image.LANCZOS))
        if spec["mode"] == "mirror":
            top = np.concatenate([a, a[:, ::-1]], axis=1)
            tile = np.concatenate([top, top[::-1, :]], axis=0)
        else:
            gx, gy = spec["gap"]
            tile = np.zeros((a.shape[0] + gy, a.shape[1] + gx), np.uint8)
            tile[: a.shape[0], : a.shape[1]] = a
        files = {}
        for colour in spec["colours"]:
            name = f"{slug}_{colour}.png"
            _save_one_colour(tile, COLOURS[colour], out / name)
            files[colour] = f"brand42/patterns/{name}"
        meta[slug] = {"w": int(tile.shape[1]), "h": int(tile.shape[0]), "files": files,
                      "ink": round(float((tile > 127).mean()), 4)}
        sizes = ", ".join(f"{c} {(out / f'{slug}_{c}.png').stat().st_size // 1024} KB" for c in spec["colours"])
        print(f"patterns/{slug}: tile {tile.shape[1]}x{tile.shape[0]} ({spec['mode']}) - {sizes}")
    ts = (
        "/**\n"
        " * GENERATED by scripts/make_brand42_assets.py --patterns: do not edit.\n"
        " * One seamless tile per pattern (repeats in both directions), sizes in\n"
        " * output px at 1080 wide; `files` are staticFile() paths per colour.\n"
        " */\n"
        "export type PatternSlug = " + " | ".join(json.dumps(k) for k in meta) + ";\n\n"
        "export const PATTERN_TILES: Record<PatternSlug, { w: number; h: number; ink: number; "
        "files: Partial<Record<\"white\" | \"violet\", string>> }> = "
        + json.dumps(meta, indent=2) + ";\n"
    )
    (REMOTION / "src" / "brand42" / "patternTiles.ts").write_text(ts, encoding="utf-8")
    print("src/brand42/patternTiles.ts")


# --------------------------------------------------------------------- icons --

PHOSPHOR = "https://cdn.jsdelivr.net/npm/@phosphor-icons/core@2.1.1"

# Every icon the backend's keyword table may ask for (Phosphor names).
ICON_IDS: tuple[str, ...] = (
    "piggy-bank", "coins", "code", "terminal-window", "git-branch", "laptop", "cpu", "robot",
    "brain", "users-three", "student", "chalkboard-teacher", "graduation-cap", "book-open",
    "lightbulb", "rocket-launch", "clock", "hourglass", "calendar-blank", "swimming-pool",
    "globe-hemisphere-east", "map-pin", "buildings", "trophy", "briefcase", "handshake",
    "chats-circle", "question", "magnifying-glass", "puzzle-piece", "game-controller", "heart",
    "star", "shield-check", "chart-line-up", "target", "flag", "wifi-high", "sparkle",
    "hand-waving", "airplane-tilt", "clipboard-text", "certificate", "lock-key-open", "infinity",
    "gift", "barbell", "smiley", "coffee", "moon-stars", "house", "palette", "calculator",
    "compass", "footprints", "mountains", "atom", "seal-check", "users", "microphone",
)


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "make_brand42_assets"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def make_icons() -> None:
    paths: dict[str, str] = {}
    for name in ICON_IDS:
        svg = _get(f"{PHOSPHOR}/assets/fill/{name}-fill.svg")
        if 'viewBox="0 0 256 256"' not in svg:
            raise SystemExit(f"{name}: unexpected viewBox")
        others = set(re.findall(r"<(rect|circle|ellipse|line|polyline|polygon)\b", svg))
        if others:
            raise SystemExit(f"{name}: has {others}, only <path> is supported")
        ds = re.findall(r'<path[^>]*\bd="([^"]+)"', svg)
        if not ds:
            raise SystemExit(f"{name}: no path data")
        paths[name] = " ".join(ds)
    licence = _get(f"{PHOSPHOR}/LICENSE")
    (OUT / "ICONS-LICENSE.txt").write_text(
        "Topic icons: Phosphor Icons (fill weight), https://phosphoricons.com\n\n" + licence, encoding="utf-8")
    ts = (
        "/**\n"
        " * GENERATED by scripts/make_brand42_assets.py --icons: do not edit.\n"
        " * Phosphor Icons, fill weight (MIT, Copyright (c) 2023 Phosphor Icons;\n"
        " * public/brand42/ICONS-LICENSE.txt). SVG path data on a 256 x 256 box.\n"
        " */\n"
        "export const ICON_VIEWBOX = 256;\n\n"
        "export const ICONS: Record<string, string> = " + json.dumps(paths, indent=2) + ";\n"
    )
    (REMOTION / "src" / "brand42" / "icons.ts").write_text(ts, encoding="utf-8")
    print(f"src/brand42/icons.ts ({len(paths)} icons, {sum(len(d) for d in paths.values()) // 1024} KB of path data)")


# ------------------------------------------------------------ icon catalogue --

REPO = REMOTION.parent.parent
CATALOGUE_OUT = REPO / "backend" / "app" / "assets" / "brand42"
PHOSPHOR_PACKAGE = "@phosphor-icons/core@2.1.1"
# The rasteriser self-test: an icon must cover this share of its box at
# SELFTEST_PX, or it is too faint (a dot) or too solid to read as a badge.
SELFTEST_PX = 64
SELFTEST_COVERAGE = (0.05, 0.95)


def _js_literal_to_json(body: str, enums: dict[str, dict[str, str]]) -> str:
    """The icon array of dist/index.mjs (a JS literal) as JSON: enum
    references (e.FINANCE) become their strings, bare keys are quoted,
    trailing commas go. Strings are passed through untouched."""
    def swap(m: re.Match) -> str:
        if m.group(0).startswith('"'):
            return m.group(0)
        if m.group(1):                              # e.FINANCE
            return json.dumps(enums[m.group(1)][m.group(2)])
        return json.dumps(m.group(3))               # a bare key
    body = re.sub(r'"(?:[^"\\]|\\.)*"|\b([a-z])\.([A-Z_]+)\b|\b([A-Za-z_]\w*)(?=\s*:)', swap, body)
    return re.sub(r",(\s*[\]}])", r"\1", body)


def _phosphor_meta(text: str) -> list[dict]:
    """[{name, pascal_name, categories, figma_category, tags, alias?, ...}]
    from @phosphor-icons/core's dist/index.mjs."""
    enums: dict[str, dict[str, str]] = {}
    for var, body in re.findall(r'(\w) = /\* @__PURE__ \*/ \(\(i\) => \((.*?), i\)\)\(\1 \|\| \{\}\)', text):
        enums[var] = dict(re.findall(r'i\.(\w+) = "([^"]*)"', body))
    start = text.index("const n = [") + len("const n = ")
    end = text.index("\n];", start) + 2
    return json.loads(_js_literal_to_json(text[start:end], enums))


def _num(v: str) -> float:
    return float(v or 0)


def _svg_path_data(name: str, svg: str) -> str:
    """Every filled shape of a Phosphor fill SVG as one path-data string (on
    the 256 box). <rect>, <circle>, <ellipse>, <polygon> and <polyline> are
    converted; <line> has no area in a fill icon and is dropped."""
    if 'viewBox="0 0 256 256"' not in svg:
        raise ValueError(f"{name}: unexpected viewBox")
    if re.search(r"\btransform=", svg):
        raise ValueError(f"{name}: has a transform")
    parts: list[str] = []
    for m in re.finditer(r"<(path|rect|circle|ellipse|polygon|polyline|line)\b([^>]*?)/?>", svg):
        tag, attrs = m.groups()
        a = dict(re.findall(r'([\w:-]+)="([^"]*)"', attrs))
        if a.get("fill") == "none" or a.get("opacity") == "0":
            continue
        if tag == "path":
            parts.append(a["d"])
        elif tag == "rect":
            x, y, w, h = (_num(a.get(k, "0")) for k in ("x", "y", "width", "height"))
            rx = _num(a.get("rx") or a.get("ry") or "0")
            ry = _num(a.get("ry") or a.get("rx") or "0")
            rx, ry = min(rx, w / 2), min(ry, h / 2)
            if rx or ry:
                parts.append(
                    f"M{x + rx},{y}H{x + w - rx}A{rx},{ry} 0 0 1 {x + w},{y + ry}V{y + h - ry}"
                    f"A{rx},{ry} 0 0 1 {x + w - rx},{y + h}H{x + rx}A{rx},{ry} 0 0 1 {x},{y + h - ry}"
                    f"V{y + ry}A{rx},{ry} 0 0 1 {x + rx},{y}Z")
            else:
                parts.append(f"M{x},{y}H{x + w}V{y + h}H{x}Z")
        elif tag in ("circle", "ellipse"):
            cx, cy = _num(a.get("cx", "0")), _num(a.get("cy", "0"))
            rx = _num(a.get("r") or a.get("rx") or "0")
            ry = _num(a.get("r") or a.get("ry") or "0")
            parts.append(f"M{cx - rx},{cy}A{rx},{ry} 0 1 0 {cx + rx},{cy}A{rx},{ry} 0 1 0 {cx - rx},{cy}Z")
        elif tag in ("polygon", "polyline"):
            pts = re.findall(r"-?[\d.]+(?:e-?\d+)?", a.get("points", ""))
            pairs = [f"{pts[i]},{pts[i + 1]}" for i in range(0, len(pts) - 1, 2)]
            if pairs:
                parts.append("M" + "L".join(pairs) + "Z")
    if not parts:
        raise ValueError(f"{name}: no filled shapes")
    return " ".join(parts)


def _display_name(icon_id: str) -> str:
    return icon_id.replace("-", " ").capitalize()


def _is_brand(entry: dict) -> bool:
    return ("brands" in (entry.get("categories") or []) or entry.get("figma_category") == "brands"
            or entry["name"].endswith("-logo"))


def _clean_tags(tags) -> list[str]:
    return [t for t in dict.fromkeys(str(t).strip().lower() for t in tags or ()) if t and not t.startswith("*")]


def _brand_names(meta: list[dict]) -> list[str]:
    """The brands whose logos were left out ("instagram" from instagram-logo),
    so the backend can refuse to have one DRAWN for a custom name."""
    return sorted({re.sub(r"-logo$", "", e["name"]).replace("-", " ") for e in meta if _is_brand(e)})


def _fetch_catalogue() -> tuple[dict[str, dict], str, str, list[str]]:
    """(icons, licence text, source note, brand names) from the CDN."""
    from concurrent.futures import ThreadPoolExecutor

    meta = _phosphor_meta(_get(f"{PHOSPHOR}/dist/index.mjs"))
    keep = [e for e in meta if not _is_brand(e)]
    print(f"metadata: {len(meta)} icons, {len(meta) - len(keep)} brand logos left out")

    def fetch(entry: dict) -> tuple[str, str]:
        name = entry["name"]
        last: Exception | None = None
        for _ in range(3):
            try:
                return name, _svg_path_data(name, _get(f"{PHOSPHOR}/assets/fill/{name}-fill.svg"))
            except ValueError:
                raise
            except Exception as e:  # noqa: BLE001 - a network blip; retried
                last = e
        raise RuntimeError(f"{name}: {last}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        paths = dict(pool.map(fetch, keep))
    icons = {}
    for e in keep:
        icons[e["name"]] = {
            "d": paths[e["name"]],
            "name": _display_name(e["name"]),
            # A renamed icon's old id (its alias) still finds it.
            "tags": _clean_tags([*(e.get("tags") or ()), (e.get("alias") or {}).get("name", "").replace("-", " ")]),
            "categories": sorted({*(e.get("categories") or ()), e.get("figma_category") or ""} - {""}),
        }
    return icons, _get(f"{PHOSPHOR}/LICENSE"), f"{PHOSPHOR_PACKAGE} (cdn.jsdelivr.net)", _brand_names(meta)


def _vendored_catalogue() -> tuple[dict[str, dict], str, str, list[str]]:
    """The offline fallback: the icons already vendored for the composition,
    with tags from a local node_modules copy of the package if there is one."""
    src = (REMOTION / "src" / "brand42" / "icons.ts").read_text(encoding="utf-8")
    paths = json.loads(src[src.index("= {") + 2: src.rindex("}") + 1])
    meta: dict[str, dict] = {}
    for nm in (REMOTION / "node_modules", REPO / "frontend" / "node_modules"):
        mjs = nm / "@phosphor-icons" / "core" / "dist" / "index.mjs"
        if mjs.is_file():
            meta = {e["name"]: e for e in _phosphor_meta(mjs.read_text(encoding="utf-8"))}
            break
    icons = {}
    for name, d in paths.items():
        e = meta.get(name, {"name": name})
        if _is_brand(e):
            continue
        icons[name] = {"d": d, "name": _display_name(name), "tags": _clean_tags(e.get("tags")),
                       "categories": sorted({*(e.get("categories") or ()), e.get("figma_category") or ""} - {""})}
    lic = OUT / "ICONS-LICENSE.txt"
    licence = lic.read_text(encoding="utf-8") if lic.is_file() else "MIT License\n\nCopyright (c) 2023 Phosphor Icons\n"
    note = f"{PHOSPHOR_PACKAGE} (VENDORED FALLBACK: src/brand42/icons.ts{', node_modules metadata' if meta else ''})"
    return icons, licence, note, _brand_names(list(meta.values()))


def _selftest(icons: dict[str, dict]) -> list[str]:
    """Rasterise every icon with the backend's renderer; the ids that fail
    or fall outside SELFTEST_COVERAGE (they are dropped, with the reason)."""
    import sys

    sys.path.insert(0, str(REPO / "backend"))
    from app.services.rebrand_harness import icon_render

    lo, hi = SELFTEST_COVERAGE
    bad = []
    for name, entry in icons.items():
        try:
            cov = float(icon_render.path_alpha(entry["d"], SELFTEST_PX).mean())
        except Exception as e:  # noqa: BLE001 - reported, then dropped
            print(f"  self-test: {name} does not rasterise ({e})")
            bad.append(name)
            continue
        if not lo <= cov <= hi:
            print(f"  self-test: {name} covers {cov:.1%} of its box at {SELFTEST_PX} px - left out")
            bad.append(name)
    return bad


def make_icon_catalog() -> None:
    try:
        icons, licence, source, brands = _fetch_catalogue()
    except Exception as e:  # noqa: BLE001 - no network: fall back and say so
        print(f"WARNING: the CDN could not be read ({e}); falling back to the vendored icons")
        icons, licence, source, brands = _vendored_catalogue()
    bad = _selftest(icons)
    for name in bad:
        icons.pop(name, None)
    missing = [i for i in ICON_IDS if i not in icons]
    if missing:
        raise SystemExit(f"the 42 picks are missing from the catalogue: {missing}")
    CATALOGUE_OUT.mkdir(parents=True, exist_ok=True)
    head = {"source": source, "weight": "fill", "viewbox": 256,
            "licence": "MIT, Copyright (c) 2023 Phosphor Icons - see ICONS-LICENSE.txt",
            "excluded": "brand logos (the 'brands' category, *-logo ids)"
                        + (f"; {len(bad)} that failed the rasteriser self-test" if bad else ""),
            "brands": brands,
            "count": len(icons)}
    # One icon per line, so a re-bake diffs icon by icon.
    lines = [json.dumps(k) + ": " + json.dumps(v, separators=(",", ":")) for k, v in head.items()]
    body = ",\n".join(json.dumps(k) + ":" + json.dumps(icons[k], separators=(",", ":")) for k in sorted(icons))
    text = "{\n" + ",\n".join(lines) + ',\n"icons": {\n' + body + "\n}\n}\n"
    (CATALOGUE_OUT / "phosphor_fill.json").write_text(text, encoding="utf-8")
    (CATALOGUE_OUT / "ICONS-LICENSE.txt").write_text(
        "Rebrand studio icon library: Phosphor Icons (fill weight), https://phosphoricons.com\n"
        f"Source: {PHOSPHOR_PACKAGE}\n\n" + licence, encoding="utf-8")
    size = (CATALOGUE_OUT / "phosphor_fill.json").stat().st_size
    print(f"backend/app/assets/brand42/phosphor_fill.json ({len(icons)} icons, {size // 1024} KB) "
          f"+ ICONS-LICENSE.txt - {source}")


# ------------------------------------------------------- bold icon catalogue --

# Bold strokes are thin shapes: a "minus" covers little of its box, and that
# is still a good icon. Only a shape that does not rasterise, or is nothing
# at all, is left out (the backend then draws the fill shape).
BOLD_SELFTEST_COVERAGE = (0.01, 0.95)


def make_icon_catalog_bold() -> None:
    """phosphor_bold.json: every id of phosphor_fill.json in the bold weight.
    phosphor_fill.json is only READ here (for its ids): it must never change
    (its hash keys the icon director's paid answers)."""
    from concurrent.futures import ThreadPoolExecutor
    import sys

    fill = json.loads((CATALOGUE_OUT / "phosphor_fill.json").read_text(encoding="utf-8"))
    ids = sorted(fill["icons"])

    def fetch(name: str) -> tuple[str, str | None, str]:
        last: Exception | None = None
        for _ in range(3):
            try:
                return name, _svg_path_data(name, _get(f"{PHOSPHOR}/assets/bold/{name}-bold.svg")), ""
            except ValueError as e:
                return name, None, str(e)
            except Exception as e:  # noqa: BLE001 - a network blip; retried
                last = e
        return name, None, f"not downloaded ({last})"

    with ThreadPoolExecutor(max_workers=16) as pool:
        got = list(pool.map(fetch, ids))
    sys.path.insert(0, str(REPO / "backend"))
    from app.services.rebrand_harness import icon_render

    lo, hi = BOLD_SELFTEST_COVERAGE
    paths: dict[str, str] = {}
    missing: dict[str, str] = {}
    for name, d, why in got:
        if d is None:
            missing[name] = why
            continue
        try:
            cov = float(icon_render.path_alpha(d, SELFTEST_PX).mean())
        except Exception as e:  # noqa: BLE001 - reported, then left out
            missing[name] = f"does not rasterise ({e})"
            continue
        if not lo <= cov <= hi:
            missing[name] = f"covers {cov:.1%} of its box at {SELFTEST_PX} px"
            continue
        paths[name] = d
    for name, why in missing.items():
        print(f"  bold: {name} left out - {why} (the fill shape is drawn instead)")
    missing_picks = [i for i in ICON_IDS if i not in paths]
    if missing_picks:
        raise SystemExit(f"the 42 picks are missing from the bold catalogue: {missing_picks}")
    # The fill file these ids came from (its catalogue_version()).
    fill_version = "phosphor-fill-" + hashlib.sha256(
        (CATALOGUE_OUT / "phosphor_fill.json").read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:10]
    head = {"source": f"{PHOSPHOR_PACKAGE} (cdn.jsdelivr.net)", "weight": "bold", "viewbox": 256,
            "licence": "MIT, Copyright (c) 2023 Phosphor Icons - see ICONS-LICENSE.txt",
            "ids_from": fill_version, "missing": sorted(missing), "count": len(paths)}
    lines = [json.dumps(k) + ": " + json.dumps(v, separators=(",", ":")) for k, v in head.items()]
    body = ",\n".join(json.dumps(k) + ":" + json.dumps({"d": paths[k]}, separators=(",", ":"))
                      for k in sorted(paths))
    text = "{\n" + ",\n".join(lines) + ',\n"icons": {\n' + body + "\n}\n}\n"
    (CATALOGUE_OUT / "phosphor_bold.json").write_text(text, encoding="utf-8", newline="\n")
    size = (CATALOGUE_OUT / "phosphor_bold.json").stat().st_size
    print(f"backend/app/assets/brand42/phosphor_bold.json ({len(paths)} icons, {size // 1024} KB, "
          f"{len(missing)} missing)")


# --------------------------------------------------------------- label fonts --

GOOGLE_FONTS = "https://github.com/google/fonts/raw/main/ofl"
FONTS_OUT = CATALOGUE_OUT / "fonts"
LABEL_WEIGHT = 600
# Each face: its variable source in google/fonts, the static file it becomes,
# its OFL, the scripts it draws and what its cmap must cover.
LABEL_FACES: tuple[dict, ...] = (
    {"slug": "jost", "family": "Jost", "src": "jost/Jost[wght].ttf", "ofl": "jost/OFL.txt",
     "file": "Jost-SemiBold.ttf", "licence": "Jost-OFL.txt", "scripts": ["Latn"],
     "covers": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:;!?&'-/()+%#@…’"
               "éèàçüöä"},
    {"slug": "kufi", "family": "Noto Kufi Arabic", "src": "notokufiarabic/NotoKufiArabic[wght].ttf",
     "ofl": "notokufiarabic/OFL.txt", "file": "NotoKufiArabic-SemiBold.ttf",
     "licence": "NotoKufiArabic-OFL.txt", "scripts": ["Arab"],
     # Every Arabic letter, the Arabic-Indic digits, the harakat and ، ؟
     "covers": "".join(chr(c) for c in range(0x0621, 0x063B)) + "".join(chr(c) for c in range(0x0641, 0x064B))
               + "".join(chr(c) for c in range(0x064B, 0x0653)) + "".join(chr(c) for c in range(0x0660, 0x066A))
               + "،؟ …"},
)
LABEL_HEADERS = {
    "jost": ("Jost - Copyright 2020 The Jost Project Authors (https://github.com/indestructible-type/Jost)\n"
             "Shipped here as a static SemiBold (wght 600) instance of Jost[wght], TTF.\n\n"),
    "kufi": ("Noto Kufi Arabic - Copyright 2022 The Noto Project Authors (https://github.com/notofonts/arabic)\n"
             "Shipped here as a static SemiBold (wght 600) instance of NotoKufiArabic[wght], TTF.\n\n"),
}


def _get_bytes(url: str) -> bytes:
    from urllib.parse import quote

    url = quote(url, safe=":/")                      # "Jost[wght].ttf" has brackets
    req = urllib.request.Request(url, headers={"User-Agent": "make_brand42_assets"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _sha8(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def make_label_fonts() -> None:
    """The chip icons' label fonts: static wght 600 TTFs + OFL + fonts.json.
    A face key is "<slug>-600@<sha8 of the file>"; the label key names a
    Latin face and an Arabic face together ("label-600@<sha8 of both keys>").
    An existing fonts.json is extended, never rewritten: an old key keeps
    pointing at its own file."""
    import io
    import tempfile

    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    FONTS_OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = FONTS_OUT / "fonts.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file()
                else {"faces": {}, "labels": {}})
    keys: dict[str, str] = {}
    for face in LABEL_FACES:
        src = _get_bytes(f"{GOOGLE_FONTS}/{face['src']}")
        # recalcTimestamp=False: the file keeps its source's date, so the
        # same source always bakes to the same bytes (and the same key).
        font = TTFont(io.BytesIO(src), recalcTimestamp=False)
        try:
            static = instantiateVariableFont(font, {"wght": LABEL_WEIGHT}, updateFontNames=True)
        except Exception:  # noqa: BLE001 - no STAT names to update: keep the family's own
            font = TTFont(io.BytesIO(src), recalcTimestamp=False)
            static = instantiateVariableFont(font, {"wght": LABEL_WEIGHT})
        cmap = static.getBestCmap()
        gaps = [c for c in face["covers"] if ord(c) not in cmap]
        if gaps:
            raise SystemExit(f"{face['file']}: no glyph for {gaps!r}")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / face["file"]
            static.save(out)
            data = out.read_bytes()
        key = f"{face['slug']}-{LABEL_WEIGHT}@{_sha8(data)}"
        known = manifest["faces"].get(key)
        name = known["file"] if known else face["file"]
        if known is None:
            # A new file for a face that already has one keeps both (old
            # versions draw with theirs): the new one is named by its key.
            if name in {v["file"] for v in manifest["faces"].values()}:
                name = f"{Path(face['file']).stem}@{_sha8(data)}.ttf"
            (FONTS_OUT / name).write_bytes(data)
            manifest["faces"][key] = {
                "file": name, "sha256": hashlib.sha256(data).hexdigest(), "family": face["family"],
                "weight": LABEL_WEIGHT, "scripts": face["scripts"], "licence": face["licence"],
                "source": f"google/fonts ofl/{face['src']} at wght {LABEL_WEIGHT}",
            }
        ofl = _get_bytes(f"{GOOGLE_FONTS}/{face['ofl']}").decode("utf-8").replace("\r\n", "\n")
        (FONTS_OUT / face["licence"]).write_text(LABEL_HEADERS[face["slug"]] + ofl, encoding="utf-8",
                                                  newline="\n")
        keys[face["scripts"][0]] = key
        print(f"fonts/{name} ({len(data) // 1024} KB) = {key}")
    pair = f"{keys['Latn']}|{keys['Arab']}"
    label = f"label-{LABEL_WEIGHT}@{_sha8(pair.encode())}"
    manifest["labels"].setdefault(label, {"latin": keys["Latn"], "arabic": keys["Arab"]})
    manifest["default"] = label
    manifest["note"] = ("Chip icon label fonts (make_brand42_assets.py --label-fonts). Never delete or "
                        "edit an entry: stored versions name their label key.")
    ordered = {"note": manifest["note"], "default": manifest["default"],
               "faces": dict(sorted(manifest["faces"].items())), "labels": dict(sorted(manifest["labels"].items()))}
    manifest_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                             newline="\n")
    print(f"fonts/fonts.json - default label font {label}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--logo", type=Path)
    ap.add_argument("--vg5000", type=Path)
    ap.add_argument("--vg5000-licence", type=Path)
    ap.add_argument("--jost", type=Path)
    ap.add_argument("--jost-licence", type=Path)
    ap.add_argument("--patterns", type=Path, help="the pattern kit dir (backend/data/branding/patterns)")
    ap.add_argument("--icons", action="store_true", help="download the Phosphor topic icons")
    ap.add_argument("--icon-catalog", action="store_true",
                    help="bake the whole Phosphor fill set for the backend's icon library")
    ap.add_argument("--icon-catalog-bold", action="store_true",
                    help="bake the same ids in Phosphor's bold weight (phosphor_bold.json)")
    ap.add_argument("--label-fonts", action="store_true",
                    help="Jost + Noto Kufi Arabic SemiBold for the chip icons' words")
    args = ap.parse_args()
    if not (args.logo or args.vg5000 or args.jost or args.patterns or args.icons or args.icon_catalog
            or args.icon_catalog_bold or args.label_fonts):
        ap.error("nothing to build: pass --logo, --vg5000, --jost, --patterns, --icons, --icon-catalog, "
                 "--icon-catalog-bold and/or --label-fonts")

    if args.logo:
        make_logo(args.logo)
    if args.vg5000:
        if not args.vg5000_licence:
            ap.error("--vg5000 needs --vg5000-licence (the OFL must ship with the font)")
        copy_font(args.vg5000, args.vg5000_licence)
    if args.jost:
        if not args.jost_licence:
            ap.error("--jost needs --jost-licence (the OFL must ship with the font)")
        make_jost(args.jost, args.jost_licence)
    if args.patterns:
        make_patterns(args.patterns)
    if args.icons:
        make_icons()
    if args.icon_catalog:
        make_icon_catalog()
    if args.icon_catalog_bold:
        make_icon_catalog_bold()
    if args.label_fonts:
        make_label_fonts()


if __name__ == "__main__":
    main()
