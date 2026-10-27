"""The Icons tab's moving preview, mirrored in numpy (issue 7's ghost).

IconOverlayPlayer plays the scene and draws a canvas over it: the pattern
layer the icon plan names (underlay.pattern_url) at 0.9, the icons, then the
speaker's shape at that moment cut out of both (the plan's masks, two a
second, blended between, scaled up: destination-out). There is no JS test
runner in the repo, so drawRef + cutSpeaker are mirrored here, as
test_rebrand_studio_recolour.py mirrors the live recolour.

The layer must come UNCUT (the preview's layers.png). The target's ink
(icons_pattern.png, _preview_ink) has the FIRST frame's speaker cut out of
it: as soon as she moves, the canvas shows a hole in the pattern shaped like
where she stood — the "ghost window" measured below."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import rebrand_pipeline as pipeline
from app.services import vertex_client
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import speaker_track
from app.services.rebrand_harness import studio

cv2 = pytest.importorskip("cv2")

W, H = 180, 320
DIV = speaker_track.MASK_DIV
W8, H8 = W // DIV, H // DIV
TIMES = [0.0, 0.5, 1.0, 1.5, 2.0]
VIOLET = np.array([0x7F, 0x2B, 0xF2], np.float32)
GROW_PX = 3


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    async def refuse(*_a, **_k):
        raise AssertionError("a paid model call in a test")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse)
    monkeypatch.setattr(vertex_client, "gemini_image", refuse)


def _speaker(t: float) -> np.ndarray:
    """The speaker at t: a figure drifting 40 px to the right over 2 s."""
    m = np.zeros((H, W), np.uint8)
    cv2.ellipse(m, (int(round(60 + 20 * t)), 190), (34, 95), 0, 0, 360, 1, -1)
    return m.astype(bool)


def _frame(t: float) -> np.ndarray:
    """The scene's frame at t (RGB): a grey room, the speaker light."""
    ramp = np.linspace(70, 120, H, dtype=np.float32)[:, None, None]
    f = np.repeat(np.repeat(ramp, W, axis=1), 3, axis=2)
    f[_speaker(t)] = (215, 200, 190)
    return f.astype(np.uint8)


def _pattern() -> np.ndarray:
    """A dense pattern over the whole frame (the worst case: fine stripes)."""
    yy, xx = np.mgrid[0:H, 0:W]
    return ((xx + yy) // 6) % 2 == 0


def _masks() -> list[np.ndarray]:
    """The plan's masks as the moving preview gets them: the speaker at 1/8
    (INTER_AREA, as speaker_track keeps them), stored 0..255."""
    out = []
    for t in TIMES:
        small = cv2.resize(_speaker(t).astype(np.float32), (W8, H8), interpolation=cv2.INTER_AREA)
        out.append(np.clip(np.rint(small * 255), 0, 255).astype(np.uint8) / 255.0)
    return out


def _canvas_alpha(layer_alpha: np.ndarray, masks: list, t: float) -> np.ndarray:
    """drawRef's canvas alpha at t: the layer at 0.9, then cutSpeaker (the
    two samples around t blended, scaled up smoothly, destination-out)."""
    k = 0
    while k + 1 < len(TIMES) and TIMES[k + 1] <= t:
        k += 1
    k2 = min(k + 1, len(masks) - 1)
    span = TIMES[k2] - TIMES[k]
    f = min(1.0, max(0.0, (t - TIMES[k]) / span)) if span > 0 else 0.0
    mix = np.clip(masks[k] * (1 - f) + (masks[k2] * f if f > 0 else 0.0), 0.0, 1.0).astype(np.float32)
    cut = cv2.resize(mix, (W, H), interpolation=cv2.INTER_LINEAR)
    return 0.9 * layer_alpha * (1.0 - cut)


def _ghost(layer_alpha: np.ndarray, t: float) -> float:
    """The share of the pattern the canvas leaves see-through away from the
    speaker (outside her shape at t, grown GROW_PX): a hole in the pattern
    where nobody is."""
    pattern = _pattern()
    here = cv2.dilate(_speaker(t).astype(np.uint8), np.ones((2 * GROW_PX + 1,) * 2, np.uint8)).astype(bool)
    a = _canvas_alpha(layer_alpha, _masks(), t)
    hole = pattern & ~here & (a < 0.45)
    return float(hole.sum()) / float(pattern.sum())


def _scene(tmp_path: Path) -> tuple[studio.Scene, Path]:
    work = tmp_path / "1" / "scene_0_abc"
    work.mkdir(parents=True)
    first = work / "c0_first.png"
    Image.fromarray(_frame(0.0)).save(first)
    seg = pipeline.Segment(kind="scene", index=0, source=first, is_still=False)
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=W, height=H, work=work, options={},
                       run_ctx={}, intro=None, scenes=[seg], outro=None)
    ctx.target_seg = seg
    scene = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=W, height=H, work=work, ctx=ctx,
                         chunks=[work / "c0.mp4"], firsts=[first], seconds=[8.0])
    return scene, first


