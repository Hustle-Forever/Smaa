"""Tests for the provider switch in ``app/services/openai_client.py``.

Every public name the twenty callers import (`chat`, `vision_describe`,
`embed_batch`, `transcribe`, `openai_image`, `openai_image_edit`) now
dispatches on `LLM_PROVIDER`. These tests pin the Vertex side of that: what
each call turns into, and what comes back in the shape the callers already
parse. No network: the four Gemini functions are replaced with recorders.

The OpenAI side is the code that was always there, renamed with an
underscore; one test checks the switch actually reaches it.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import openai_client as oc  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Recorder:
    """A stand-in for one gemini_* function: remembers the call, returns `reply`."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[dict] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return self.reply


@pytest.fixture
def vertex(monkeypatch):
    monkeypatch.setattr(oc, "uses_vertex", lambda: True)
    return monkeypatch


# ---------------------------------------------------------------------------
# The mapping, pure
# ---------------------------------------------------------------------------

def test_a_mini_becomes_the_fast_gemini_and_the_rest_the_big_one():
    assert oc.vertex_model_for("gpt-4o-mini") == oc.config.vertex_gemini_fast_model
    assert oc.vertex_model_for("gpt-4o") == oc.config.vertex_gemini_model
    assert oc.vertex_model_for(None) == oc.config.vertex_gemini_model


def test_the_fast_model_thinks_not_at_all_and_the_big_one_gets_headroom():
    assert oc.vertex_budget("gemini-2.5-flash", 300) == (300, 0)
    tokens, thinking = oc.vertex_budget("gemini-2.5-pro", 300)
    assert tokens == 300 + oc.THINKING_HEADROOM and thinking is None
    assert oc.vertex_budget("gemini-2.5-flash", 0)[0] == 256, "never a zero budget"


def test_an_openai_schema_loses_the_keys_vertex_rejects():
    schema = {
        "type": "object", "additionalProperties": False, "strict": True,
        "properties": {
            "lines": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                 "properties": {"t": {"type": "string"}},
                                                 "required": ["t"]}},
            "mood": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["lines"],
    }
    got = oc.gemini_schema(schema)
    assert "additionalProperties" not in json.dumps(got) and "strict" not in json.dumps(got)
    assert got["properties"]["lines"]["items"]["properties"] == {"t": {"type": "string"}}
    assert got["properties"]["mood"]["enum"] == ["a", "b"]
    assert got["required"] == ["lines"]


@pytest.mark.parametrize("raw,expected", [
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ('Sure! Here it is:\n{"a": 1}\nHope that helps.', '{"a": 1}'),
    ('{"a": 1}', '{"a": 1}'),
    ("not json at all", "not json at all"),
])
def test_the_json_a_reply_holds_is_handed_back_as_text(raw, expected):
    assert oc.json_text(raw) == expected


@pytest.mark.parametrize("size,aspect", [
    ("1024x1024", "1:1"), ("1024x1536", "9:16"), ("1536x1024", "16:9"),
    ("1024x1792", "9:16"), ("nonsense", "1:1"),
])
def test_a_gpt_image_size_becomes_the_ratio_it_stood_for(size, aspect):
    assert oc.aspect_for_size(size) == aspect


def test_a_transcript_comes_back_in_whispers_shape():
    got = oc.shape_transcript({"segments": [
        {"start": 0.0, "end": 2.5, "text": "  Hello   there "},
        {"start": 2.5, "end": 1.0, "text": "backwards end"},   # pulled up to its start
        {"start": 4, "end": 5, "text": "   "},                  # nothing said: dropped
        "junk",
    ]})
    assert got["text"] == "Hello there backwards end"
    assert got["segments"] == [
        {"id": 0, "start": 0.0, "end": 2.5, "text": "Hello there"},
        {"id": 1, "start": 2.5, "end": 2.5, "text": "backwards end"},
    ]
    assert got["duration"] == 2.5
    assert oc.shape_transcript(None) == {"text": "", "segments": [], "duration": 0.0, "language": ""}


# ---------------------------------------------------------------------------
# The dispatch, with the Gemini functions recorded
# ---------------------------------------------------------------------------

def test_chat_goes_to_gemini_with_the_mapped_model(vertex):
    rec = _Recorder("  a plain answer  ")
    vertex.setattr(oc, "gemini_text", rec)
    got = _run(oc.chat("hello", system="be brief", model="gpt-4o-mini", max_tokens=120,
                       temperature=0.7))
    assert got == "a plain answer"
    call = rec.calls[0]
    assert call["args"] == ("hello",)
    assert call["system"] == "be brief"
    assert call["model"] == oc.config.vertex_gemini_fast_model
    assert call["temperature"] == 0.7
    assert call["json_schema"] is None
    assert call["thinking_budget"] == 0, "the fast model answers without thinking"


def test_json_object_mode_asks_in_words_and_unfences_the_reply(vertex):
    rec = _Recorder('```json\n{"ok": true}\n```')
    vertex.setattr(oc, "gemini_text", rec)
    got = _run(oc.chat("give me json", system="sys", response_format={"type": "json_object"}))
    assert json.loads(got) == {"ok": True}
    assert "JSON" in rec.calls[0]["system"] and rec.calls[0]["system"].startswith("sys")
    assert rec.calls[0]["json_schema"] is None


