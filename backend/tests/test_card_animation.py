"""Tests for the shared ``card_animation`` glue.

``card_animation`` is the single home for the "render a branded still card →
animate it with Veo" recipe that the standalone Create-Animation tool
(``app/routers/yas_producer.py``) and the Talking-Head v2 intro/outro bookends
(``app/services/yas_talking_head_v2.py``) BOTH use. These tests:

  * lock the render directives byte-for-byte (so the refactor stays a no-op vs
    the old hand-inlined strings on both sides),
  * verify the Veo i2v coroutine builder forwards exactly the right args and is
    NOT awaited internally (the Talking-Head reel must keep batching it under its
    concurrency semaphore), and
  * assert the two pipelines actually REUSE this module (guarding against anyone
    re-inlining the glue later).

Runs standalone (``cd backend && python tests/test_card_animation.py``) and under
pytest.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

from app.config import Config, config  # noqa: E402
from app.services import card_animation as ca  # noqa: E402
from app.services.poster_prompts import LOGO_INSTRUCTION  # noqa: E402
from app.services.yas_poster_prompts import ANIMATE_NEGATIVE_PROMPT  # noqa: E402


# --- constants ---------------------------------------------------------------

def test_gemini_constants_track_the_configured_pro_pair():
    # The premium image tier is CONFIG-DRIVEN, not hardcoded: access to
    # `gemini-3-pro-image-preview` is granted per GCP project, and on a project
    # without it every render died with `404 NOT_FOUND: Publisher model … was
    # not found`. What must hold is that these constants follow the configured
    # PRO pair — pinning one model name here is what made this un-deployable.
    assert ca.GEMINI_PRO_MODEL == config.vertex_image_pro_model
    assert ca.GEMINI_PRO_LOCATION == config.vertex_image_pro_location


def test_pro_pair_defaults_to_nano_banana_pro(monkeypatch):
    # With nothing overriding it, the default is still the premium pair — the
    # env vars are an escape hatch for projects that lack access, not a
    # downgrade of the intended quality tier.
    monkeypatch.delenv("VERTEX_IMAGE_PRO_MODEL", raising=False)
    monkeypatch.delenv("VERTEX_IMAGE_PRO_LOCATION", raising=False)
    fresh = Config.from_env()
    assert fresh.vertex_image_pro_model == "gemini-3-pro-image"
    assert fresh.vertex_image_pro_location == "global"


def test_pro_pair_is_overridable_as_a_pair(monkeypatch):
    # Both axes must be settable together: preview image models are published
    # to a single endpoint, so moving the model without the location just
    # relocates the 404.
    monkeypatch.setenv("VERTEX_IMAGE_PRO_MODEL", "gemini-2.5-flash-image")
    monkeypatch.setenv("VERTEX_IMAGE_PRO_LOCATION", "us-central1")
    fresh = Config.from_env()
    assert fresh.vertex_image_pro_model == "gemini-2.5-flash-image"
    assert fresh.vertex_image_pro_location == "us-central1"


def test_negative_prompt_is_canonical():
    # The card i2v negative MUST be the exact canonical poster-prompts string —
    # it is RAI-classifier-sensitive; a divergent copy would risk Veo blocks.
    assert ca.CARD_I2V_NEGATIVE_PROMPT == ANIMATE_NEGATIVE_PROMPT
    assert ca.CARD_I2V_NEGATIVE_PROMPT  # non-empty


def test_logo_path_points_at_branding_png():
    assert ca.LOGO_PATH.name == "42_logo_white.png"
    assert ca.LOGO_PATH.parent.name == "branding"


# --- aspect_render_note: byte-for-byte parity with the OLD inlined strings ----

def test_aspect_render_note_animation_strings():
    # These are the EXACT strings yas_producer.animation_generate_frame used to
    # build inline (no edge-to-edge suffix).
    assert ca.aspect_render_note("9:16") == "RENDER A 9:16 VERTICAL (tall, portrait) IMAGE."
    assert ca.aspect_render_note("16:9") == "RENDER A 16:9 HORIZONTAL (wide, landscape) IMAGE."


def test_aspect_render_note_talking_head_strings():
    # These are the EXACT strings _render_card_animation_style used to build
    # inline (WITH the edge-to-edge suffix).
    assert (
        ca.aspect_render_note("9:16", edge_to_edge=True)
        == "RENDER A 9:16 VERTICAL (tall, portrait) IMAGE, filled edge-to-edge."
    )
    assert (
        ca.aspect_render_note("16:9", edge_to_edge=True)
        == "RENDER A 16:9 HORIZONTAL (wide, landscape) IMAGE, filled edge-to-edge."
    )


def test_aspect_render_note_unknown_defaults_vertical():
    # Both call sites validate/normalise to 9:16/16:9 first; an unexpected value
    # falls back to vertical (matches the prior `else` branch on both sides).
    assert ca.aspect_render_note("1:1").startswith("RENDER A 9:16 VERTICAL")


# --- build_logo_parts --------------------------------------------------------

def test_build_logo_parts_present():
    # The 42 logo PNG is committed to the repo, so it should be present here.
    parts, attached = ca.build_logo_parts()
    assert attached is True
    assert parts == [("text", LOGO_INSTRUCTION), ("image", ca.LOGO_PATH)]


def test_build_logo_parts_missing(monkeypatch=None):
    # When the PNG is absent, return no parts + attached=False so the caller can
    # degrade (drop the logo, or self-draw clause) — caller policy, not ours.
    orig = ca.LOGO_PATH
    try:
        ca.LOGO_PATH = Path("/nonexistent/42_logo_white.png")
        parts, attached = ca.build_logo_parts()
        assert attached is False
        assert parts == []
    finally:
        ca.LOGO_PATH = orig


# --- build_card_i2v_coro: forwards exact args, does NOT await internally ------

def test_build_card_i2v_coro_forwards_args():
    captured = {}

    def _fake_veo(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return "SENTINEL_OP"  # sync return → no un-awaited coroutine

    orig = ca.veo_generate
    try:
        ca.veo_generate = _fake_veo
        out = ca.build_card_i2v_coro(
            "the motion prompt",
            start_image=Path("/tmp/card.png"),
            aspect="16:9",
            duration=4,
            model="veo-3.1-fast-generate-001",
            with_audio=True,
            negative_prompt="NEG",
        )
    finally:
        ca.veo_generate = orig

    # Returned the helper's call result verbatim (proves it did NOT await/wrap).
    assert out == "SENTINEL_OP"
    assert captured["prompt"] == "the motion prompt"
    assert captured["model"] == "veo-3.1-fast-generate-001"
    assert captured["duration_sec"] == 4
    assert captured["aspect_ratio"] == "16:9"
    assert captured["with_audio"] is True
    assert captured["start_image"] == Path("/tmp/card.png")
    assert captured["reference_images"] is None  # a card has no character refs
    assert captured["negative_prompt"] == "NEG"


def test_build_card_i2v_coro_defaults():
    captured = {}

    def _fake_veo(prompt, **kwargs):
        captured.update(kwargs)
        return None

    orig = ca.veo_generate
    try:
        ca.veo_generate = _fake_veo
        ca.build_card_i2v_coro(
            "p", start_image=Path("/tmp/c.png"), aspect="9:16",
            duration=4, model="m",
        )
    finally:
        ca.veo_generate = orig

    # Bookend defaults: silent + the canonical card negative.
    assert captured["with_audio"] is False
    assert captured["negative_prompt"] == ca.CARD_I2V_NEGATIVE_PROMPT
    assert captured["reference_images"] is None


# --- render_card_image: forwards to gemini on the right model/endpoint --------

def test_render_card_image_forwards():
    captured = {}

    async def _fake_gemini(parts, **kwargs):
        captured["parts"] = parts
        captured.update(kwargs)
        return b"IMG_BYTES"

    orig = ca.gemini_image_multipart
    try:
        ca.gemini_image_multipart = _fake_gemini
        out = asyncio.run(
            ca.render_card_image(
                [("text", "hello")], system_instruction="SYS", timeout=5,
            )
        )
    finally:
        ca.gemini_image_multipart = orig

    assert out == b"IMG_BYTES"
    assert captured["parts"] == [("text", "hello")]
    assert captured["system_instruction"] == "SYS"
    assert captured["model"] == ca.GEMINI_PRO_MODEL
    assert captured["location"] == ca.GEMINI_PRO_LOCATION


# --- reuse guards: the two pipelines actually USE the shared module -----------

def _read(rel: str) -> str:
    return (_REPO_BACKEND / rel).read_text(encoding="utf-8")


def test_talking_head_reuses_shared_glue():
    src = _read("app/services/yas_talking_head_v2.py")
    # Every intro/outro bookend Veo submit (the reel's start, single and
    # multi-scene, a clip retry, a transient resubmit) goes through the shared
    # builder: _bookend_job makes it, via _card_i2v_coro, which puts the card
    # in the clip's shape first and is the builder's only caller.
    wrapper_calls = (src.count("_card_i2v_coro(") - src.count("def _card_i2v_coro(")
                     - src.count("build_card_i2v_coro("))
    assert wrapper_calls == 1
    assert src.count("_bookend_job(") - src.count("def _bookend_job(") == 4
    assert src.count("card_animation.build_card_i2v_coro(") == 1
    # The card frame render goes through the shared render + logo helpers.
    assert "card_animation.render_card_image(" in src
    assert "card_animation.build_logo_parts()" in src
    assert "card_animation.aspect_render_note(" in src
    # And the OLD duplicated constants/strings are gone (no re-inlining).
    assert "POSTER_LOGO_PATH = (" not in src
    assert 'GEMINI_PRO_MODEL = "gemini-3-pro-image-preview"' not in src


def test_animation_tool_reuses_shared_glue():
    src = _read("app/routers/yas_producer.py")
    # The standalone animation frame + Veo submit both go through the shared glue.
    assert "card_animation.render_card_image(" in src
    assert "card_animation.build_card_i2v_coro(" in src
    assert "card_animation.aspect_render_note(" in src
    assert "card_animation.build_logo_parts()" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} card_animation tests passed.")


def test_the_logo_ships_with_the_code_not_the_data_volume():
    # Production mounts a volume over backend/data, which hid the old copy there:
    # posters and cards went out without the real mark and the model drew its
    # own. The PNG lives under app/ (baked into the image) and must exist.
    from app.services import poster_pipeline as pp
    assert ca.LOGO_PATH == pp.POSTER_LOGO_PATH
    assert ca.LOGO_PATH.exists(), ca.LOGO_PATH
    assert "data" not in ca.LOGO_PATH.relative_to(Path(ca.__file__).resolve().parents[2]).parts
    assert ca.LOGO_PATH.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