def _preview(scene: studio.Scene, recipe: rc.Recipe, seed: str, *, layers: bool) -> Path:
    """A made preview of the pattern: its target (the frame with the pattern
    laid in behind the first frame's speaker, as runner.target lays it) and,
    with icons, its uncut layers.png."""
    out = scene.work / "preview" / studio._preview_key(recipe, seed)
    out.mkdir(parents=True)
    behind = _pattern() & ~_speaker(0.0)
    target = _frame(0.0).astype(np.float32)
    target[behind] = VIOLET
    Image.fromarray(target.astype(np.uint8)).save(out / "c0_target.png")
    if layers:
        rgba = np.zeros((H, W, 4), np.uint8)
        rgba[..., :3] = VIOLET.astype(np.uint8)
        rgba[..., 3] = _pattern().astype(np.uint8) * 255
        Image.fromarray(rgba, "RGBA").save(out / studio.LAYERS_PNG)
    (out / "preview.json").write_text(json.dumps({"patterns": [{"colour": "#7f2bf2", "box": None}]}),
                                      encoding="utf-8")
    return out


def _alpha_of(url: str) -> np.ndarray:
    path = studio.resolve_file(url.split("/file/", 1)[1])
    with Image.open(path) as im:
        return np.asarray(im.convert("RGBA"))[..., 3].astype(np.float32) / 255.0


def test_the_moving_preview_lays_the_uncut_layer_and_shows_no_ghost(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    scene, _first = _scene(tmp_path)
    recipe = rc.preset_42_default("scene_0")
    seed = studio.default_seed(1, "scene_0")
    out = _preview(scene, recipe, seed, layers=True)

    pat = asyncio.run(studio._icon_pattern(scene, recipe, seed, None, None, spec=None, final=False))
    assert pat.url.endswith(f"/{studio.LAYERS_PNG}"), pat.url
    # The target's ink still places the icons (where the pattern shows).
    assert pat.ink is not None and pat.ink.shape == (H8, W8) and pat.colour == "#7f2bf2"
    assert (out / "icons_pattern.png").is_file()

    uncut = _alpha_of(pat.url)
    cut_at_0 = _alpha_of(studio.url_for(out / "icons_pattern.png"))
    for t in (0.0, 1.0, 2.0):
        assert _ghost(uncut, t) <= 0.01, (t, _ghost(uncut, t))
    # The old layer: a window in the pattern where she stood at 0 s.
    assert _ghost(cut_at_0, 0.0) <= 0.01
    assert _ghost(cut_at_0, 1.0) > 0.03 and _ghost(cut_at_0, 2.0) > 0.06


def test_an_older_preview_without_layers_still_plays(tmp_path, monkeypatch):
    """A preview made before layers.png (no icon element then): the target's
    ink is the layer, as before."""
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    scene, _first = _scene(tmp_path)
    recipe = rc.preset_42_default("scene_0")
    seed = studio.default_seed(1, "scene_0")
    _preview(scene, recipe, seed, layers=False)
    pat = asyncio.run(studio._icon_pattern(scene, recipe, seed, None, None, spec=None, final=False))
    assert pat.url.endswith("/icons_pattern.png"), pat.url
