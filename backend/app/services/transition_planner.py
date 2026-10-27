"""Designing the intro→main seam: measurements and precedent in, a recipe out.

The recipe is data, not prose. Every field maps onto a prop of the
``pattern-transition`` Remotion composition, so what is planned here is
exactly what gets rendered — there is no second interpretation step where a
model's intent can be lost.

Two planners produce the same shape:

``compose``   deterministic. Reads the two moments and lays out a transition
              from the kit by rule. It is not a stub — it is the design, and
              it runs when Vertex is unreachable, which on some machines is
              always.

``plan``      asks Gemini to rewrite that composition knowing what is actually
              in frame, then falls back to it on any failure.

Both outputs go through ``enforce``, which is where the non-negotiables live:
nothing covers a face, nothing covers burned-in captions, the tint stays
legible against the footage, and the timing stays inside the clip. A planner
cannot opt out of those, which is the difference between asking a model not to
cover a face and it not being able to.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import pattern_kit
from .rebrand_moments import GRID_COLS, GRID_ROWS, Moment, format_precedents

log = logging.getLogger("archivist.rebrand.planner")

# The seam's shape. The intro tail is short — just enough to see the pattern
# arrive before the cut — and the main head is the "2 or 3 seconds" the
# transition has to resolve within.
PRE_SEC = 0.8
POST_SEC = 2.2
MIN_DURATION = 1.6
MAX_DURATION = 4.0
# `cover="shot"` runs the overlay across the whole talking head rather than
# just the seam, so the branding can arrive, hold and then leave. The ceiling
# is the clip, so the duration cap above does not apply to it.
MAX_SHOT_DURATION = 40.0

# The branded panel. Only PART of the frame is ever branded — the point of the
# composite is that the real room survives — so anything that lingers past the
# transition is confined to this strip and nothing else is.
PANEL_WIDTH = 0.26            # fraction of frame width
PANEL_MARGIN = 0.025          # gap to the frame edge
PANEL_TOP = 0.08
PANEL_BOTTOM = 0.70           # stops above the caption band
PANEL_HOLD_SEC = 3.5          # how long it stays before it fades away
PANEL_IN_SEC = 0.15           # after the cut, when it starts arriving
PANEL_FADE_SEC = 0.6          # how long it takes to leave

MOVES = ("sweep", "wipe", "drift", "scan", "stamp")
BLENDS = ("normal", "screen", "plus-lighter")
DIRECTIONS = ("left", "right", "up", "down", "none")

# Fallback palette when a frame has no usable accent of its own (a grey office
# wall yields nothing). These are the colours the source art was drawn in.
BRAND_RED = "#de423e"
BRAND_INDIGO = "#3c3386"
BRAND_INK = "#f4f4f8"


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _parse_hex(h: str) -> tuple[int, int, int]:
    s = (h or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return (255, 255, 255)
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (255, 255, 255)


def _to_hex(rgb) -> str:
    r, g, b = (int(_clamp(round(v), 0, 255)) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _relative_luminance(h: str) -> float:
    def chan(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _parse_hex(h)
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _boost(h: str, *, toward_light: bool, amount: float = 0.45) -> str:
    """Push a colour to where it will read over footage without leaving its
    hue — a muddy accent lifted, not replaced."""
    r, g, b = _parse_hex(h)
    if toward_light:
        return _to_hex((r + (255 - r) * amount, g + (255 - g) * amount, b + (255 - b) * amount))
    return _to_hex((r * (1 - amount), g * (1 - amount), b * (1 - amount)))


def legible_over(color: str, backdrop_lum: float, *, min_ratio: float = 2.6) -> str:
    """The same colour, adjusted until it will actually be visible over a
    frame of the given brightness. Patterns tinted from a dark intro vanish
    over dark footage otherwise."""
    backdrop = _to_hex((backdrop_lum, backdrop_lum, backdrop_lum))
    out = color
    for _ in range(6):
        if _contrast(out, backdrop) >= min_ratio:
            return out
        out = _boost(out, toward_light=backdrop_lum < 128, amount=0.22)
    return out


def _hue(h: str) -> float:
    r, g, b = (c / 255.0 for c in _parse_hex(h))
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d < 1e-6:
        return 0.0
    if mx == r:
        deg = 60 * (((g - b) / d) % 6)
    elif mx == g:
        deg = 60 * (((b - r) / d) + 2)
    else:
        deg = 60 * (((r - g) / d) + 4)
    return deg % 360


def _hue_distance(a: str, b: str) -> float:
    d = abs(_hue(a) - _hue(b)) % 360
    return min(d, 360 - d)


def derive_palette(a: Moment, b: Moment) -> dict:
    """The transition's colours.

    The INTRO sets the hue — that is the whole point of the move. Graphics
    that arrive in the card's own colour look like they grew out of it;
    graphics in some other colour look stuck on.

    The incoming shot deliberately does NOT get a vote on hue. Its dominant
    saturated colour is usually the speaker's skin or a warm office wall, and
    tinting brand graphics skin-colour is exactly the failure this avoids. The
    shot only gets to decide LEGIBILITY: the colour is lifted or dropped until
    it reads against that frame's brightness.

    The ramp resolves onto whichever brand anchor is closest in hue to the
    intro, so the seam starts in the card's colour and lands on 42's."""
    am, bm = a.measured, b.measured
    src = am.accent if am.accent_sat >= 0.18 else BRAND_RED
    anchor = min((BRAND_RED, BRAND_INDIGO), key=lambda c: _hue_distance(src, c))
    src = legible_over(src, am.mean_lum)
    dst = legible_over(anchor, bm.mean_lum)
    # If the two ends landed on nearly the same colour the ramp does nothing;
    # split them so the tint visibly travels.
    if _contrast(src, dst) < 1.25:
        dst = _boost(dst, toward_light=bm.mean_lum < 128, amount=0.3)
    flash = _boost(src, toward_light=True, amount=0.62)
    return {
        "from": src,
        "to": dst,
        "ink": bm.ink if _contrast(bm.ink, _to_hex((bm.mean_lum,) * 3)) > 2.5 else BRAND_INK,
        "flash": flash,
    }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _box(x: float, y: float, w: float, h: float) -> dict:
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def _overlap(a: dict, b: dict) -> float:
    """Fraction of box `a` that box `b` covers."""
    ax0, ay0 = float(a.get("x", 0)), float(a.get("y", 0))
    ax1, ay1 = ax0 + float(a.get("w", 0)), ay0 + float(a.get("h", 0))
    bx0, by0 = float(b.get("x", 0)), float(b.get("y", 0))
    bx1, by1 = bx0 + float(b.get("w", 0)), by0 + float(b.get("h", 0))
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    area = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    return (ix * iy) / area


def safe_zones(m: Moment) -> list[dict]:
    """Boxes nothing may sit on after the cut.

    The vision model's `keep_clear` when it ran; otherwise the standing
    geometry of a vertical talking head — the face sits in the upper middle
    and the caption band across the lower third. Guessing those is far better
    than protecting nothing."""
    zones = [
        dict(b) for b in (m.described.get("keep_clear") or [])
        if all(k in b for k in ("x", "y", "w", "h"))
    ]
    if zones:
        return zones
    subject = m.described.get("subject_box")
    if subject and all(k in subject for k in ("x", "y", "w", "h")):
        zones.append({"what": "subject", **subject})
    else:
        zones.append({"what": "face (assumed)", **_box(0.22, 0.10, 0.56, 0.46)})
    zones.append({"what": "caption band (assumed)", **_box(0.0, 0.70, 1.0, 0.18)})
    return zones


def _quiet_column(m: Moment) -> str:
    """Which side of the frame is emptiest — where a vertical strip belongs."""
    busy = m.measured.busy
    if len(busy) < GRID_COLS * GRID_ROWS:
        return "right"
    left = sum(busy[r * GRID_COLS] for r in range(GRID_ROWS))
    right = sum(busy[r * GRID_COLS + (GRID_COLS - 1)] for r in range(GRID_ROWS))
    return "left" if left < right else "right"


def _rel(seconds: float, duration: float) -> float:
    """A transition beat, as a fraction of the whole render.

    The beat is a real duration. In `seam` mode the render is only the join so
    a third of a second is a big fraction of it; in `shot` mode the same beat
    is a small fraction of a nine-second clip. Converting through time keeps
    the sweep the same SPEED either way, instead of stretching it to fill
    whatever is being covered."""
    if duration <= 0:
        return seconds
    return seconds / duration


def brand_region(b: Moment, *, side: str = "auto") -> dict:
    """The strip of frame the branding is allowed to occupy.

    Everything that survives the transition lives in here. The rest of the
    frame — the real room, the ceiling, the wall mark, the light — is never
    touched, which is the whole reason this is a composite and not a re-render.

    `side="auto"` reads the incoming frame's busy grid and takes the emptier
    edge; a caller can force it when the reel needs consistency across shots."""
    chosen = _quiet_column(b) if side == "auto" else side
    if chosen not in ("left", "right"):
        chosen = "right"
    x = PANEL_MARGIN if chosen == "left" else 1.0 - PANEL_MARGIN - PANEL_WIDTH
    return {
        "side": chosen,
        "x": round(x, 4), "y": PANEL_TOP,
        "w": PANEL_WIDTH, "h": round(PANEL_BOTTOM - PANEL_TOP, 4),
    }


def _panel_layers(region: dict, palette: dict, *, duration: float, cut_at: float,
                  hold_sec: float, have: set) -> list[dict]:
    """What stays behind after the cut, and when it goes.

    Deliberately thin: a measuring plate and a bead spine, inside the strip.
    The transition may fill the frame — that is what a transition is — but what
    LINGERS over a person talking has to be quiet enough to ignore."""
    if duration <= 0:
        return []
    arrive = (cut_at + PANEL_IN_SEC) / duration
    settled = (cut_at + PANEL_IN_SEC + 0.55) / duration
    gone = (cut_at + PANEL_IN_SEC + hold_sec + PANEL_FADE_SEC) / duration
    arrive, settled = _clamp(arrive, 0.0, 0.98), _clamp(settled, 0.02, 0.99)
    gone = _clamp(gone, settled + 0.01, 1.0)
    # Enter from beyond whichever edge the strip sits on.
    entry = "left" if region["side"] == "right" else "right"
    box = {k: region[k] for k in ("x", "y", "w", "h")}

    out: list[dict] = []
    if "grid" in have:
        out.append({
            "pattern": "grid", "move": "wipe", "direction": entry,
            "start": arrive, "end": settled,
            "box": _box(box["x"] - 0.01, box["y"] - 0.02, box["w"] + 0.02, box["h"] + 0.04),
            "rotate": 90, "scale": 1.0,
            "color_from": palette["from"], "color_to": palette["to"],
            "opacity": 0.4, "blend": "screen", "use_art": False,
            "parallax": 0.0, "reveal": False,
            "residue": 0.18, "residue_end": gone,
        })
    if "beads_row" in have:
        # The bead art is a wide banner; a quarter-turn stands it up. The box
        # stays the ON-SCREEN footprint — the renderer swaps the art's own
        # dimensions inside it.
        spine_w = min(box["w"] * 0.42, 0.1)
        out.append({
            "pattern": "beads_row", "move": "wipe", "direction": entry,
            "start": arrive, "end": settled,
            "box": _box(box["x"] + box["w"] - spine_w, box["y"], spine_w, box["h"]),
            "rotate": 90, "scale": 1.0,
            "color_from": palette["to"], "color_to": palette["to"],
            "opacity": 0.82, "blend": "screen", "use_art": False,
            "parallax": 0.0, "reveal": False,
            "residue": 0.6, "residue_end": gone,
        })
    return out


def _sweep_direction(a: Moment) -> str:
    """Travel with whatever the intro was already doing; a sweep that fights
    the outgoing motion reads as a mistake."""
    motion = a.measured.motion
    if motion in ("left", "right", "up", "down") and a.measured.motion_strength >= 0.15:
        return motion
    return "up"


def _opposite(d: str) -> str:
    return {"left": "right", "right": "left", "up": "down", "down": "up"}.get(d, "none")


# ---------------------------------------------------------------------------
# The deterministic composition
# ---------------------------------------------------------------------------

def compose(
    a: Moment, b: Moment, *, duration: float, cut_at: float,
    cover: str = "seam", side: str = "auto", hold_sec: float = PANEL_HOLD_SEC,
) -> dict:
    """Lay out the seam from the kit by rule.

    The shape of the move: the grid arrives first as a measuring plate, the
    structure sweeps through and CARRIES the cut (it is the reveal mask, so
    the incoming footage is uncovered by the graphic rather than dissolved
    into), and the bead field scans past just after.

    Everything in that list LEAVES. What remains is a narrow branded panel on
    one edge, which arrives with the cut, holds for `hold_sec`, and then fades
    off — because the background of this reel is a real room that is worth
    keeping, and a graphic parked over it for eight seconds is a sticker.

    `cover="shot"` extends the render across the whole talking head so the
    panel has somewhere to leave; `cover="seam"` keeps the old behaviour of
    rendering only the join."""
    palette = derive_palette(a, b)
    have = {p.slug for p in pattern_kit.kit_or_empty()}
    direction = _sweep_direction(a)
    region = brand_region(b, side=side)
    cut = _clamp(cut_at / duration, 0.02, 0.85) if duration > 0 else 0.35

    layers: list[dict] = []
    # Every transition beat is a REAL duration. Expressed as fractions they
    # would stretch to fill whatever is being covered, so a sweep that reads as
    # snappy across a 3 s seam would crawl across a 9 s shot.
    rel = lambda sec: _rel(sec, duration)   # noqa: E731

    # 1. The measuring plate. Present from the first frame, so the graphics
    #    that follow look like they are being drawn ON something. It is part of
    #    the transition, so it clears shortly after the cut — a full-frame
    #    texture held over the whole shot is exactly the "everything is
    #    branded" look this is meant to avoid.
    if "grid" in have:
        layers.append({
            "pattern": "grid", "move": "drift", "direction": _opposite(direction),
            "start": 0.0, "end": _clamp(cut + rel(0.9), 0.05, 1.0),
            "box": _box(-0.1, -0.1, 1.2, 1.2),
            "rotate": 0, "scale": 1.0,
            "color_from": palette["from"], "color_to": palette["to"],
            "opacity": 0.3, "blend": "screen", "use_art": False,
            "parallax": 0.15, "reveal": False, "residue": 0.0,
        })

    # 2. The schematic, drawn on behind the card while it is still up. Sits in
    #    the outgoing frame's world, so it leaves before the cut resolves.
    if "blueprint" in have:
        layers.append({
            "pattern": "blueprint", "move": "stamp", "direction": "none",
            "start": 0.0, "end": _clamp(cut + rel(0.66), 0.05, 0.95),
            "box": _box(0.06, 0.08, 0.88, 0.7),
            "rotate": 0, "scale": 1.0,
            "color_from": palette["from"], "color_to": palette["from"],
            "opacity": 0.42, "blend": "screen", "use_art": False,
            "parallax": 0.3, "reveal": False, "residue": 0.0,
        })

    # 3. The hero. This layer is the cut: it sweeps across and the incoming
    #    footage is revealed through its shape.
    if "structure" in have:
        layers.append({
            "pattern": "structure", "move": "sweep", "direction": direction,
            "start": _clamp(cut - rel(1.02), 0.0, 0.7),
            "end": _clamp(cut + rel(1.2), 0.05, 1.0),
            "box": _box(-0.25, -0.2, 1.5, 1.4),
            "rotate": -8, "scale": 1.25,
            "color_from": palette["from"], "color_to": palette["to"],
            "opacity": 0.95, "blend": "normal", "use_art": False,
            "parallax": 0.6, "reveal": True, "residue": 0.0,
        })

    # 4. The data band, riding out on its own decay. Placed below the face,
    #    above the caption band, travelling the way its density falls. It is a
    #    transition beat, so it is gone shortly after the cut.
    if "beads_field" in have:
        layers.append({
            "pattern": "beads_field", "move": "scan", "direction": "right",
            "start": _clamp(cut + rel(0.06), 0.02, 0.9),
            "end": _clamp(cut + rel(1.65), 0.1, 1.0),
            "box": _box(-0.05, 0.56, 1.1, 0.12),
            "rotate": 0, "scale": 1.0,
            "color_from": palette["to"], "color_to": palette["to"],
            "opacity": 0.62, "blend": "screen", "use_art": False,
            "parallax": 0.4, "reveal": False, "residue": 0.0,
        })

    # 5. The branded panel — the only thing that outlives the transition, and
    #    the only thing confined to a region. It holds, then it goes.
    layers.extend(_panel_layers(region, palette, duration=duration, cut_at=cut_at,
                                hold_sec=hold_sec, have=have))

    leaves_at = cut_at + PANEL_IN_SEC + hold_sec + PANEL_FADE_SEC
    return {
        "style": "measured sweep",
        "rationale": (
            f"The grid establishes, the plan draws behind the card, the lattice sweeps "
            f"{direction} and hands over the cut. Everything then clears except a narrow "
            f"panel down the {region['side']}, which holds for {hold_sec:.1f}s and fades "
            f"at {leaves_at:.1f}s — the room itself is never touched."
        ),
        "brand_region": region,
        "cover": cover,
        "duration_sec": round(duration, 3),
        "cut_at_sec": round(cut_at, 3),
        "palette": palette,
        "flash": {"at": round(cut, 3), "strength": 0.34},
        "camera": {"shake": 0.18, "push": 0.1},
        "grain": 0.06,
        "vignette": 0.12,
        "layers": layers,
        "planner": "composed",
    }


# ---------------------------------------------------------------------------
# Invariants — applied to every recipe, whoever wrote it
# ---------------------------------------------------------------------------

def enforce(recipe: dict, a: Moment, b: Moment, *, duration: float, cut_at: float,
            cover: str = "seam", side: str = "auto",
            hold_sec: float = PANEL_HOLD_SEC) -> dict:
    """Make a recipe safe and renderable. Runs on the model's output and on
    the composition alike, so the guarantees hold regardless of who planned."""
    have = {p.slug for p in pattern_kit.kit_or_empty()}
    zones = safe_zones(b)
    cut_frac = _clamp(cut_at / duration, 0.02, 0.95) if duration > 0 else 0.35
    region = recipe.get("brand_region")
    if not (isinstance(region, dict) and all(k in region for k in ("x", "y", "w", "h"))):
        region = brand_region(b, side=side)

    out = dict(recipe)
    ceiling = MAX_SHOT_DURATION if cover == "shot" else MAX_DURATION
    out["duration_sec"] = round(_clamp(float(out.get("duration_sec") or duration),
                                       MIN_DURATION, ceiling), 3)
    out["brand_region"] = region
    out["cover"] = cover
    out["cut_at_sec"] = round(_clamp(float(out.get("cut_at_sec") or cut_at),
                                     0.05, out["duration_sec"] - 0.05), 3)

    palette = out.get("palette") or {}
    base = derive_palette(a, b)
    for key in ("from", "to", "ink", "flash"):
        val = str(palette.get(key) or "")
        palette[key] = val if val.startswith("#") and len(val) in (4, 7) else base[key]
    # A tint that cannot be seen over the incoming frame is not a choice we honour.
    palette["to"] = legible_over(palette["to"], b.measured.mean_lum)
    palette["from"] = legible_over(palette["from"], a.measured.mean_lum)
    out["palette"] = palette

    clean: list[dict] = []
    for raw in (out.get("layers") or []):
        if not isinstance(raw, dict):
            continue
        slug = str(raw.get("pattern") or "")
        if slug not in have:
            log.info("[planner] dropping layer for unknown pattern %r", slug)
            continue
        pat = pattern_kit.get(slug)
        layer = {
            "pattern": slug,
            "move": raw.get("move") if raw.get("move") in MOVES else "drift",
            "direction": raw.get("direction") if raw.get("direction") in DIRECTIONS else "none",
            "start": _clamp(float(raw.get("start", 0.0) or 0.0), 0.0, 1.0),
            "end": _clamp(float(raw.get("end", 1.0) or 1.0), 0.0, 1.0),
            "rotate": _clamp(float(raw.get("rotate", 0) or 0), -180, 180),
            "scale": _clamp(float(raw.get("scale", 1.0) or 1.0), 0.15, 3.0),
            "opacity": _clamp(float(raw.get("opacity", 0.6) or 0.6), 0.0, 1.0),
            "blend": raw.get("blend") if raw.get("blend") in BLENDS else "normal",
            "use_art": bool(raw.get("use_art")) and bool(pat and pat.art),
            "parallax": _clamp(float(raw.get("parallax", 0.0) or 0.0), 0.0, 1.0),
            "reveal": bool(raw.get("reveal")),
            "residue": _clamp(float(raw.get("residue", 0.0) or 0.0), 0.0, 1.0),
            "residue_end": _clamp(float(raw.get("residue_end", 1.0) or 1.0), 0.0, 1.0),
            "color_from": str(raw.get("color_from") or palette["from"]),
            "color_to": str(raw.get("color_to") or palette["to"]),
        }
        if layer["end"] <= layer["start"]:
            layer["end"] = _clamp(layer["start"] + 0.2, 0.0, 1.0)
        box = raw.get("box") if isinstance(raw.get("box"), dict) else {}
        layer["box"] = _box(
            _clamp(float(box.get("x", 0.0) or 0.0), -1.0, 1.5),
            _clamp(float(box.get("y", 0.0) or 0.0), -1.0, 1.5),
            _clamp(float(box.get("w", 1.0) or 1.0), 0.02, 2.5),
            _clamp(float(box.get("h", 1.0) or 1.0), 0.02, 2.5),
        )
        for key in ("color_from", "color_to"):
            v = layer[key]
            if not (v.startswith("#") and len(v) in (4, 7)):
                layer[key] = palette["to"]

        # The invariant that matters. A layer still on screen after the cut
        # must not HIDE the face or the captions. A sweep that is gone by then
        # may cross anything — that is the point of a sweep.
        #
        # "Hide" is measured, not assumed. What occludes is ink times opacity:
        # the grid is 0.3% ink, so at any opacity it cannot obscure a face,
        # while the lattice is 17% ink and at full opacity plainly can. Judging
        # by bounding box alone would strip the atmosphere out of every
        # transition to protect a face from a texture that is 99.7% holes.
        lingers = layer["end"] > cut_frac + 0.05 or layer["residue"] > 0.02
        held = layer["residue"] if layer["residue"] > 0.02 else layer["opacity"]
        occlusion = (pat.coverage if pat else 0.5) * held

        # A layer that HOLDS over the talking head is branding, not transition,
        # and branding lives in the strip. The room keeps the rest of the frame
        # — that is the difference between this and repainting the background.
        if layer["residue"] > 0.02:
            layer["box"] = _confine(layer["box"], region)
            if layer["residue_end"] >= 0.999 and cover == "shot":
                # Nothing brands a whole shot by accident. Give it the standard
                # exit rather than letting it sit there to the last frame.
                layer["residue_end"] = _clamp(
                    (cut_at + PANEL_IN_SEC + hold_sec + PANEL_FADE_SEC)
                    / max(duration, 0.01), 0.05, 1.0)

        if lingers and not layer["reveal"] and occlusion > 0.06:
            worst = max((_overlap(zone, layer["box"]) for zone in zones), default=0.0)
            if worst > 0.22:
                fixed = _relocate(layer["box"], zones, b)
                if fixed is None:
                    if layer["residue"] > 0.02:
                        layer["residue"] = 0.0
                    layer["end"] = min(layer["end"], _clamp(cut_frac + 0.04, 0.0, 1.0))
                    log.info("[planner] layer %s cannot clear the safe zones — cut short", slug)
                else:
                    log.info("[planner] layer %s moved off a safe zone", slug)
                    layer["box"] = fixed

        clean.append(layer)

    if not clean:
        log.warning("[planner] recipe had no usable layers — composing instead")
        return enforce(
            compose(a, b, duration=duration, cut_at=cut_at, cover=cover,
                    side=side, hold_sec=hold_sec),
            a, b, duration=duration, cut_at=cut_at, cover=cover, side=side,
            hold_sec=hold_sec)

    out["layers"] = clean[:6]          # more than six reads as noise, not design
    flash = out.get("flash") if isinstance(out.get("flash"), dict) else {}
    out["flash"] = {
        "at": _clamp(float(flash.get("at", cut_frac) or cut_frac), 0.0, 1.0),
        "strength": _clamp(float(flash.get("strength", 0.3) or 0.0), 0.0, 0.8),
    }
    camera = out.get("camera") if isinstance(out.get("camera"), dict) else {}
    out["camera"] = {
        "shake": _clamp(float(camera.get("shake", 0.15) or 0.0), 0.0, 0.5),
        "push": _clamp(float(camera.get("push", 0.1) or 0.0), 0.0, 0.4),
    }
    out["grain"] = _clamp(float(out.get("grain", 0.05) or 0.0), 0.0, 0.3)
    out["vignette"] = _clamp(float(out.get("vignette", 0.1) or 0.0), 0.0, 0.5)
    out["style"] = str(out.get("style") or "untitled")[:80]
    out["rationale"] = str(out.get("rationale") or "")[:600]
    out["safe_zones"] = zones
    return out


def _confine(box: dict, region: dict) -> dict:
    """Shrink and slide a box until it sits inside the branded strip."""
    rx, ry = float(region["x"]), float(region["y"])
    rw, rh = float(region["w"]), float(region["h"])
    w = min(float(box.get("w", rw)), rw)
    h = min(float(box.get("h", rh)), rh)
    x = _clamp(float(box.get("x", rx)), rx, rx + rw - w)
    y = _clamp(float(box.get("y", ry)), ry, ry + rh - h)
    return _box(x, y, w, h)


def _relocate(box: dict, zones: list[dict], b: Moment) -> Optional[dict]:
    """Slide a box into the emptiest place that clears every safe zone, keeping
    its size. None when it simply does not fit anywhere."""
    w, h = float(box.get("w", 0.2)), float(box.get("h", 0.2))
    if w >= 0.98 and h >= 0.98:
        return None
    busy = b.measured.busy
    candidates: list[tuple[float, dict]] = []
    steps = 9
    for iy in range(steps):
        for ix in range(steps):
            x = (1.0 - w) * ix / max(1, steps - 1)
            y = (1.0 - h) * iy / max(1, steps - 1)
            cand = _box(x, y, w, h)
            if max((_overlap(z, cand) for z in zones), default=0.0) > 0.06:
                continue
            cx, cy = x + w / 2, y + h / 2
            cell = min(GRID_ROWS - 1, int(cy * GRID_ROWS)) * GRID_COLS + \
                min(GRID_COLS - 1, int(cx * GRID_COLS))
            detail = busy[cell] if cell < len(busy) else 0.5
            drift = abs(cx - (float(box.get("x", 0)) + w / 2)) + \
                abs(cy - (float(box.get("y", 0)) + h / 2))
            candidates.append((detail * 2.0 + drift, cand))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


# ---------------------------------------------------------------------------
# The model planner
# ---------------------------------------------------------------------------

def _layer_schema() -> dict:
    return {
        "type": "OBJECT",
        "properties": {
            "pattern": {"type": "STRING", "description": "Exact slug from the kit."},
            "move": {"type": "STRING", "description": " | ".join(MOVES)},
            "direction": {"type": "STRING", "description": " | ".join(DIRECTIONS)},
            "start": {"type": "NUMBER", "description": "0..1 fraction of the transition."},
            "end": {"type": "NUMBER", "description": "0..1, greater than start."},
            "box": {
                "type": "OBJECT",
                "description": "Fractional placement; may exceed 0..1 so a layer can start off-frame.",
                "properties": {"x": {"type": "NUMBER"}, "y": {"type": "NUMBER"},
                               "w": {"type": "NUMBER"}, "h": {"type": "NUMBER"}},
                "required": ["x", "y", "w", "h"],
            },
            "rotate": {"type": "NUMBER"},
            "scale": {"type": "NUMBER"},
            "color_from": {"type": "STRING", "description": "#rrggbb"},
            "color_to": {"type": "STRING", "description": "#rrggbb"},
            "opacity": {"type": "NUMBER", "description": "0..1 peak opacity."},
            "blend": {"type": "STRING", "description": " | ".join(BLENDS)},
            "use_art": {"type": "BOOLEAN", "description": "Original colours instead of a tint (structure only)."},
            "parallax": {"type": "NUMBER", "description": "0..1 extra travel, for depth."},
            "reveal": {"type": "BOOLEAN",
                       "description": "This layer's shape UNCOVERS the incoming clip. At most one layer."},
            "residue": {"type": "NUMBER",
                        "description": "0..1 opacity it settles to and holds over the talking head. 0 = it leaves."},
            "residue_end": {"type": "NUMBER",
                            "description": "0..1 point at which the residue has faded to nothing. Only meaningful with residue > 0."},
        },
        "required": ["pattern", "move", "start", "end", "box", "opacity"],
    }


RECIPE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "style": {"type": "STRING", "description": "Two or three words naming the move."},
        "rationale": {"type": "STRING", "description": "One sentence: why this joins THESE two frames."},
        "palette": {
            "type": "OBJECT",
            "properties": {"from": {"type": "STRING"}, "to": {"type": "STRING"},
                           "ink": {"type": "STRING"}, "flash": {"type": "STRING"}},
            "required": ["from", "to"],
        },
        "flash": {
            "type": "OBJECT",
            "properties": {"at": {"type": "NUMBER"}, "strength": {"type": "NUMBER"}},
        },
        "camera": {
            "type": "OBJECT",
            "properties": {"shake": {"type": "NUMBER"}, "push": {"type": "NUMBER"}},
        },
        "grain": {"type": "NUMBER"},
        "vignette": {"type": "NUMBER"},
        "layers": {"type": "ARRAY", "items": _layer_schema()},
    },
    "required": ["style", "rationale", "palette", "layers"],
}


PLANNER_SYSTEM = """You are a motion designer cutting ONE seam: the last beat of a title card
into the first beats of a talking-head shot, for a vertical social reel by 42 Abu Dhabi.

