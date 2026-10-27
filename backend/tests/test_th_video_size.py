"""TH v2 Video size (#63, part 2): every still, card and keyframe is exactly
the reel's shape, and the size is fixed once stills exist.

- _conform_to_aspect trims a near shape (Gemini's 768x1344 or 1344x768) to the
  exact ratio for free, reframes a far one ONCE (mocked), pads when told to,
  and leaves an exact one byte for byte. It works in both directions.
- _run_image_gen and _render_card_animation_style (TH opt-in) conform where
  the still or card is made; the video editor's card call is unchanged.
- The as-is character photo is trimmed, or reframed once.
- edit_run and generate-previews refuse a new size once a still or card
  exists (409, nothing applied); fetch_run says so (aspect_locked) and gives
  every scene its still's width and height.

No model calls: gemini_image, render_card_image and _run_image_gen are fakes.
"""
from __future__ import annotations

import asyncio
import copy
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from app.services import card_animation
from app.services import yas_talking_head_v2 as th


def _png_bytes(size: tuple[int, int], color=(90, 120, 150)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _dims(data: bytes) -> tuple[int, int]:
    return Image.open(BytesIO(data)).size


def _exact(size: tuple[int, int], aspect: str = "9:16") -> bool:
    w, h = size
    return w * 16 == h * 9 if aspect == "9:16" else w * 9 == h * 16


@pytest.fixture
def gen(monkeypatch):
    """_run_image_gen faked: records each call (and whether its source files
    existed at the time), returns a still of `size`."""
    state = {"calls": [], "size": (768, 1344), "raise": None}

    async def run_image_gen(*, prompt, image_model, source_images=None, aspect="9:16", size=None,
                            conform="reframe"):
        state["calls"].append({
            "prompt": prompt, "sources": list(source_images or []), "aspect": aspect,
            "conform": conform, "engine": image_model,
            "existed": [Path(p).exists() for p in (source_images or [])],
        })
        if state["raise"]:
            raise state["raise"]
        return _png_bytes(state["size"]), "image/png"

    monkeypatch.setattr(th, "_run_image_gen", run_image_gen)
    return state


# ---------------------------------------------------------------------------
# _conform_to_aspect
# ---------------------------------------------------------------------------

def test_gemini_9x16_becomes_exactly_9x16(gen):
    out = asyncio.run(th._conform_to_aspect(_png_bytes((768, 1344)), "9:16", "reframe"))
    assert _dims(out) == (756, 1344) and _exact(_dims(out))
    assert gen["calls"] == [], "a near shape is a free trim, no model call"


def test_gemini_16x9_becomes_exactly_16x9(gen):
    out = asyncio.run(th._conform_to_aspect(_png_bytes((1344, 768)), "16:9", "reframe"))
    assert _dims(out) == (1344, 756) and _exact(_dims(out), "16:9")
    assert gen["calls"] == []


@pytest.mark.parametrize("size,aspect", [
    ((720, 1280), "9:16"), ((1080, 1920), "9:16"), ((756, 1344), "9:16"),
    ((1280, 720), "16:9"), ((1920, 1080), "16:9"), ((1344, 756), "16:9"),
])
@pytest.mark.parametrize("policy", ["crop", "pad", "reframe"])
def test_an_exact_size_is_returned_byte_for_byte(gen, size, aspect, policy):
    data = _png_bytes(size)
    assert asyncio.run(th._conform_to_aspect(data, aspect, policy)) == data
    assert gen["calls"] == []


@pytest.mark.parametrize("size", [(1248, 832), (1024, 1024)])
def test_a_far_off_still_is_reframed_once(gen, size):
    out = asyncio.run(th._conform_to_aspect(_png_bytes(size), "9:16", "reframe"))
    assert _dims(out) == (756, 1344)
    call, = gen["calls"]
    assert call["conform"] is None, "the reframe is not itself reframed"
    assert call["engine"] == th.ENGINE_GEMINI_PRO and call["aspect"] == "9:16"
    assert "RESIZE ONLY" in call["prompt"] and "person" in call["prompt"]
    tmp, = call["sources"]
    assert call["existed"] == [True] and not tmp.exists(), "the temporary source is removed"


def test_a_portrait_still_is_reframed_once_for_a_landscape_reel(gen):
    gen["size"] = (1344, 768)
    out = asyncio.run(th._conform_to_aspect(_png_bytes((1024, 1024)), "16:9", "reframe"))
    assert _dims(out) == (1344, 756) and len(gen["calls"]) == 1


def test_a_card_reframe_keeps_every_letter(gen):
    asyncio.run(th._conform_to_aspect(_png_bytes((1024, 1024)), "9:16", "reframe", card=True))
    call, = gen["calls"]
    assert "title card" in call["prompt"] and "every letter" in call["prompt"]


def test_a_reframe_with_its_own_file_sends_that_file(gen, tmp_path):
    src = tmp_path / "photo.png"
    src.write_bytes(_png_bytes((1000, 1000)))
    asyncio.run(th._conform_to_aspect(src.read_bytes(), "9:16", "reframe", source=src))
    assert gen["calls"][0]["sources"] == [src] and src.exists()


def test_a_reframe_that_comes_back_wrong_pads_the_original(gen):
    gen["size"] = (1024, 1024)
    original = Image.new("RGB", (1248, 832), (200, 30, 30))
    buf = BytesIO()
    original.save(buf, "PNG")
    out = asyncio.run(th._conform_to_aspect(buf.getvalue(), "9:16", "reframe"))
    assert len(gen["calls"]) == 1, "never a second reframe"
    padded = Image.open(BytesIO(out)).convert("RGB")
    assert _exact(padded.size)
    left, top = (padded.width - 1248) // 2, (padded.height - 832) // 2
    assert padded.crop((left, top, left + 1248, top + 832)).tobytes() == original.tobytes()


def test_a_failed_reframe_raises(gen):
    gen["raise"] = RuntimeError("quota")
    with pytest.raises(RuntimeError):
        asyncio.run(th._conform_to_aspect(_png_bytes((1024, 1024)), "9:16", "reframe"))


@pytest.mark.parametrize("size,aspect", [((1080, 1350), "9:16"), ((720, 1280), "16:9")])
def test_pad_keeps_every_pixel_and_calls_no_model(gen, size, aspect):
    card = Image.new("RGB", size, (240, 240, 240))
    card.paste((10, 10, 200), (40, 40, size[0] - 40, size[1] - 40))
    buf = BytesIO()
    card.save(buf, "PNG")
    out = Image.open(BytesIO(asyncio.run(th._conform_to_aspect(buf.getvalue(), aspect, "pad")))).convert("RGB")
    assert _exact(out.size, aspect) and gen["calls"] == []
    left, top = (out.width - size[0]) // 2, (out.height - size[1]) // 2
    assert out.crop((left, top, left + size[0], top + size[1])).tobytes() == card.tobytes()


def test_the_crop_policy_trims_even_a_far_off_still(gen):
    out = asyncio.run(th._conform_to_aspect(_png_bytes((1024, 1024)), "9:16", "crop"))
    assert _dims(out) == (576, 1024) and gen["calls"] == []


def test_a_near_shape_is_trimmed_whatever_the_policy(gen):
    out = asyncio.run(th._conform_to_aspect(_png_bytes((768, 1344)), "9:16", "pad"))
    assert _dims(out) == (756, 1344)


def _ruler(size=(1024, 1024)) -> bytes:
    """Red channel = x // 4, so a crop's first column tells where it starts."""
    im = Image.new("RGB", size)
    im.putdata([(x // 4, 0, 0) for _y in range(size[1]) for x in range(size[0])])
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _crop_left(data: bytes) -> int:
    out = Image.open(BytesIO(data)).convert("RGB")
    assert out.size == (576, 1024)
    return out.getpixel((0, 500))[0] * 4


@pytest.mark.parametrize("face_x,left", [
    (100, 0),      # near the left edge: the window stops at the edge
    (950, 448),    # near the right edge: the window stops at the edge
    (600, 312),    # centred on the face
])
def test_the_trim_keeps_the_face_in(gen, monkeypatch, face_x, left):
    monkeypatch.setattr(th, "_face_center", lambda im: (face_x, 400))
    assert _crop_left(asyncio.run(th._conform_to_aspect(_ruler(), "9:16", "crop"))) == left


def test_without_a_face_the_trim_is_centred(gen, monkeypatch):
    monkeypatch.setattr(th, "_face_center", lambda im: None)
    assert _crop_left(asyncio.run(th._conform_to_aspect(_ruler(), "9:16", "crop"))) == 224


def test_face_detection_on_a_plain_image_finds_nothing():
    assert th._face_center(Image.new("RGB", (768, 1344), (120, 120, 120))) is None


def test_an_unknown_policy_is_refused(gen):
    with pytest.raises(ValueError):
        asyncio.run(th._conform_to_aspect(_png_bytes((768, 1344)), "9:16", "stretch"))


def test_unreadable_bytes_pass_through(gen):
    assert asyncio.run(th._conform_to_aspect(b"not an image", "9:16", "reframe")) == b"not an image"


# ---------------------------------------------------------------------------
# Where stills and cards are made
# ---------------------------------------------------------------------------

@pytest.fixture
def gemini(monkeypatch):
    """vertex gemini_image faked: the n-th call returns sizes[n] (the last
    one repeats)."""
    state = {"calls": [], "sizes": [(768, 1344)]}

    async def fake(prompt, *, model=None, aspect_ratio=None, source_images=None,
                   system_instruction=None, location=None):
        state["calls"].append({"prompt": prompt, "aspect": aspect_ratio,
                               "sources": list(source_images or [])})
        n = min(len(state["calls"]), len(state["sizes"])) - 1
        return _png_bytes(state["sizes"][n])

    monkeypatch.setattr(th, "gemini_image", fake)
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    return state


def test_run_image_gen_returns_an_exact_still(gemini):
    data, mime = asyncio.run(th._run_image_gen(prompt="p", image_model=th.ENGINE_GEMINI_PRO,
                                               aspect="9:16"))
    assert _dims(data) == (756, 1344) and mime == "image/png"
    assert len(gemini["calls"]) == 1 and gemini["calls"][0]["aspect"] == "9:16"


def test_run_image_gen_landscape(gemini):
    gemini["sizes"] = [(1344, 768)]
    data, _ = asyncio.run(th._run_image_gen(prompt="p", image_model=th.ENGINE_GEMINI_PRO,
                                            aspect="16:9"))
    assert _dims(data) == (1344, 756)


def test_run_image_gen_reframes_an_ignored_shape_once(gemini):
    gemini["sizes"] = [(1024, 1024), (768, 1344)]
    data, _ = asyncio.run(th._run_image_gen(prompt="a speaker", image_model=th.ENGINE_GEMINI_PRO,
                                            aspect="9:16"))
    assert _dims(data) == (756, 1344)
    first, second = gemini["calls"]
    assert first["prompt"] == "a speaker"
    assert "RESIZE ONLY" in second["prompt"] and len(second["sources"]) == 1
    assert second["aspect"] == "9:16"


def test_run_image_gen_never_reframes_twice(gemini):
    gemini["sizes"] = [(1024, 1024)]  # the model ignores the shape every time
    data, _ = asyncio.run(th._run_image_gen(prompt="p", image_model=th.ENGINE_GEMINI_PRO,
                                            aspect="9:16"))
    assert len(gemini["calls"]) == 2 and _exact(_dims(data))


def test_run_image_gen_without_conform_is_untouched(gemini):
    data, _ = asyncio.run(th._run_image_gen(prompt="p", image_model=th.ENGINE_GEMINI_PRO,
                                            aspect="9:16", conform=None))
    assert _dims(data) == (768, 1344)


@pytest.fixture
def card_render(monkeypatch, tmp_path):
    state = {"calls": [], "size": (768, 1344)}

    async def render(parts, *, system_instruction, timeout, aspect_ratio=None):
        state["calls"].append(aspect_ratio)
        return _png_bytes(state["size"])

    monkeypatch.setattr(card_animation, "render_card_image", render)
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    ref = tmp_path / "ref.png"
    ref.write_bytes(_png_bytes((720, 1280)))
    state["ref"] = ref
    return state


def _card(card_render, **kw):
    return asyncio.run(th._render_card_animation_style(
        role="intro", topic="t", headline="Hello", subtitle="", ref_mode="text_only",
        ref_path=card_render["ref"], aspect="9:16", **kw,
    ))


def test_th_cards_are_made_in_the_exact_shape(card_render):
    data, mime, _prompt = _card(card_render, conform="reframe")
    assert _dims(data) == (756, 1344) and mime == "image/png"
    assert card_render["calls"] == ["9:16"]


def test_the_video_editor_cards_are_untouched(card_render):
    # yas_video_edit_ai_bookends calls the public alias without `conform`.
    assert th.render_card_animation_style is th._render_card_animation_style
    data, _mime, _prompt = _card(card_render)
    assert _dims(data) == (768, 1344)


def test_a_square_th_card_is_reframed_once_as_a_card(card_render, gen):
    card_render["size"] = (1024, 1024)
    data, _mime, _prompt = _card(card_render, conform="reframe")
    assert _dims(data) == (756, 1344)
    call, = gen["calls"]
    assert "title card" in call["prompt"]


def test_th_card_call_sites_opt_in():
    import inspect
    src = inspect.getsource(th)
    # Every TH card is drawn in one place, _make_bookend_card, which
    # generate_previews' intro and outro and the card retry all call.
    assert src.count('aspect=gem_aspect, conform="reframe",') == 1
    assert 'feedback=feedback, aspect=gem_aspect, conform="reframe",' in inspect.getsource(th._make_bookend_card)
    assert "_make_bookend_card(" in inspect.getsource(th.generate_previews)
    assert "_make_bookend_card(" in inspect.getsource(th.retry_preview)


# ---------------------------------------------------------------------------
# The character photo used as-is
# ---------------------------------------------------------------------------

def _photo(tmp_path, size) -> Path:
    p = tmp_path / f"photo-{size[0]}x{size[1]}.png"
    p.write_bytes(_png_bytes(size))
    return p


def test_as_is_photo_near_the_shape_is_trimmed_without_a_model(gen, tmp_path):
    cand = _photo(tmp_path, (768, 1344))
    data, mime, prompt = asyncio.run(th._as_is_or_reframe(1, 0, cand, md={"aspect_ratio": "9:16"}))
    assert _dims(data) == (756, 1344) and prompt == th._USE_AS_IS_PROMPT
    assert gen["calls"] == []


def test_as_is_exact_photo_is_the_photo(gen, tmp_path):
    cand = _photo(tmp_path, (720, 1280))
    data, _mime, prompt = asyncio.run(th._as_is_or_reframe(1, 0, cand, md={"aspect_ratio": "9:16"}))
    assert data == th._png_bytes_from_path(cand) and prompt == th._USE_AS_IS_PROMPT


def test_as_is_far_off_photo_is_reframed_once(gen, tmp_path):
    cand = _photo(tmp_path, (1000, 1000))
    data, _mime, prompt = asyncio.run(th._as_is_or_reframe(1, 0, cand, md={"aspect_ratio": "9:16"}))
    assert _dims(data) == (756, 1344)
    call, = gen["calls"]
    assert call["sources"] == [cand] and call["conform"] is None
    assert prompt.startswith("EDIT THE ATTACHED IMAGE")


# ---------------------------------------------------------------------------
# Video size is fixed once stills exist
# ---------------------------------------------------------------------------

RUN = 5


def _row(md: dict, **cols) -> dict:
    row = {"id": RUN, "topic": "t", "mode": th.MODE_PREVIEW, "status": "ready",
           "image_model": "gemini_pro", "local_path": None, "storage_bucket": None,
           "storage_path": None, "width": None, "height": None, "error": None,
           "saved_asset_id": None, "created_at": None, "updated_at": None, "metadata": md}
    row.update(cols)
    return row


@pytest.fixture
def store(monkeypatch):
    s = {"rows": {}, "patches": []}

    async def fetch_run_row(run_id):
        if run_id not in s["rows"]:
            raise HTTPException(404, "nope")
        return copy.deepcopy(s["rows"][run_id])

    async def patch_metadata(run_id, patch):
        s["patches"].append(copy.deepcopy(patch))
        s["rows"][run_id]["metadata"].update(copy.deepcopy(patch))

    async def fetch_run(run_id, **_):
        return {"id": run_id, "aspect_ratio": s["rows"][run_id]["metadata"].get("aspect_ratio")}

    monkeypatch.setattr(th, "_fetch_run_row", fetch_run_row)
    monkeypatch.setattr(th, "_patch_metadata", patch_metadata)
    monkeypatch.setattr(th, "fetch_run", fetch_run)
    return s


LOCKS = {
    "scene 0 still": ({"aspect_ratio": "9:16"}, {"local_path": "yas_producer/talking-head-v2/5/main.png"}),
    "bucket-only still": ({"aspect_ratio": "9:16"}, {"storage_path": "yas/talking-head-v2/5/main.png"}),
    "scene row": ({"aspect_ratio": "9:16", "scene_count": 2,
                   "scenes": [{"index": 0}, {"index": 1, "keyframe_id": 77}]}, {}),
    "intro card": ({"aspect_ratio": "9:16", "intro_image_id": 81}, {}),
    "outro card": ({"aspect_ratio": "9:16", "outro_image_id": 82}, {}),
}


@pytest.mark.parametrize("why", sorted(LOCKS))
def test_edit_refuses_a_new_size_once_stills_exist(store, why):
    md, cols = LOCKS[why]
    store["rows"][RUN] = _row(copy.deepcopy(md), **cols)
    assert th._aspect_locked(store["rows"][RUN], md)
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.edit_run(run_id=RUN, aspect_ratio="16:9", video_quality="quality"))
    assert e.value.status_code == 409 and e.value.detail == th.ASPECT_LOCKED_MESSAGE
    assert store["patches"] == [], "a refused size change edits nothing"


def test_edit_with_the_same_size_is_fine_once_locked(store):
    store["rows"][RUN] = _row({"aspect_ratio": "16:9", "intro_image_id": 81})
    out = asyncio.run(th.edit_run(run_id=RUN, aspect_ratio="16:9"))
    assert out["aspect_ratio"] == "16:9"


def test_a_legacy_run_without_a_size_is_9x16(store):
    store["rows"][RUN] = _row({}, local_path="yas_producer/talking-head-v2/5/main.png")
    asyncio.run(th.edit_run(run_id=RUN, aspect_ratio="9:16"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.edit_run(run_id=RUN, aspect_ratio="16:9"))
    assert e.value.status_code == 409


def test_edit_saves_a_new_size_before_any_still(store):
    store["rows"][RUN] = _row({"aspect_ratio": "9:16", "intro_excluded": True})
    assert not th._aspect_locked(store["rows"][RUN], store["rows"][RUN]["metadata"])
    out = asyncio.run(th.edit_run(run_id=RUN, aspect_ratio="16:9"))
    assert out["aspect_ratio"] == "16:9"
    assert any(p.get("aspect_ratio") == "16:9" for p in store["patches"])


def test_generate_previews_refuses_a_new_size_once_stills_exist(store, gen):
    store["rows"][RUN] = _row({"aspect_ratio": "9:16", "outro_image_id": 82})
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.generate_previews(RUN, look={"aspect_ratio": "16:9"}))
    assert e.value.status_code == 409
    assert gen["calls"] == [] and store["patches"] == []


def test_the_look_patch_keeps_the_same_size_and_sets_a_new_one_before_stills(store):
    store["rows"][RUN] = _row({"aspect_ratio": "9:16", "intro_image_id": 81})
    asyncio.run(th._apply_look_patch(RUN, {"aspect_ratio": "9:16"}))
    assert store["patches"][-1]["aspect_ratio"] == "9:16"
    store["rows"][RUN] = _row({"aspect_ratio": "9:16"})
    asyncio.run(th._apply_look_patch(RUN, {"aspect_ratio": "16:9"}))
    assert store["patches"][-1]["aspect_ratio"] == "16:9"


def test_the_edit_route_answers_409_with_the_message(store):
    from app.routers import yas_producer as yp

    store["rows"][RUN] = _row({"aspect_ratio": "9:16"}, local_path="x/main.png")
    app = FastAPI()
    app.include_router(yp.router)
    r = TestClient(app).post(f"/api/yas-producer/talking-head-v2/{RUN}/edit",
                             json={"aspect_ratio": "16:9"})
    assert r.status_code == 409 and r.json()["detail"] == th.ASPECT_LOCKED_MESSAGE


# ---------------------------------------------------------------------------
# fetch_run: every scene's still size, and the lock
# ---------------------------------------------------------------------------

@pytest.fixture
def fetch_env(monkeypatch):
    s = {"run": None, "scene_rows": {}}

    async def fetch_run_row(run_id):
        return copy.deepcopy(s["run"])

    async def scene_row(scene_id):
        return copy.deepcopy(s["scene_rows"].get(int(scene_id)))

    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(th, "_fetch_run_row", fetch_run_row)
    monkeypatch.setattr(th, "_fetch_scene_row", scene_row)
    monkeypatch.setattr(th, "_fetch_animation_row", none)
    monkeypatch.setattr(th, "_fetch_card_row", none)
    return s


def test_fetch_run_gives_every_scene_its_still_size(fetch_env):
    md = {"aspect_ratio": "9:16", "scene_count": 3,
          "scenes": [{"index": 0}, {"index": 1, "keyframe_id": 901}, {"index": 2}]}
    fetch_env["run"] = _row(md, local_path="yas_producer/talking-head-v2/5/main.png",
                            width=768, height=1344)
    fetch_env["scene_rows"][901] = {"id": 901, "local_path": "yas_producer/talking-head-v2/5/s1.png",
                                    "width": 756, "height": 1344, "updated_at": None}
    out = asyncio.run(th.fetch_run(RUN, with_reel=False))
    sizes = [(s["width"], s["height"]) for s in out["scenes"]]
    assert sizes == [(768, 1344), (756, 1344), (None, None)]
    assert out["aspect_locked"] is True
    assert (out["main"]["width"], out["main"]["height"]) == (768, 1344)


def test_fetch_run_single_scene_size_and_no_lock_before_stills(fetch_env):
    fetch_env["run"] = _row({"aspect_ratio": "16:9"})
    out = asyncio.run(th.fetch_run(RUN, with_reel=False))
    assert out["aspect_locked"] is False
    assert (out["scenes"][0]["width"], out["scenes"][0]["height"]) == (None, None)
    fetch_env["run"] = _row({"aspect_ratio": "16:9"}, local_path="x/main.png", width=1344, height=756)
    out = asyncio.run(th.fetch_run(RUN, with_reel=False))
    assert out["aspect_locked"] is True
    assert (out["scenes"][0]["width"], out["scenes"][0]["height"]) == (1344, 756)
