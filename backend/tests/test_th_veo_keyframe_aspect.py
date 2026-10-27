"""Every TH v2 Veo submission gets keyframes in exactly the clip's shape.

Run 809: 1248x832 stills and 1280x800 cards went to Veo for a 9:16 reel, and
Veo letterboxed them — horizontal videos with bars inside a vertical frame.
Run 827: Gemini's own 9:16 (768x1344, 1.6% wider) passed the ±6% check and
Veo still added 10 px bars, so a keyframe now goes out at the EXACT shape.
The push-in end frame is made from the conformed start (one reframe at most,
never a second one with its own invented background).
"""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services import card_animation
from app.services import yas_talking_head_v2 as th


def _png(path: Path, size: tuple[int, int]) -> Path:
    Image.new("RGB", size, (90, 120, 150)).save(path)
    return path


def _png_bytes(size: tuple[int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def gen(monkeypatch):
    """_run_image_gen faked: records each call, returns a still of `size`."""
    state = {"calls": [], "size": (768, 1376), "raise": None}

    async def run_image_gen(*, prompt, image_model, source_images=None, aspect="9:16", size=None,
                            conform="reframe"):
        state["calls"].append({"prompt": prompt, "sources": source_images, "aspect": aspect,
                               "engine": image_model, "conform": conform})
        if state["raise"]:
            raise state["raise"]
        return _png_bytes(state["size"]), "image/png"

    monkeypatch.setattr(th, "_run_image_gen", run_image_gen)
    return state


def test_a_landscape_still_is_reframed_before_veo(tmp_path, gen):
    src = _png(tmp_path / "main.png", (1248, 832))
    out = asyncio.run(th._veo_ready(src, "9:16"))
    assert out == tmp_path / "main.9x16.png" and out.exists()
    # The 768x1376 reframe is trimmed to exactly 9:16 (no bars from Veo).
    assert Image.open(out).size == (765, 1360)
    call, = gen["calls"]
    assert call["sources"] == [src] and call["aspect"] == "9:16"
    assert call["engine"] == th.ENGINE_GEMINI_PRO
    assert call["conform"] is None, "the reframe itself is never reframed again"
    assert "RESIZE ONLY" in call["prompt"] and "person" in call["prompt"]
    assert Image.open(src).size == (1248, 832), "the approved still is left untouched"


def test_a_still_already_in_shape_goes_through_untouched(tmp_path, gen):
    src = _png(tmp_path / "main.png", (720, 1280))
    assert asyncio.run(th._veo_ready(src, "9:16")) == src
    assert gen["calls"] == []


def test_the_reframe_is_cached_until_the_still_changes(tmp_path, gen):
    src = _png(tmp_path / "main.png", (1248, 832))
    first = asyncio.run(th._veo_ready(src, "9:16"))
    second = asyncio.run(th._veo_ready(src, "9:16"))
    assert first == second and len(gen["calls"]) == 1


def test_16_9_reels_reframe_portrait_stills(tmp_path, gen):
    gen["size"] = (1376, 768)
    src = _png(tmp_path / "intro.png", (720, 1280))
    out = asyncio.run(th._veo_ready(src, "16:9", card=True))
    assert out.name == "intro.16x9.png"
    assert "title card" in gen["calls"][0]["prompt"] and "every letter" in gen["calls"][0]["prompt"]
    w, h = Image.open(out).size
    assert w * 9 == h * 16


def test_a_reframe_that_comes_back_wrong_uses_the_original(tmp_path, gen):
    gen["size"] = (1248, 832)
    src = _png(tmp_path / "main.png", (1248, 832))
    assert asyncio.run(th._veo_ready(src, "9:16")) == src


def test_a_failed_reframe_never_costs_the_video(tmp_path, gen):
    gen["raise"] = RuntimeError("quota")
    src = _png(tmp_path / "main.png", (1248, 832))
    assert asyncio.run(th._veo_ready(src, "9:16")) == src


def test_an_end_frame_of_its_own_is_put_in_shape_on_its_own(tmp_path, gen, monkeypatch):
    # No push_in spec and an end frame that is not the start: each frame is
    # conformed on its own (no caller in the module sends this today).
    sent = {}

    async def vertex_veo(prompt, **kwargs):
        sent.update(kwargs, prompt=prompt)
        return {"operation_name": "op"}

    monkeypatch.setattr(th, "_vertex_veo_generate", vertex_veo)
    start = _png(tmp_path / "main.png", (1248, 832))
    last = _png(tmp_path / "main_pushin.png", (1248, 832))
    asyncio.run(th.veo_generate("talk", start_image=start, last_image=last,
                                aspect_ratio="9:16", duration_sec=8))
    assert sent["start_image"].name == "main.9x16.png"
    assert sent["last_image"].name == "main_pushin.9x16.png"
    assert sent["prompt"] == "talk" and sent["duration_sec"] == 8


def test_card_submissions_send_the_card_in_shape(tmp_path, gen, monkeypatch):
    sent = {}

    async def build(prompt, **kwargs):
        sent.update(kwargs, prompt=prompt)
        return {"operation_name": "op"}

    monkeypatch.setattr(card_animation, "build_card_i2v_coro", build)
    card = _png(tmp_path / "intro-810.png", (1280, 800))
    asyncio.run(th._card_i2v_coro("animate", start_image=card, aspect="9:16",
                                  duration=4, model="veo"))
    assert sent["start_image"].name == "intro-810.9x16.png"
    assert sent["prompt"] == "animate" and sent["aspect"] == "9:16"
    assert "title card" in gen["calls"][0]["prompt"]


# ---------------------------------------------------------------------------
# Exact shape, and one conformed start for both ends of the clip
# ---------------------------------------------------------------------------

def _exact(path_or_size, aspect="9:16") -> bool:
    w, h = path_or_size if isinstance(path_or_size, tuple) else Image.open(path_or_size).size
    return w * 16 == h * 9 if aspect == "9:16" else w * 9 == h * 16


@pytest.fixture
def vertex(monkeypatch):
    """vertex_client.veo_generate faked: records what Veo would get."""
    sent: dict = {}

    async def vertex_veo(prompt, **kwargs):
        sent.update(kwargs, prompt=prompt)
        return {"operation_name": "op"}

    monkeypatch.setattr(th, "_vertex_veo_generate", vertex_veo)
    return sent


@pytest.fixture
def push_in_on(monkeypatch):
    for k in ("VEO_LAST_FRAME_ANCHOR", "VEO_CAMERA_PUSH_IN", "VEO_CAMERA_PUSH_IN_ZOOM"):
        monkeypatch.delenv(k, raising=False)


def test_gemini_9x16_still_is_trimmed_not_sent_with_bars(tmp_path, gen):
    # Run 827: 768x1344 is within ±6%, so it used to go to Veo as it was and
    # come back with 10 px bars. Now it is trimmed for free, no model call.
    src = _png(tmp_path / "main.png", (768, 1344))
    out = asyncio.run(th._veo_ready(src, "9:16"))
    assert out == tmp_path / "main.9x16.png"
    assert Image.open(out).size == (756, 1344) and _exact(out)
    assert gen["calls"] == []
    assert Image.open(src).size == (768, 1344)


def test_gemini_16x9_still_is_trimmed_too(tmp_path, gen):
    src = _png(tmp_path / "main.png", (1344, 768))
    out = asyncio.run(th._veo_ready(src, "16:9"))
    assert Image.open(out).size == (1344, 756) and _exact(out, "16:9")
    assert gen["calls"] == []


def test_scene_inputs_hand_the_push_in_to_the_wrapper(tmp_path, push_in_on):
    kf = _png(tmp_path / "main.png", (720, 1280))
    inputs = th._scene_video_inputs(kf, None, None, run_id=7, scene_index=1)
    assert inputs["start_image"] == kf and inputs["last_image"] == kf
    assert inputs["push_in"] == {"zoom": 1.12}
    assert not (tmp_path / "main_pushin.png").exists(), "the end frame is made after conforming"


def test_a_far_off_still_is_reframed_once_and_both_ends_share_it(tmp_path, gen, vertex, push_in_on):
    gen["size"] = (768, 1344)
    kf = _png(tmp_path / "main.png", (1248, 832))
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16", duration_sec=8,
                                **th._scene_video_inputs(kf, None, None, run_id=7)))
    assert len(gen["calls"]) == 1, "one reframe for the start, none for the end"
    start, last = vertex["start_image"], vertex["last_image"]
    assert start.name == "main.9x16.png" and last.name == "main.9x16_pushin.png"
    assert Image.open(start).size == Image.open(last).size == (756, 1344)
    assert _exact(start) and _exact(last)
    assert "push_in" not in vertex, "the spec is never sent to Veo"
    assert vertex["seed"] == (7 * 10007) & 0x7FFFFFFF