You are not generating imagery. You are placing a fixed kit of brand graphics over footage
that already exists, and animating them. Every layer you write is rendered exactly as
specified, so specify like a designer, not like a prompt.

What makes this land:
- The cut is CARRIED by a graphic. Set `reveal: true` on exactly one layer and the incoming
  shot is uncovered through that layer's shape. That is the transition; everything else is
  dressing around it.
- Colour comes from the outgoing card and resolves into the incoming shot, so the graphics
  look like they grew out of the intro instead of being pasted onto the reel.
- Most layers LEAVE. A transition that dumps five graphics onto a talking head is a mess.
  Give at most one layer a `residue` above 0.25, and place it where the incoming frame is
  empty — use the busy grid you are given, do not guess.
- Travel with the outgoing motion, never against it.
- Dense patterns sweep past; sparse ones may linger.

Hard rules (a layer breaking one is moved or cut automatically, so write within them):
- Nothing that is still on screen after the cut may overlap a keep-clear box: face, mouth,
  captions, logo.
- No layer may use a pattern slug that is not in the kit.
- Timing values are fractions of the whole transition, 0..1.

Return ONLY the recipe object."""


def _brief_for(a: Moment, b: Moment, precedents: str, base: dict, seconds: float,
               cut_at: float, *, cover: str = "seam",
               hold_sec: float = PANEL_HOLD_SEC) -> str:
    cut_frac = cut_at / seconds if seconds else 0.35
    zones = "\n".join(
        f"  - {z.get('what', 'area')}: x={z.get('x'):.2f} y={z.get('y'):.2f} "
        f"w={z.get('w'):.2f} h={z.get('h'):.2f}"
        for z in safe_zones(b)
    )
    bm = b.measured
    grid = "\n".join(
        "    " + " ".join(f"{bm.busy[r * GRID_COLS + c]:.2f}" for c in range(GRID_COLS))
        for r in range(GRID_ROWS)
        if (r + 1) * GRID_COLS <= len(bm.busy)
    )
    return f"""THE KIT — the only graphics that exist:
{pattern_kit.catalogue()}

