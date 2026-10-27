"""Tests for the "model returned no image" recovery path.

Image *edit* calls (Create Poster / Animation "Remove logo" and "Refine") feed a
finished still back to Gemini and ask it to re-render with one change. The model
sometimes replies with TEXT instead of an image — and because the low-level
``gemini_image_multipart`` retries the IDENTICAL request 6×, that used to burn
~60s and then surface a misleading 502 ("check the model ID").

This locks in the fix, which has three pieces, all exercised here WITHOUT network:

  1. ``vertex_client._extract_image_bytes`` raises a typed ``GeminiNoImageError``
     that distinguishes a permanent safety/policy block (``blocked=True``) from a
     transient text-only reply (``blocked=False``), carrying the diagnostics.
  2. ``vertex_client._image_should_retry`` fails fast on a safety block (no point
     retrying) but keeps retrying everything else.
  3. ``card_animation._render_edit_escalating`` re-issues a text-only failure ONCE
     with an "image only" directive appended, and re-raises a safety block.

Runs standalone (``cd backend && python tests/test_no_image_recovery.py``) and pytest.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))


# --- tiny fakes mimicking the google-genai response shape --------------------

def _part(*, text=None, image=None):
    p = types.SimpleNamespace(text=text, inline_data=None)
    if image is not None:
        p.inline_data = types.SimpleNamespace(data=image)
    return p


def _resp(*, parts=None, finish_reason=None, block_reason=None):
    cand = types.SimpleNamespace(
        finish_reason=(types.SimpleNamespace(name=finish_reason) if finish_reason else None),
        content=types.SimpleNamespace(parts=parts or []),
    )
    fb = (
        types.SimpleNamespace(block_reason=types.SimpleNamespace(name=block_reason))
        if block_reason
        else None
    )
    return types.SimpleNamespace(candidates=[cand], prompt_feedback=fb)


# --- _extract_image_bytes ----------------------------------------------------

def test_extract_returns_image_bytes_when_present():
    from app.services import vertex_client as vc

    resp = _resp(parts=[_part(text="here you go"), _part(image=b"PNGDATA")])
    assert vc._extract_image_bytes(resp, "model-x") == b"PNGDATA"


def test_extract_text_only_reply_is_non_blocked():
    from app.services import vertex_client as vc

    # A purely descriptive reply (NOT a refusal) — the model chatted instead of
    # rendering, which is recoverable by escalation, so it must stay non-blocked.
    resp = _resp(
        parts=[_part(text="Here is a description of the edited poster layout.")],
        finish_reason="STOP",
    )
    try:
        vc._extract_image_bytes(resp, "model-x")
        assert False, "expected GeminiNoImageError"
    except vc.GeminiNoImageError as e:
        assert e.blocked is False
        assert e.finish_reason == "STOP"
        assert e.text and "description" in e.text


def test_extract_soft_refusal_text_is_blocked():
    from app.services import vertex_client as vc

    # The real-world failure: an IMAGE model declining the task in words, with a
    # normal finish_reason and NO safety block. Must be treated as permanent
    # (blocked) so it fails fast instead of retrying/escalating into a 429.
    resp = _resp(
        parts=[_part(text="I'm just a language model and can't help with that.")],
        finish_reason="STOP",
    )
    try:
        vc._extract_image_bytes(resp, "model-x")
        assert False, "expected GeminiNoImageError"
    except vc.GeminiNoImageError as e:
        assert e.blocked is True, "a soft refusal must be treated as permanent"
        assert e.finish_reason == "STOP"


def test_looks_like_refusal_discriminates():
    from app.services import vertex_client as vc

    assert vc._looks_like_refusal("I'm just a language model and can't help with that.")
    assert vc._looks_like_refusal("Sorry, I cannot generate that image.")
    # A plain description of the edit is NOT a refusal — it should still escalate.
    assert not vc._looks_like_refusal("Here is the poster with the badge removed.")
    assert not vc._looks_like_refusal(None)


def test_extract_safety_finish_reason_is_blocked():
    from app.services import vertex_client as vc

    resp = _resp(parts=[], finish_reason="IMAGE_SAFETY")
    try:
        vc._extract_image_bytes(resp, "model-x")
        assert False, "expected GeminiNoImageError"
    except vc.GeminiNoImageError as e:
        assert e.blocked is True
        assert e.finish_reason == "IMAGE_SAFETY"


def test_extract_prompt_feedback_block_is_blocked():
    from app.services import vertex_client as vc

    resp = _resp(parts=[], block_reason="PROHIBITED_CONTENT")
    try:
        vc._extract_image_bytes(resp, "model-x")
        assert False, "expected GeminiNoImageError"
    except vc.GeminiNoImageError as e:
        assert e.blocked is True
        assert e.block_reason == "PROHIBITED_CONTENT"


# --- retry predicate ---------------------------------------------------------

def test_retry_predicate_skips_safety_block_retries_others():
    from app.services import vertex_client as vc

    blocked = vc.GeminiNoImageError("x", blocked=True)
    textonly = vc.GeminiNoImageError("x", blocked=False)
    assert vc._image_should_retry(blocked) is False        # permanent → fail fast
    assert vc._image_should_retry(textonly) is True         # transient → retry
    assert vc._image_should_retry(RuntimeError("429")) is True  # other errors still retry


# --- escalation in card_animation -------------------------------------------

def test_escalation_appends_image_only_directive_on_text_reply(monkeypatch):
    from app.services import card_animation as ca
    from app.services import poster_prompts as pp
    from app.services.vertex_client import GeminiNoImageError

    calls: list[list] = []

    async def fake_render(parts, *, system_instruction, timeout, aspect_ratio=None):
        calls.append(parts)
        if len(calls) == 1:
            raise GeminiNoImageError("text reply", blocked=False, finish_reason="STOP")
        return b"RECOVERED"

    monkeypatch.setattr(ca, "render_card_image", fake_render)

    base = [("text", "ref"), ("image", Path("/tmp/x.png")), ("text", "RENDER it")]
    out = asyncio.run(
        ca._render_edit_escalating(base, system_instruction="sys", timeout=1.0, what="delogo")
    )
    assert out == b"RECOVERED"
    assert len(calls) == 2, "should re-issue exactly once after a text-only reply"
    # First attempt is byte-identical to the proven prompt.
    assert calls[0][-1] == ("text", "RENDER it")
    # Escalated attempt appends the image-only enforcer to the FINAL directive.
    assert calls[1][-1][1].endswith(pp.IMAGE_ONLY_RETRY_ENFORCER)
    assert calls[1][-1][1].startswith("RENDER it")


def test_escalation_reraises_safety_block_without_retry(monkeypatch):
    from app.services import card_animation as ca
    from app.services.vertex_client import GeminiNoImageError

    calls: list[list] = []

    async def fake_render(parts, *, system_instruction, timeout, aspect_ratio=None):
        calls.append(parts)
        raise GeminiNoImageError("blocked", blocked=True, finish_reason="IMAGE_SAFETY")

    monkeypatch.setattr(ca, "render_card_image", fake_render)

    base = [("text", "ref"), ("image", Path("/tmp/x.png")), ("text", "RENDER it")]
    try:
        asyncio.run(
            ca._render_edit_escalating(base, system_instruction="sys", timeout=1.0, what="delogo")
        )
        assert False, "expected the safety block to propagate"
    except GeminiNoImageError as e:
        assert e.blocked is True
    assert len(calls) == 1, "a safety block must NOT trigger the escalated retry"


# --- HTTP mapping ------------------------------------------------------------

def test_http_mapper_blocked_is_422_textonly_is_502():
    from app.routers.yas_producer import _http_for_no_image
    from app.services.vertex_client import GeminiNoImageError

    blocked = _http_for_no_image(GeminiNoImageError("x", blocked=True), action="remove the logo")
    assert blocked.status_code == 422 and "remove the logo" in blocked.detail

    textonly = _http_for_no_image(GeminiNoImageError("x", blocked=False), action="apply that change")
    assert textonly.status_code == 502 and "apply that change" in textonly.detail


# --- minimal monkeypatch shim so the file runs standalone (no pytest) --------

class _MP:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


if __name__ == "__main__":
    import inspect

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        if "monkeypatch" in inspect.signature(fn).parameters:
            mp = _MP()
            try:
                fn(mp)
            finally:
                mp.undo()
        else:
            fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nALL {len(fns)} no-image-recovery tests passed.")
