"""The speaker's mask, from Gemini's segmentation answer — so the Omni
pattern can sit BEHIND the speaker. Parsing, caching, the retry, and never
raising."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import person_matte as pm
from app.services import vertex_client


def _png_b64(size, value=255) -> str:
    buf = io.BytesIO()
    Image.new("L", size, value).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _answer(*entries, fenced=True, data_url=True) -> str:
    items = [{"box_2d": box, "label": "person",
              "mask": ("data:image/png;base64," if data_url else "") + _png_b64((16, 16))}
             for box in entries]
    body = json.dumps(items)
    return f"```json\n{body}\n```" if fenced else body


def test_a_mask_lands_in_its_box():
    # box_2d is [y0, x0, y1, x1] on a 0-1000 grid.
    m = pm.parse_masks(_answer([250, 500, 1000, 1000]), 200, 400)
    assert m.shape == (400, 200) and m.dtype == np.float32
    assert m[100:, 100:].min() == 1.0, "inside the box"
    assert m[:100, :].max() == 0.0 and m[:, :100].max() == 0.0, "outside it"


def test_several_people_are_one_union_and_plain_json_parses():
    m = pm.parse_masks(_answer([0, 0, 500, 500], [500, 500, 1000, 1000],
                               fenced=False, data_url=False), 100, 100)
    assert m[:50, :50].min() == 1.0 and m[50:, 50:].min() == 1.0
    assert m[:50, 50:].max() == 0.0


def test_garbage_and_empty_answers_are_none():
    assert pm.parse_masks("I cannot segment this.", 100, 100) is None
    assert pm.parse_masks("```json\n[]\n```", 100, 100) is None
    assert pm.parse_masks(json.dumps([{"box_2d": [0, 0, 1, 1], "mask": "not base64!"}]),
                          100, 100) is None


@pytest.fixture(autouse=True)
def no_local_matte(monkeypatch):
    """These tests are about the Gemini path; the local matte has its own."""
    monkeypatch.setattr(pm, "_local_mask", lambda frame: None)


def _frame(tmp_path) -> Path:
    f = tmp_path / "chunk_first.png"
    Image.new("RGB", (200, 400), (9, 9, 9)).save(f)
    return f


def test_the_mask_is_cached_next_to_the_frame(tmp_path, monkeypatch):
    calls = []

    async def gemini(prompt, **kw):
        calls.append(kw["model"])
        return _answer([0, 0, 1000, 500])

    monkeypatch.setattr(vertex_client, "gemini_multimodal", gemini)
    f = _frame(tmp_path)
    a = asyncio.run(pm.person_mask(f))
    b = asyncio.run(pm.person_mask(f))
    assert calls == [pm.MATTE_MODEL], "the second read comes from the cache"
    assert (tmp_path / "chunk_first.person.png").exists()
    assert np.allclose(a, b, atol=1 / 255)


def test_one_retry_when_the_first_answer_has_no_mask(tmp_path, monkeypatch):
    answers = iter(["no mask here", _answer([0, 0, 1000, 1000])])

    async def gemini(prompt, **kw):
        return next(answers)

    monkeypatch.setattr(vertex_client, "gemini_multimodal", gemini)
    m = asyncio.run(pm.person_mask(_frame(tmp_path)))
    assert m is not None and m.min() == 1.0


def test_it_never_raises(tmp_path, monkeypatch):
    async def gemini(prompt, **kw):
        raise RuntimeError("vertex down")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", gemini)
    assert asyncio.run(pm.person_mask(_frame(tmp_path))) is None
    assert asyncio.run(pm.person_mask(tmp_path / "missing.png")) is None


def test_the_local_matte_comes_first_and_gemini_is_not_called(tmp_path, monkeypatch):
    local = np.zeros((400, 200), np.float32)
    local[100:, 50:150] = 1.0
    monkeypatch.setattr(pm, "_local_mask", lambda frame: local)

    async def gemini(prompt, **kw):
        raise AssertionError("Gemini must not be called when the local matte works")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", gemini)
    m = asyncio.run(pm.person_mask(_frame(tmp_path)))
    assert m is not None and m[200, 100] == 1.0 and m[50, 100] == 0.0