def test_json_schema_mode_hands_gemini_the_cleaned_schema_and_returns_text(vertex):
    rec = _Recorder({"lines": [{"t": "hi"}]})   # gemini_text parses schema'd replies
    vertex.setattr(oc, "gemini_text", rec)
    fmt = {"type": "json_schema", "json_schema": {"name": "s", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {"lines": {"type": "array", "items": {"type": "object",
                       "properties": {"t": {"type": "string"}}}}},
        "required": ["lines"]}}}
    got = _run(oc.chat("go", model="gpt-4o", response_format=fmt))
    assert json.loads(got) == {"lines": [{"t": "hi"}]}
    assert rec.calls[0]["json_schema"] == {
        "type": "object",
        "properties": {"lines": {"type": "array", "items": {"type": "object",
                       "properties": {"t": {"type": "string"}}}}},
        "required": ["lines"]}
    assert rec.calls[0]["model"] == oc.config.vertex_gemini_model
    assert rec.calls[0]["thinking_budget"] is None


def test_a_cut_schema_reply_still_comes_back_as_the_best_json_text(vertex):
    rec = _Recorder({"_raw": 'Here: {"lines": []} and more'})
    vertex.setattr(oc, "gemini_text", rec)
    fmt = {"type": "json_schema", "json_schema": {"schema": {"type": "object", "properties": {"lines": {"type": "array"}}}}}
    assert _run(oc.chat("go", response_format=fmt)) == '{"lines": []}'


def test_vision_goes_to_gemini_with_the_picture(vertex, tmp_path):
    rec = _Recorder("a desk")
    vertex.setattr(oc, "gemini_multimodal", rec)
    pic = tmp_path / "a.png"
    pic.write_bytes(b"\x89PNG")
    assert _run(oc.vision_describe(pic, prompt="what is this", model="gpt-4o-mini", max_tokens=90)) == "a desk"
    assert rec.calls[0]["images"] == [pic]
    assert rec.calls[0]["args"] == ("what is this",)
    assert rec.calls[0]["max_tokens"] == 256


def test_embeddings_are_asked_for_at_openais_width(vertex):
    rec = _Recorder([[0.1] * oc.OPENAI_EMBED_DIM])
    vertex.setattr(oc, "gemini_embed", rec)
    got = _run(oc.embed_batch(["hello"]))
    assert len(got[0]) == 1536
    assert rec.calls[0]["dim"] == 1536
    assert _run(oc.embed_batch([])) == [] and len(rec.calls) == 1, "nothing to embed, no call"


def test_transcribe_sends_the_audio_and_shapes_the_answer(vertex, tmp_path):
    rec = _Recorder({"segments": [{"start": 0, "end": 1.5, "text": "hi"}]})
    vertex.setattr(oc, "gemini_multimodal", rec)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    got = _run(oc.transcribe(wav))
    assert got["text"] == "hi" and got["segments"][0]["end"] == 1.5
    assert rec.calls[0]["audio"] == [wav]
    assert rec.calls[0]["json_schema"] is oc._TRANSCRIBE_SCHEMA


def test_pictures_are_drawn_at_the_ratio_the_size_meant(vertex, tmp_path):
    rec = _Recorder(b"png")
    vertex.setattr(oc, "gemini_image", rec)
    assert _run(oc.openai_image("a poster", size="1024x1536", quality="high")) == b"png"
    assert rec.calls[0]["aspect_ratio"] == "9:16"
    canvas, face = tmp_path / "c.png", tmp_path / "f.png"
    canvas.write_bytes(b"1"); face.write_bytes(b"2")
    assert _run(oc.openai_image_edit("drop the face in", image_paths=[canvas, face],
                                     size="1536x1024")) == b"png"
    assert rec.calls[1]["source_images"] == [canvas, face]
    assert rec.calls[1]["aspect_ratio"] == "16:9"


def test_an_edit_with_no_pictures_is_refused_before_any_call(vertex):
    rec = _Recorder(b"png")
    vertex.setattr(oc, "gemini_image", rec)
    with pytest.raises(ValueError):
        _run(oc.openai_image_edit("x", image_paths=[]))
    assert rec.calls == []


# ---------------------------------------------------------------------------
# The other side of the switch
# ---------------------------------------------------------------------------

def test_the_openai_side_is_still_there_and_gemini_is_not_touched(monkeypatch):
    monkeypatch.setattr(oc, "uses_vertex", lambda: False)

    async def openai_chat(prompt, **kw):
        return f"openai said {prompt}"

    async def never(*a, **kw):
        raise AssertionError("Gemini was called with the switch on openai")

    monkeypatch.setattr(oc, "_chat_openai", openai_chat)
    monkeypatch.setattr(oc, "gemini_text", never)
    assert _run(oc.chat("hi")) == "openai said hi"


def test_the_switch_reads_the_config_and_defaults_to_vertex():
    assert oc.uses_vertex() == (oc.config.llm_provider != "openai")
    assert oc.config.llm_provider in ("vertex", "openai")


def test_a_call_that_names_no_model_gets_what_openais_default_would_have_been(vertex):
    """OpenAI's defaults here are the minis, so the unnamed call goes to the
    fast Gemini. Measured before this: a five-word logo description took 23
    seconds on the big model."""
    rec = _Recorder("x")
    vertex.setattr(oc, "gemini_text", rec)
    vertex.setattr(oc, "gemini_multimodal", rec)
    _run(oc.chat("hi"))
    _run(oc.vision_describe(Path("/nonexistent.png")))
    assert rec.calls[0]["model"] == oc.vertex_model_for(oc.config.openai_model_chat)
    assert rec.calls[1]["model"] == oc.vertex_model_for(oc.config.openai_model_vision)