def test_the_end_frame_is_a_zoom_of_the_conformed_start(tmp_path, gen, vertex, push_in_on):
    # A still with a marked centre: the end frame is the conformed start
    # zoomed about its centre, not an independent picture.
    im = Image.new("RGB", (768, 1344), (20, 20, 20))
    im.paste((250, 10, 10), (334, 622, 434, 722))
    kf = tmp_path / "main.png"
    im.save(kf)
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16",
                                **th._scene_video_inputs(kf, None, None)))
    start, last = Image.open(vertex["start_image"]), Image.open(vertex["last_image"])
    assert start.size == last.size == (756, 1344)
    assert gen["calls"] == []
    cx, cy = 378, 672
    assert start.getpixel((cx, cy))[0] > 200 and last.getpixel((cx, cy))[0] > 200
    # The marked square is bigger on the end frame (push-in).
    def red_width(img):
        return sum(1 for x in range(img.width) if img.getpixel((x, cy))[0] > 200)
    assert red_width(last) > red_width(start)


def test_a_still_already_exact_keeps_todays_files(tmp_path, gen, vertex, push_in_on):
    kf = _png(tmp_path / "main.png", (720, 1280))
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16",
                                **th._scene_video_inputs(kf, None, None)))
    assert vertex["start_image"] == kf
    assert vertex["last_image"] == tmp_path / "main_pushin.png"
    assert Image.open(vertex["last_image"]).size == (720, 1280)
    assert gen["calls"] == []


