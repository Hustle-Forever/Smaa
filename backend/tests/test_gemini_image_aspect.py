"""gemini_image must force the requested shape at the API level.

TH v2 asked for 9:16 stills and got 1248x832 ones: the Gemini path never sent
aspect_ratio, so Nano Banana followed its landscape reference photos, and Veo
then letterboxed the landscape keyframe into a 9:16 frame. A caller that asks
for no shape (the template fill) must still get none forced on it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services import vertex_client as vc


def _fake(monkeypatch) -> list:
    seen: list = []

    async def generate_content(*, model, contents, config):
        seen.append(config)
        return SimpleNamespace()

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content)))
    monkeypatch.setattr(vc, "get_client", lambda location: client)
    monkeypatch.setattr(vc, "resolve_image_target",
                        lambda model, location: ("gemini-2.5-flash-image", "us-central1"))
    monkeypatch.setattr(vc, "_extract_image_bytes", lambda resp, model_name: b"png")
    monkeypatch.setattr(vc, "_charge_gemini_usage", lambda model_name, resp: None)
    return seen


def test_a_requested_aspect_is_sent_as_image_config(monkeypatch):
    seen = _fake(monkeypatch)
    assert asyncio.run(vc.gemini_image("a person", aspect_ratio="9:16")) == b"png"
    assert seen[0].image_config is not None
    assert seen[0].image_config.aspect_ratio == "9:16"


def test_no_aspect_means_no_forced_shape(monkeypatch):
    seen = _fake(monkeypatch)
    asyncio.run(vc.gemini_image("fill the template"))
    assert seen[0].image_config is None


def test_system_instruction_still_goes_with_the_aspect(monkeypatch):
    seen = _fake(monkeypatch)
    asyncio.run(vc.gemini_image("x", aspect_ratio="16:9", system_instruction="keep the face"))
    assert seen[0].image_config.aspect_ratio == "16:9"
    assert seen[0].system_instruction == "keep the face"
