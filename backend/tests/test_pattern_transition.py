"""The intro→main seam: frame selection, palette, and the safe-zone invariants.

These cover the decisions that are easy to get silently wrong and expensive to
notice — a transition designed for a black frame, brand graphics tinted the
speaker's skin colour, or a pattern parked over a face. Each of those happened
during the build; each has a test here.

No ffmpeg, no network, no Remotion: the moments are constructed directly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services import pattern_kit
from app.services import transition_planner as planner
from app.services.rebrand_moments import GRID_COLS, GRID_ROWS, Measured, Moment


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _measured(**over) -> Measured:
    base = dict(
        mean_lum=40.0, stddev=45.0, is_dark=True,
        palette=[{"hex": "#101018", "weight": 0.5, "sat": 0.1, "lum": 0.07},
                 {"hex": "#2a56a8", "weight": 0.2, "sat": 0.6, "lum": 0.41}],
        accent="#2a56a8", accent_sat=0.6, ink="#f4f4f8",
        busy=[0.1] * (GRID_COLS * GRID_ROWS), quietest_cell=0, busiest_cell=4,
        edge_energy=0.2, motion="up", motion_strength=0.4,
    )
    base.update(over)
    return Measured(**base)


def _moment(kind="intro_out", **over) -> Moment:
    described = over.pop("described", {})
    return Moment(kind=kind, ref="x", frame=Path("nope.png"), at_sec=1.0,
                  measured=_measured(**over), described=described)


@pytest.fixture(autouse=True)
def _kit(monkeypatch):
    """A stand-in kit, so the tests do not depend on the prepared PNGs.

    Coverage numbers are the real measured ones — they drive the occlusion
    rule, so faking them would test nothing."""
    fake = [
        pattern_kit.PreparedPattern(
            slug="grid", role="grid", description="grid",
            mask="branding/patterns/grid.mask.png", art=None,
            width=2400, height=1200, aspect=2.0, coverage=0.003,
            source_color="#a2a2a2"),
        pattern_kit.PreparedPattern(
            slug="structure", role="structure", description="lattice",
            mask="branding/patterns/structure.mask.png",
            art="branding/patterns/structure.art.png",
            width=2400, height=2033, aspect=1.18, coverage=0.174,
            source_color="#cbc8c8"),
        pattern_kit.PreparedPattern(
            slug="beads_row", role="strip", description="beads",
            mask="branding/patterns/beads_row.mask.png", art=None,
            width=2400, height=465, aspect=5.16, coverage=0.194,
            source_color="#dd423e"),
        pattern_kit.PreparedPattern(
            slug="blueprint", role="blueprint", description="plan",
            mask="branding/patterns/blueprint.mask.png", art=None,
            width=2112, height=2400, aspect=0.88, coverage=0.104,
            source_color="#de423e"),
        pattern_kit.PreparedPattern(
            slug="beads_field", role="field", description="field",
            mask="branding/patterns/beads_field.mask.png", art=None,
            width=2400, height=685, aspect=3.5, coverage=0.289,
            source_color="#3c3386"),
    ]
    monkeypatch.setattr(pattern_kit, "kit_or_empty", lambda: fake)
    monkeypatch.setattr(pattern_kit, "get",
                        lambda slug: next((p for p in fake if p.slug == slug), None))
    return fake


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

def test_hue_comes_from_the_intro_not_the_speaker():
    """The shot's dominant colour is usually skin. It must not tint the kit.

    This is the whole point of "colour it from the intro": a warm face makes
    the incoming frame's accent a skin tone, and a brand graphic in skin tone
    looks like a mistake rather than a brand."""
    intro = _moment(accent="#2a56a8", accent_sat=0.62)          # card blue
    shot = _moment("main_in", accent="#e7af8c", accent_sat=0.66,  # skin
                   mean_lum=90.0)
    pal = planner.derive_palette(intro, shot)

    assert planner._hue_distance(pal["from"], "#2a56a8") < 25, pal
    # Nothing in the ramp may land near the skin hue it was handed.
    for end in ("from", "to"):
        assert planner._hue_distance(pal[end], "#e7af8c") > 40, (end, pal)


def test_a_colourless_intro_falls_back_to_brand():
    intro = _moment(accent="#8a8a8a", accent_sat=0.02)
    pal = planner.derive_palette(intro, _moment("main_in"))
    assert planner._hue_distance(pal["from"], planner.BRAND_RED) < 30, pal


def test_tint_is_lifted_until_it_can_be_seen():
    """A dark accent over dark footage is invisible; it gets lifted, in hue."""
    dark = _moment(accent="#101828", accent_sat=0.45, mean_lum=18.0)
    lifted = planner.legible_over(dark.measured.accent, dark.measured.mean_lum)
    assert planner._relative_luminance(lifted) > \
        planner._relative_luminance(dark.measured.accent)
    assert planner._hue_distance(lifted, dark.measured.accent) < 20


# ---------------------------------------------------------------------------
# Safe zones
# ---------------------------------------------------------------------------

def _face_moment() -> Moment:
    return _moment("main_in", mean_lum=90.0, described={
        "subject": "person",
        "keep_clear": [{"what": "face", "x": 0.25, "y": 0.10, "w": 0.5, "h": 0.4}],
    })


def test_a_dense_pattern_is_moved_off_the_face():
    shot = _face_moment()
    recipe = {
        "style": "x", "layers": [{
            "pattern": "beads_row", "move": "wipe", "direction": "down",
            "start": 0.5, "end": 1.0,
            "box": {"x": 0.3, "y": 0.15, "w": 0.4, "h": 0.3},   # on the face
            "opacity": 0.8, "residue": 0.6, "blend": "screen",
        }],
    }
    out = planner.enforce(recipe, _moment(), shot, duration=3.0, cut_at=0.8)
    layer = out["layers"][0]
    assert planner._overlap(
        {"x": 0.25, "y": 0.10, "w": 0.5, "h": 0.4}, layer["box"]) <= 0.07, layer


def test_a_sweep_that_leaves_before_the_cut_may_cross_anything():
    """A transition is allowed to cover the frame — that is what it is for.
    Only what LINGERS is policed."""
    shot = _face_moment()
    recipe = {
        "style": "x", "layers": [{
            "pattern": "structure", "move": "sweep", "direction": "up",
            "start": 0.0, "end": 0.2,           # gone well before the cut
            "box": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "opacity": 1.0, "residue": 0.0,
        }],
    }
    out = planner.enforce(recipe, _moment(), shot, duration=3.0, cut_at=0.8)
    assert out["layers"][0]["box"] == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}


def test_a_sparse_texture_is_not_relocated_by_the_face_rule():
    """The face rule is about occlusion, not bounding boxes.

    The grid is 0.3% ink. Judging it by its box would strip the atmosphere out
    of every transition to protect a face from something that is 99.7% holes.
    A transition layer (residue 0) keeps its full-frame box."""
    shot = _face_moment()
    box = {"x": -0.1, "y": -0.1, "w": 1.2, "h": 1.2}
    recipe = {
        "style": "x", "layers": [{
            "pattern": "grid", "move": "drift", "direction": "down",
            "start": 0.0, "end": 0.9, "box": dict(box),
            "opacity": 0.3, "residue": 0.0, "blend": "screen",
        }],
    }
    out = planner.enforce(recipe, _moment(), shot, duration=3.0, cut_at=0.8)
    layer = out["layers"][0]
    assert layer["box"] == box, "a layer that leaves may cover the frame"
    assert layer["end"] == pytest.approx(0.9)


def _inside(box: dict, region: dict) -> bool:
    return (box["x"] >= region["x"] - 1e-6
            and box["y"] >= region["y"] - 1e-6
            and box["x"] + box["w"] <= region["x"] + region["w"] + 1e-6
            and box["y"] + box["h"] <= region["y"] + region["h"] + 1e-6)


def test_anything_that_lingers_is_pulled_into_the_branded_strip():
    """The rule that keeps the real room.

    These reels are shot inside the actual campus, and that background is the
    best thing in the frame. So a graphic may fill the screen while it is
    TRANSITIONING, but the moment it holds over the talking head it becomes
    branding — and branding lives in one strip, never across the picture."""
    shot = _face_moment()
    recipe = {
        "style": "x", "layers": [{
            "pattern": "grid", "move": "drift", "direction": "down",
            "start": 0.0, "end": 0.5,
            "box": {"x": -0.1, "y": -0.1, "w": 1.2, "h": 1.2},   # whole frame
            "opacity": 0.3, "residue": 0.16, "blend": "screen",
        }],
    }
    out = planner.enforce(recipe, _moment(), shot, duration=9.0, cut_at=0.8,
                          cover="shot")
    layer, region = out["layers"][0], out["brand_region"]
    assert _inside(layer["box"], region), (layer["box"], region)
    assert layer["box"]["w"] <= planner.PANEL_WIDTH + 1e-6


def test_a_dense_lingering_layer_is_confined_rather_than_killed():
    """Full-frame, opaque and holding — it is moved into the strip, where it
    is both safe and useful, instead of being discarded."""
    shot = _face_moment()
    recipe = {
        "style": "x", "layers": [{
            "pattern": "structure", "move": "drift", "direction": "none",
            "start": 0.0, "end": 1.0,
            "box": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "opacity": 0.95, "residue": 0.8,
        }],
    }
    out = planner.enforce(recipe, _moment(), shot, duration=9.0, cut_at=0.8,
                          cover="shot")
    layer = out["layers"][0]
    assert _inside(layer["box"], out["brand_region"])
    # The strip is an edge strip, so it clips the generous assumed face box by
    # a few percent at most — well under the threshold that would move it, and
    # nowhere near the middle of the frame where the speaker actually is.
    assert layer["box"]["x"] > 0.5
    for zone in planner.safe_zones(shot):
        assert planner._overlap(zone, layer["box"]) < 0.15


def test_branding_is_never_allowed_to_stay_for_the_whole_shot():
    """"Then disappear after some time" is a guarantee, not a preference.

    A recipe that asks to hold to the last frame gets the standard exit
    instead — a graphic parked over someone for nine seconds is a sticker."""
    shot = _face_moment()
    recipe = {
        "style": "x", "layers": [{
            "pattern": "beads_row", "move": "wipe", "direction": "left",
            "start": 0.1, "end": 0.2,
            "box": {"x": 0.88, "y": 0.1, "w": 0.08, "h": 0.5},
            "opacity": 0.8, "residue": 0.6, "residue_end": 1.0,
        }],
    }
    out = planner.enforce(recipe, _moment(), shot, duration=9.0, cut_at=0.8,
                          cover="shot", hold_sec=3.5)
    end = out["layers"][0]["residue_end"]
    assert end < 1.0
    assert end * 9.0 == pytest.approx(0.8 + planner.PANEL_IN_SEC + 3.5
                                      + planner.PANEL_FADE_SEC, abs=0.05)


def test_covering_the_seam_only_leaves_a_hold_alone():
    """In seam mode the render IS the transition, so a residue that runs to the
    last frame is correct — the next clip continues it."""
    shot = _face_moment()
    recipe = {
        "style": "x", "layers": [{
            "pattern": "beads_row", "move": "wipe", "direction": "left",
            "start": 0.1, "end": 0.2,
            "box": {"x": 0.88, "y": 0.1, "w": 0.08, "h": 0.5},
            "opacity": 0.8, "residue": 0.6, "residue_end": 1.0,
        }],
    }
    out = planner.enforce(recipe, _moment(), shot, duration=3.0, cut_at=0.8,
                          cover="seam")
    assert out["layers"][0]["residue_end"] == 1.0


def test_assumed_zones_protect_a_frame_the_model_never_described():
    """With no vision result there is still a face and a caption band in a
    vertical talking head. Guessing them beats protecting nothing."""
    zones = planner.safe_zones(_moment("main_in"))
    assert any("face" in (z.get("what") or "") for z in zones)
    assert any("caption" in (z.get("what") or "") for z in zones)


# ---------------------------------------------------------------------------
# Recipe hygiene
# ---------------------------------------------------------------------------

def test_unknown_patterns_are_dropped_not_rendered():
    out = planner.enforce(
        {"style": "x", "layers": [
            {"pattern": "not_in_the_kit", "move": "sweep", "start": 0, "end": 1,
             "box": {"x": 0, "y": 0, "w": 1, "h": 1}, "opacity": 1},
            {"pattern": "grid", "move": "drift", "start": 0, "end": 1,
             "box": {"x": 0, "y": 0, "w": 1, "h": 1}, "opacity": 0.3},
        ]},
        _moment(), _moment("main_in"), duration=3.0, cut_at=0.8)
    assert [l["pattern"] for l in out["layers"]] == ["grid"]


def test_an_empty_recipe_falls_back_to_a_real_design():
    """A planner that returns nothing usable must not produce a blank cut."""
    out = planner.enforce({"style": "x", "layers": []},
                          _moment(), _moment("main_in"), duration=3.0, cut_at=0.8)
    assert len(out["layers"]) >= 3
    assert any(l["reveal"] for l in out["layers"])


def test_nonsense_values_are_clamped_rather_than_rendered():
    out = planner.enforce(
        {"style": "x", "layers": [{
            "pattern": "grid", "move": "teleport", "direction": "sideways",
            "start": 0.9, "end": 0.1,             # backwards
            "box": {"x": 99, "y": -99, "w": 0, "h": 500},
            "opacity": 7, "rotate": 9999, "scale": 0, "blend": "chartreuse",
        }]},
        _moment(), _moment("main_in"), duration=99.0, cut_at=-3.0)
    layer = out["layers"][0]
    assert layer["move"] in planner.MOVES
    assert layer["direction"] in planner.DIRECTIONS
    assert layer["blend"] in planner.BLENDS
    assert layer["end"] > layer["start"]
    assert 0.0 <= layer["opacity"] <= 1.0
    assert -180 <= layer["rotate"] <= 180
    assert layer["scale"] >= 0.15
    assert planner.MIN_DURATION <= out["duration_sec"] <= planner.MAX_DURATION
    assert 0 < out["cut_at_sec"] < out["duration_sec"]


def test_the_composition_carries_the_cut_with_one_reveal_layer():
    """Exactly one layer may hand over the cut — two would fight."""
    recipe = planner.compose(_moment(), _moment("main_in"), duration=3.0, cut_at=0.8)
    assert sum(1 for l in recipe["layers"] if l["reveal"]) == 1


def test_the_sweep_travels_with_the_outgoing_motion():
    left = planner.compose(_moment(motion="left", motion_strength=0.5),
                           _moment("main_in"), duration=3.0, cut_at=0.8)
    hero = next(l for l in left["layers"] if l["reveal"])
    assert hero["direction"] == "left"

    still = planner.compose(_moment(motion="none", motion_strength=0.0),
                            _moment("main_in"), duration=3.0, cut_at=0.8)
    assert next(l for l in still["layers"] if l["reveal"])["direction"] == "up"


def test_the_residue_goes_to_the_emptier_side_of_the_incoming_frame():
    busy_left = [0.9 if c == 0 else 0.05 for _ in range(GRID_ROWS)
                 for c in range(GRID_COLS)]
    shot = _moment("main_in", busy=busy_left)
    recipe = planner.compose(_moment(), shot, duration=3.0, cut_at=0.8)
    spine = next(l for l in recipe["layers"] if l["pattern"] == "beads_row")
    assert spine["box"]["x"] > 0.5, "the busy side is the left, so the spine goes right"