OUTGOING FRAME (end of the intro card, {a.at_sec:.2f}s in):
{a.as_text()}

INCOMING FRAME (start of the talking head, {b.at_sec:.2f}s in):
{b.as_text()}

WHERE THE INCOMING FRAME IS BUSY ({GRID_COLS}x{GRID_ROWS}, 0 empty .. 1 packed):
{grid}

KEEP CLEAR in the incoming frame (fractions):
{zones}

TIMING: this render is {seconds:.2f}s long and the cut lands at {cut_frac:.2f}
(the card is on screen before it, the talking head after).
{_coverage_note(cover, base, hold_sec, seconds)}

{precedents}

A WORKING BASELINE — the deterministic composition for this seam. Improve on it knowing
what is actually in these two frames; keep what is right:
{_summarise(base)}

Design this seam."""

def _coverage_note(cover: str, base: dict, hold_sec: float, seconds: float) -> str:
    """What the model must understand about WHAT IT IS COVERING.

    Covering the whole shot is the case that goes wrong: a planner that treats
    it like a long transition brands the entire frame for nine seconds, which
    throws away the real room the reel was shot in."""
    if cover != "shot":
        return "You are covering the JOIN only, so every layer ends with the render."
    r = base.get("brand_region") or {}
    return "\n".join([
        "You are covering the WHOLE talking head, not just the join. Its background is a",
        "REAL ROOM, and it is the best thing in the shot — it must survive untouched. So:",
        "- transition layers may fill the frame, but all of them must be GONE within about",
        "  a second and a half of the cut.",
        f"- exactly ONE narrow branded panel may remain, and only inside the strip "
        f"x={r.get('x')} y={r.get('y')} w={r.get('w')} h={r.get('h')} (the {r.get('side')} edge).",
        f"- that panel must fade away after about {hold_sec:.1f}s — set `residue_end`.",
        f"- nothing else may hold. A graphic parked over someone for {seconds:.0f}s is a sticker,",
        "  and branding the whole frame is the exact failure this replaces.",
    ])


def _summarise(recipe: dict) -> str:
    lines = [f"style: {recipe.get('style')}", f"palette: {recipe.get('palette')}"]
    for l in recipe.get("layers") or []:
        lines.append(
            f"- {l['pattern']}: {l['move']} {l.get('direction')} "
            f"{l['start']:.2f}→{l['end']:.2f} box={l['box']} op={l['opacity']:.2f} "
            f"{'REVEAL ' if l.get('reveal') else ''}residue={l.get('residue', 0):.2f}"
            + (f" until {l.get('residue_end', 1):.2f}" if l.get("residue", 0) > 0.02 else "")
        )
    return "\n".join(lines)


async def plan(
    a: Moment, b: Moment, *, duration: float = PRE_SEC + POST_SEC,
    cut_at: float = PRE_SEC, precedents: Optional[list[dict]] = None,
    notes: str = "", cover: str = "seam", side: str = "auto",
    hold_sec: float = PANEL_HOLD_SEC,
) -> dict:
    """Design the seam. Never raises — falls back to the composition."""
    base = compose(a, b, duration=duration, cut_at=cut_at, cover=cover,
                   side=side, hold_sec=hold_sec)
    try:
        from .vertex_client import gemini_text

        prompt = _brief_for(a, b, format_precedents(precedents or []), base, duration,
                            cut_at, cover=cover, hold_sec=hold_sec)
        if notes.strip():
            prompt += f"\n\nTHE TEAM ASKED FOR:\n{notes.strip()[:600]}"
        out = await gemini_text(
            prompt, system=PLANNER_SYSTEM, json_schema=RECIPE_SCHEMA,
            temperature=0.65, max_tokens=4000,
        )
        if not isinstance(out, dict) or not out.get("layers"):
            raise ValueError("planner returned no layers")
        out["duration_sec"] = duration
        out["cut_at_sec"] = cut_at
        out["planner"] = "gemini"
        return enforce(out, a, b, duration=duration, cut_at=cut_at, cover=cover,
                       side=side, hold_sec=hold_sec)
    except Exception as e:  # noqa: BLE001 — the composition is a real design, not a stub
        log.warning("[planner] model planning unavailable (%s) — using the composition",
                    str(e)[:200])
        return enforce(base, a, b, duration=duration, cut_at=cut_at, cover=cover,
                       side=side, hold_sec=hold_sec)
