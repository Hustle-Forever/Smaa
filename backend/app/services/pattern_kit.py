"""The 42 pattern kit — five brand graphics, prepared once, tintable at render.

The source art (``patterns/*.png`` at the repo root) is print-resolution: five
RGBA files between 8k and 16k pixels wide, 75 MB in total. Nothing can use
those directly, and three of them are flat single-colour line art whose colour
is baked into the pixels — which is exactly the colour we need to CHANGE per
reel, because the transition is supposed to take its palette from the intro it
grows out of.

So each source is reduced to two web-sized derivatives:

``<slug>.mask.png``
    White RGB, alpha carrying the shape. Used as a CSS mask over a solid
    colour, so the same file renders in any brand colour at render time with
    no per-reel image processing. This is the file the compositions use.

``<slug>.art.png``
    The original colours, downscaled. Only meaningful for multi-tone art
    (``structure``), where flattening to one colour would throw away the
    shading that makes it read as three-dimensional.

``kit.json``
    Measured metadata — bounding box, aspect, ink coverage, source colour.
    Measured rather than hand-typed so it cannot drift from the pixels, and
    so the planner can reason about density ("this one is 15% ink, it can sit
    over a face; that one is 54%, it cannot").

Preparation is idempotent and keyed on source mtime+size, so importing this
module in a worker never re-does the work.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal, Optional

from ..config import config

log = logging.getLogger("archivist.pattern_kit")

# Where the prepared kit lives (served to Remotion as /data/branding/patterns/).
KIT_DIR = (config.asset_root.parent / "branding" / "patterns").resolve()
KIT_MANIFEST = KIT_DIR / "kit.json"

# Longest edge of a derivative. The transition canvas is 720x1280 or 1280x720;
# 2400 leaves room for a pattern to be scaled up ~2x during a sweep and still
# out-resolve the frame.
MAX_EDGE = 2400

PatternRole = Literal["strip", "structure", "blueprint", "grid", "field"]


@dataclass(frozen=True)
class PatternSpec:
    """How one source file becomes a usable overlay."""
    slug: str
    source: str                 # file name under the source directory
    role: PatternRole
    # How the shape is recovered from the source pixels:
    #   "alpha"     — art on transparency; the alpha channel IS the shape
    #   "ink"       — art on an opaque light sheet; darkness IS the shape
    #   "glow"      — light art on an opaque dark ground; brightness IS the
    #                 shape. Keying such art on alpha yields its silhouette,
    #                 not its line-work.
    key: Literal["alpha", "ink", "glow"]
    keep_art: bool              # also emit the full-colour derivative
    description: str            # what the planner is told this thing looks like


# The five sources, in the order they were handed over. `role` and
# `description` are the only editorial judgements in this file; everything
# else below is measured.
SPECS: tuple[PatternSpec, ...] = (
    PatternSpec(
        slug="beads_row",
        source="1.png",
        role="strip",
        key="alpha",
        keep_art=False,
        description=(
            "Four sparse rows of pixel-bead glyphs strung on hairlines, like a "
            "DNA sequence read-out. Very wide and thin. Reads as a ticker or an "
            "edge strip; rotated upright it becomes a vertical spine down one "
            "side of the frame. Sparse enough to cross a face without hiding it."
        ),
    ),
    PatternSpec(
        slug="structure",
        source="2.png",
        role="structure",
        key="glow",
        keep_art=True,
        description=(
            "A dense isometric lattice of extruded bars in white outline, "
            "interlocking at right angles, with Arabic letterforms curled "
            "around them and small solid diamonds scattered through. The heavy "
            "hero graphic: architectural, loud, nearly opaque in the middle. "
            "Belongs in a sweeping panel or a masked wipe, never flat over a "
            "face."
        ),
    ),
    PatternSpec(
        slug="blueprint",
        source="3.png",
        role="blueprint",
        key="alpha",
        keep_art=False,
        description=(
            "A campus floor plan in single-weight line: corridors, rooms and "
            "cluster partitions, taller than it is wide. Pure outline, mostly "
            "empty space. Reads as a schematic being drawn — ideal for a "
            "stroke-on reveal or a slow drift behind the subject."
        ),
    ),
    PatternSpec(
        slug="grid",
        source="4.png",
        role="grid",
        key="ink",
        keep_art=False,
        description=(
            "Engineering graph paper: a fine measuring grid with heavier "
            "crosshair ticks at every major interval. Uniform, directionless, "
            "almost invisible on its own. The base plate other patterns are "
            "measured against; never the subject of a transition."
        ),
    ),
    PatternSpec(
        slug="beads_field",
        source="5.png",
        role="field",
        key="alpha",
        keep_art=False,
        description=(
            "Seven tightly packed rows of the same bead glyphs, dense and solid "
            "at the left and thinning to bare hairlines at the right. Carries a "
            "built-in left-to-right decay, so it dissolves on its own if it "
            "travels in that direction. A band of data, not a texture."
        ),
    ),
)

_BY_SLUG = {s.slug: s for s in SPECS}


@dataclass
class PreparedPattern:
    """A pattern as the planner and the renderer see it."""
    slug: str
    role: str
    description: str
    mask: str                   # /data-relative path of the mask PNG
    art: Optional[str]          # /data-relative path of the colour PNG, if any
    width: int                  # of the derivative
    height: int
    aspect: float               # width / height
    coverage: float             # 0..1 fraction of the box that is ink
    source_color: str           # dominant hex of the original art

    def to_props(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------

def _fingerprint(src_dir: Path) -> str:
    """Identity of the source set — so the kit rebuilds when the art changes
    and not otherwise."""
    parts = []
    for spec in SPECS:
        p = src_dir / spec.source
        st = p.stat()
        parts.append(f"{spec.slug}:{st.st_size}:{int(st.st_mtime)}")
    return "|".join(parts)


def _dominant_hex(rgb_rows, weights) -> str:
    """Weighted-mean colour of the inked pixels, as #rrggbb."""
    import numpy as np

    total = float(weights.sum())
    if total <= 0:
        return "#ffffff"
    mean = (rgb_rows.astype("float64") * weights[:, None]).sum(axis=0) / total
    r, g, b = (int(max(0, min(255, round(v)))) for v in mean)
    return f"#{r:02x}{g:02x}{b:02x}"


