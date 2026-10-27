"""The TARGET frame — the clip's own frame exactly as Omni must hand it back.

Omni is never shown a bare graphic: handed a transparent PNG it drew a
checkerboard, handed a lattice it drew a panel of bars. It is shown the
finished frame instead, and copies it. compose_target builds that frame from
the recipe's layers:

  behind  layers are cut out wherever the speaker is (their mask grown a
          little and softened, so no line clings to hair or a sleeve), then
          laid over the frame in order — the speaker stands in front of them;
  front   layers are laid over everything, speaker included, in order.

On a letterbox bar the speaker mask is ignored for a behind layer that asks
(`clears_bars`): MODNet's specks there are not the speaker.

rebrand_pattern_ref.strip_preview is compose_target with one behind layer —
the approved pattern target, pixel for pixel (tests/test_rebrand_golden.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# A layer draws itself for the frame's size: an RGBA PIL image width x height,
# or None when it has nothing to show at that size.
Render = Callable[[int, int], Any]


@dataclass
class Layer:
    render: Render
    z: str = "behind"              # behind | front
    clears_bars: bool = False      # behind only: ignore the speaker mask on letterbox bars
    name: str = ""


def _keep(person, width: int, height: int):
    """1 - the speaker mask grown past their edge and softened (float HxW)."""
    import numpy as np
    from PIL import Image, ImageFilter

    m = np.clip(np.asarray(person, dtype="float32"), 0.0, 1.0)
    if m.shape != (height, width):
        m = np.asarray(Image.fromarray((m * 255).astype("uint8")).resize(
            (width, height), Image.BILINEAR), dtype="float32") / 255.0
    edge = max(3, round(min(width, height) * 0.008)) | 1   # odd MaxFilter size
    grown = Image.fromarray((m * 255).astype("uint8")).filter(
        ImageFilter.MaxFilter(edge)).filter(ImageFilter.GaussianBlur(1.2))
    return 1.0 - np.asarray(grown, dtype="float32") / 255.0


def compose_target(frame: Path, out: Path, layers: list[Layer], *, person=None,
                   alphas: Optional[list] = None) -> Path:
    """`frame` with `layers` laid in (see the module doc). An opaque RGB PNG.
    `person` is a float HxW speaker mask (0..1, person_matte.person_mask);
    None lays behind layers over everything.

    `alphas`, when a list, collects each drawn layer's alpha — read only,
    nothing drawn changes: {"index" (in `layers`), "name", "z", "alpha" (the
    layer's own, uint8 HxW), "visible" (as laid in, after the speaker cut)}.
    The studio draws each pattern's box from them."""
    import numpy as np
    from PIL import Image

    from .. import rebrand_pattern_ref as pattern_ref

    with Image.open(frame) as f:
        base = f.convert("RGBA")
    width, height = base.size
    keep = None
    bars: Any = False                              # not looked for yet
    fronts = []
    for i, layer in enumerate(layers):
        img = layer.render(width, height)
        if img is None:
            continue
        own = np.asarray(img.getchannel("A")) if alphas is not None else None
        if layer.z == "front":
            fronts.append(img)
            if alphas is not None:
                alphas.append({"index": i, "name": layer.name, "z": layer.z, "alpha": own,
                               "visible": own})
            continue
        if person is not None:
            if keep is None:
                keep = _keep(person, width, height)
            k = keep
            if layer.clears_bars:
                if bars is False:
                    bars = pattern_ref.bar_mask(np.asarray(base))
                if bars is not None:
                    k = keep.copy()
                    k[bars] = 1.0
            alpha = np.asarray(img.getchannel("A"), dtype="float32") * k
            img.putalpha(Image.fromarray(np.clip(np.rint(alpha), 0, 255).astype("uint8")))
        if alphas is not None:
            alphas.append({"index": i, "name": layer.name, "z": layer.z, "alpha": own,
                           "visible": np.asarray(img.getchannel("A"))})
        base.alpha_composite(img)
    for img in fronts:
        base.alpha_composite(img)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(out, format="PNG")
    return out


def pattern_layer(ref, *, name: str = "pattern") -> Layer:
    """A PatternRef as a behind layer in its layout's place — moved, sized
    or turned when it was by hand (rebrand_pattern_ref.placed_layer: with no
    hand move, exactly its _layer)."""
    from .. import rebrand_pattern_ref as pattern_ref

    return Layer(render=lambda w, h: pattern_ref.placed_layer(ref, w, h), z="behind",
                 clears_bars=ref.clears_bars, name=name)


def union_alpha(layers: list[Layer], width: int, height: int, *, grow_frac: float = 0.01):
    """Where the behind layers have ink, as one float HxW mask (0..1): their
    alphas' union, grown by `grow_frac` of the short side and softened — the
    veil guard's keep mask for several patterns (guards.veil_keep)."""
    import numpy as np
    from PIL import Image, ImageFilter

    acc = np.zeros((height, width), dtype="float32")
    for layer in layers:
        if layer.z != "behind":
            continue
        img = layer.render(width, height)
        if img is None:
            continue
        acc = np.maximum(acc, np.asarray(img.getchannel("A"), dtype="float32") / 255.0)
    size = max(3, round(min(width, height) * grow_frac) * 2 + 1)
    grown = Image.fromarray(np.clip(np.rint(acc * 255.0), 0, 255).astype("uint8")).filter(
        ImageFilter.MaxFilter(size)).filter(ImageFilter.GaussianBlur(1.5))
    return np.asarray(grown, dtype="float32") / 255.0


def bbox(alpha, *, threshold: int = 8) -> Optional[dict]:
    """A layer's alpha bounding box in frame fractions {x, y, w, h}, or None
    when it has no ink."""
    import numpy as np

    a = np.asarray(alpha)
    if a.ndim != 2:
        return None
    rows = np.nonzero((a > threshold).any(axis=1))[0]
    cols = np.nonzero((a > threshold).any(axis=0))[0]
    if not rows.size or not cols.size:
        return None
    h, w = a.shape
    return {"x": round(int(cols[0]) / w, 4), "y": round(int(rows[0]) / h, 4),
            "w": round((int(cols[-1]) + 1 - int(cols[0])) / w, 4),
            "h": round((int(rows[-1]) + 1 - int(rows[0])) / h, 4)}