def test_without_the_push_in_the_end_frame_is_the_conformed_start(tmp_path, gen, vertex, monkeypatch):
    monkeypatch.setenv("VEO_CAMERA_PUSH_IN", "0")
    monkeypatch.delenv("VEO_LAST_FRAME_ANCHOR", raising=False)
    kf = _png(tmp_path / "main.png", (768, 1344))
    inputs = th._scene_video_inputs(kf, None, None)
    assert "push_in" not in inputs
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16", **inputs))
    assert vertex["start_image"] == vertex["last_image"] == tmp_path / "main.9x16.png"


def test_without_the_anchor_there_is_no_end_frame(tmp_path, gen, vertex, monkeypatch):
    monkeypatch.setenv("VEO_LAST_FRAME_ANCHOR", "0")
    kf = _png(tmp_path / "main.png", (720, 1280))
    inputs = th._scene_video_inputs(kf, None, None)
    assert "last_image" not in inputs and "push_in" not in inputs
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16", **inputs))
    assert "last_image" not in vertex and "push_in" not in vertex


def test_a_dropped_end_frame_drops_the_push_in(tmp_path, gen, vertex):
    # The likeness retry drops the end frame; a push_in left behind must not
    # bring one back, nor reach Veo.
    perturbed = _png(tmp_path / "perturbed.png", (720, 1280))
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16", start_image=perturbed,
                                push_in={"zoom": 1.12}))
    assert vertex["start_image"] == perturbed
    assert "last_image" not in vertex and "push_in" not in vertex