def _prepare_one(spec: PatternSpec, src: Path, out_dir: Path) -> PreparedPattern:
    import numpy as np
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # these are legitimately enormous
    im = Image.open(src).convert("RGBA")
    arr = np.asarray(im)

    lum = (
        arr[:, :, 0].astype("float32") * 0.299
        + arr[:, :, 1].astype("float32") * 0.587
        + arr[:, :, 2].astype("float32") * 0.114
    )
    if spec.key == "alpha":
        shape = arr[:, :, 3].astype("float32") / 255.0
    elif spec.key == "glow":
        # White line-work drawn on an opaque black ground. Brightness is the
        # shape; the dark shaded faces of the extrusions fall away, which is
        # what we want from a single-colour tint (they are shadow, not line).
        ceiling = float(np.percentile(lum, 99.9)) or 255.0
        shape = np.clip(lum / max(1.0, ceiling), 0.0, 1.0)
        shape *= arr[:, :, 3].astype("float32") / 255.0
    else:
        # Ink on a light sheet: darkness is the shape. The grid's lines are
        # only a few levels below paper white, so the range is stretched from
        # the actual paper level rather than from 255 — otherwise the mask
        # comes out empty.
        paper = float(np.percentile(lum, 98))
        floor = float(np.percentile(lum, 0.5))
        span = max(1.0, paper - floor)
        shape = np.clip((paper - lum) / span, 0.0, 1.0)
        shape *= arr[:, :, 3].astype("float32") / 255.0

    # Trim to the inked bounding box so scaling and positioning are about the
    # art, not about whatever margin the export happened to leave.
    ys, xs = np.nonzero(shape > 0.06)
    if len(ys) == 0:
        raise ValueError(f"{spec.slug}: no ink found in {src.name}")
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    shape = shape[y0:y1, x0:x1]
    arr = arr[y0:y1, x0:x1]

    h, w = shape.shape
    scale = min(1.0, MAX_EDGE / max(w, h))
    out_w, out_h = max(1, round(w * scale)), max(1, round(h * scale))

    coverage = float((shape > 0.35).mean())

    ink = shape > 0.35
    if ink.any():
        source_color = _dominant_hex(arr[:, :, :3][ink], shape[ink])
    else:
        source_color = "#ffffff"

    # Mask: white, shape in alpha. LANCZOS on the alpha keeps hairlines alive
    # at a twentieth of the original size.
    mask_full = Image.fromarray((shape * 255).astype("uint8"), mode="L")
    mask_small = mask_full.resize((out_w, out_h), Image.LANCZOS)
    if spec.key == "ink":
        # Faint ink on paper, then averaged down 5-6x, lands at a fraction of
        # full opacity — the grid peaked at alpha 103, which is invisible over
        # footage. Stretch the surviving range back to full so the renderer
        # controls visibility through opacity instead of inheriting whatever
        # the resample left behind.
        small = np.asarray(mask_small).astype("float32")
        peak = float(np.percentile(small, 99.9))
        if peak > 1.0:
            mask_small = Image.fromarray(
                np.clip(small * (255.0 / peak), 0, 255).astype("uint8"), mode="L"
            )
    white = Image.new("RGB", (out_w, out_h), (255, 255, 255))
    mask_img = Image.merge("RGBA", (*white.split(), mask_small))
    out_dir.mkdir(parents=True, exist_ok=True)
    mask_path = out_dir / f"{spec.slug}.mask.png"
    mask_img.save(mask_path, optimize=True)

    art_rel: Optional[str] = None
    if spec.keep_art:
        art_img = Image.fromarray(arr, mode="RGBA").resize((out_w, out_h), Image.LANCZOS)
        art_path = out_dir / f"{spec.slug}.art.png"
        art_img.save(art_path, optimize=True)
        art_rel = f"branding/patterns/{art_path.name}"

    return PreparedPattern(
        slug=spec.slug,
        role=spec.role,
        description=spec.description,
        mask=f"branding/patterns/{mask_path.name}",
        art=art_rel,
        width=out_w,
        height=out_h,
        aspect=round(out_w / out_h, 4),
        coverage=round(coverage, 4),
        source_color=source_color,
    )


def build_kit(src_dir: Path, *, force: bool = False) -> list[PreparedPattern]:
    """Prepare (or reuse) the kit. Returns the patterns in SPECS order."""
    src_dir = Path(src_dir)
    missing = [s.source for s in SPECS if not (src_dir / s.source).exists()]
    if missing:
        raise FileNotFoundError(f"pattern sources missing from {src_dir}: {missing}")

    fp = _fingerprint(src_dir)
    if not force and KIT_MANIFEST.exists():
        try:
            cached = json.loads(KIT_MANIFEST.read_text("utf-8"))
            if cached.get("fingerprint") == fp:
                have = {p["slug"]: p for p in cached.get("patterns", [])}
                if all(s.slug in have for s in SPECS) and all(
                    (KIT_DIR / Path(have[s.slug]["mask"]).name).exists() for s in SPECS
                ):
                    return [PreparedPattern(**have[s.slug]) for s in SPECS]
        except Exception:  # noqa: BLE001 — a bad manifest just means rebuild
            log.warning("[pattern_kit] manifest unreadable, rebuilding")

    log.info("[pattern_kit] preparing %d patterns from %s", len(SPECS), src_dir)
    prepared = [_prepare_one(s, src_dir / s.source, KIT_DIR) for s in SPECS]
    KIT_MANIFEST.write_text(
        json.dumps(
            {"fingerprint": fp, "max_edge": MAX_EDGE,
             "patterns": [asdict(p) for p in prepared]},
            indent=2,
        ),
        "utf-8",
    )
    log.info("[pattern_kit] kit ready → %s", KIT_DIR)
    return prepared


def load_kit() -> list[PreparedPattern]:
    """The prepared kit, from the manifest. Raises if it was never built."""
    if not KIT_MANIFEST.exists():
        raise FileNotFoundError(
            f"pattern kit not prepared — run `python -m app.services.pattern_kit <patterns dir>` "
            f"(expected {KIT_MANIFEST})"
        )
    data = json.loads(KIT_MANIFEST.read_text("utf-8"))
    by_slug = {p["slug"]: PreparedPattern(**p) for p in data.get("patterns", [])}
    return [by_slug[s.slug] for s in SPECS if s.slug in by_slug]


def kit_or_empty() -> list[PreparedPattern]:
    """The kit if it exists, [] otherwise — for callers that degrade."""
    try:
        return load_kit()
    except Exception:  # noqa: BLE001
        return []


def get(slug: str) -> Optional[PreparedPattern]:
    return next((p for p in kit_or_empty() if p.slug == slug), None)


def catalogue() -> str:
    """The kit described for a planning prompt: what each pattern is, how
    dense it is, and what shape box it wants."""
    lines = []
    for p in kit_or_empty():
        orient = "wide banner" if p.aspect >= 2 else "tall" if p.aspect <= 0.75 else "squarish"
        lines.append(
            f'- "{p.slug}" ({p.role}, {orient} {p.width}x{p.height}, '
            f"{round(p.coverage * 100)}% ink): {p.description}"
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover — one-off preparation
    import sys

    logging.basicConfig(level=logging.INFO)
    where = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "patterns"
    for pat in build_kit(where, force="--force" in sys.argv):
        print(
            f"{pat.slug:<12} {pat.width:>5}x{pat.height:<5} "
            f"aspect={pat.aspect:<7} ink={pat.coverage:.1%} src={pat.source_color} "
            f"-> {pat.mask}"
        )