def test_a_failed_reframe_still_sends_matching_ends(tmp_path, gen, vertex, push_in_on):
    gen["raise"] = RuntimeError("quota")
    kf = _png(tmp_path / "main.png", (1248, 832))
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16",
                                **th._scene_video_inputs(kf, None, None)))
    assert len(gen["calls"]) == 1
    assert vertex["start_image"] == kf
    assert Image.open(vertex["last_image"]).size == (1248, 832)


def test_a_kept_card_is_padded_never_redrawn(tmp_path, gen, monkeypatch):
    sent = {}

    async def build(prompt, **kwargs):
        sent.update(kwargs, prompt=prompt)
        return {"operation_name": "op"}

    monkeypatch.setattr(card_animation, "build_card_i2v_coro", build)
    card = Image.new("RGB", (1080, 1350), (240, 240, 240))
    card.paste((10, 10, 200), (100, 100, 980, 1250))
    path = tmp_path / "intro-900.png"
    card.save(path)
    asyncio.run(th._card_i2v_coro("animate", keep_as_is=True, start_image=path,
                                  aspect="9:16", duration=4, model="veo"))
    assert gen["calls"] == [], "no model ever redraws a kept card"
    out = sent["start_image"]
    assert out.name == "intro-900.9x16.pad.png" and _exact(out)
    assert "keep_as_is" not in sent
    padded = Image.open(out).convert("RGB")
    left, top = (padded.width - 1080) // 2, (padded.height - 1350) // 2
    assert padded.crop((left, top, left + 1080, top + 1350)).tobytes() == card.tobytes()


def test_a_near_shape_kept_card_is_padded_too_never_trimmed(tmp_path, gen, monkeypatch):
    # Within the free trim's reach (768x1344 is 1.6% wider than 9:16) a kept
    # card still reaches Veo whole, as _make_bookend_card keeps it: a URL or
    # a logo at its edge is never cut off.
    sent = {}

    async def build(prompt, **kwargs):
        sent.update(kwargs, prompt=prompt)
        return {"operation_name": "op"}

    monkeypatch.setattr(card_animation, "build_card_i2v_coro", build)
    card = Image.new("RGB", (768, 1344), (240, 240, 240))
    card.paste((10, 10, 200), (0, 600, 768, 700))  # edge to edge, like a URL bar
    path = tmp_path / "outro-901.png"
    card.save(path)
    asyncio.run(th._card_i2v_coro("animate", keep_as_is=True, start_image=path,
                                  aspect="9:16", duration=4, model="veo"))
    assert gen["calls"] == [], "no model ever redraws a kept card"
    out = sent["start_image"]
    assert out.name == "outro-901.9x16.pad.png" and _exact(out)
    padded = Image.open(out).convert("RGB")
    left, top = (padded.width - 768) // 2, (padded.height - 1344) // 2
    assert padded.crop((left, top, left + 768, top + 1344)).tobytes() == card.tobytes()
    # A still (not kept) of that size is still trimmed for free, as before.
    still = _png(tmp_path / "main.png", (768, 1344))
    assert Image.open(asyncio.run(th._veo_ready(still, "9:16"))).size == (756, 1344)


def test_the_bookend_wrapper_has_one_caller():
    # test_card_animation counts the bookend submits through the wrapper:
    # every bookend clip is planned by _bookend_job, its one caller.
    import inspect
    src = inspect.getsource(th)
    calls = (src.count("_card_i2v_coro(") - src.count("def _card_i2v_coro(")
             - src.count("build_card_i2v_coro("))
    assert calls == 1
    assert "_card_i2v_coro(" in inspect.getsource(th._bookend_job)
