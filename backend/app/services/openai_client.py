"""The language-model wrapper the older agents call, with one provider switch.

Functions (the names every caller imports; unchanged):
  await embed_batch(texts)               -> list[list[float]]
  await chat(prompt, system=...)         -> str
  await vision_describe(image_path, ...) -> str
  await transcribe(audio_path)           -> dict
  await openai_image(prompt, ...)        -> bytes
  await openai_image_edit(prompt, ...)   -> bytes

THE SWITCH. `LLM_PROVIDER` in the .env decides who answers: `vertex` (the
default) routes every one of these to Gemini through `vertex_client`, and
`openai` keeps the original OpenAI calls. The switch lives HERE, in the one
module all twenty callers already import, so no caller had to change and the
whole app moves at once. It was flipped on 2026-09-30, the day the OpenAI
account ran out of credits mid-poster ("Stage 1 (OpenAI) failed: ... no
credits remaining"), and because everything newer in the app already ran on
Vertex anyway.

WHAT MAPS TO WHAT. A "mini" model becomes the fast Gemini, anything else the
big one (`vertex_model_for`). JSON answers are asked for as JSON and handed
back as a JSON STRING, because that is what these callers parse. Embeddings
are asked for at OpenAI's width so the existing vector columns still fit
(`OPENAI_EMBED_DIM`); rows embedded before the switch live in a different
space and want re-embedding before search across both means anything.
Transcripts come back in Whisper's `verbose_json` shape (`shape_transcript`).
Pictures are drawn by the Gemini image model at the aspect the requested size
implied (`aspect_for_size`).

The OpenAI half is exactly what it was: one AsyncOpenAI client per process,
cost approximated per call, tenacity retries. The Vertex half carries no
retry of its own here because `vertex_client` already retries and fails over
between regions.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from ..config import config
from .vertex_client import gemini_embed, gemini_image, gemini_multimodal, gemini_text

log = logging.getLogger("archivist.openai")


# Per-1M-token approximate rates (USD, May 2026). NOT authoritative billing.
_RATES = {
    "gpt-4o-mini":            {"in": 0.15,  "out": 0.60},
    "gpt-4o":                 {"in": 2.50,  "out": 10.00},
    "gpt-4-turbo":            {"in": 10.00, "out": 30.00},
    "text-embedding-3-small": {"in": 0.02,  "out": 0.0},
    "text-embedding-3-large": {"in": 0.13,  "out": 0.0},
}


class BudgetExceededError(RuntimeError):
    """NOTHING RAISES THIS ANY MORE. See `CostMeter` and the Vertex twin in
    `vertex_client.VertexBudgetExceededError`. Kept for the call sites that
    catch it and for the day a real cap comes back."""


@dataclass
class CostMeter:
    """Adds up estimated OpenAI spend. It COUNTS, it does not police.

    The same three faults as the Vertex meter, so it was turned off with it:
    one shared pot across unrelated features, a total that resets on every
    restart, and no relationship to OpenAI's own limits, which live in OpenAI's
    dashboard and actually hold. Leaving one of the pair armed would just mean
    meeting the same wall from the other side a day later.

    `enforced = True` puts it back, and every caller still catches the error.
    """
    spent_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)
    budget_usd: float = config.openai_budget_usd
    enforced: bool = False

    def add(self, model: str, usd: float) -> None:
        self.spent_usd += usd
        self.by_model[model] = self.by_model.get(model, 0.0) + usd
        if self.enforced and self.spent_usd >= self.budget_usd:
            raise BudgetExceededError(
                f"OPENAI_BUDGET_USD=${self.budget_usd:.2f} reached "
                f"(spent ${self.spent_usd:.4f}); aborting"
            )
        if self.enforced and self.spent_usd >= 0.8 * self.budget_usd:
            log.warning("OpenAI spend at $%.4f / $%.2f budget", self.spent_usd, self.budget_usd)

    def over_budget(self, fraction: float = 1.0) -> bool:
        """Is spend past `fraction` of the cap? Always False while unenforced."""
        if not self.enforced or self.budget_usd <= 0:
            return False
        return self.spent_usd >= self.budget_usd * fraction

    def estimate_text(self, model: str, in_tokens: int, out_tokens: int) -> float:
        rates = _RATES.get(model)
        if not rates:
            return 0.0
        return (in_tokens * rates["in"] + out_tokens * rates["out"]) / 1_000_000


_client: AsyncOpenAI | None = None
_meter: CostMeter | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _client = AsyncOpenAI(api_key=config.openai_api_key)
    return _client


def get_meter() -> CostMeter:
    global _meter
    if _meter is None:
        _meter = CostMeter()
    return _meter


def _retry():
    return retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

@_retry()
async def _embed_openai(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    """Embed up to ~100 texts in one request."""
    if not texts:
        return []
    model = model or config.openai_model_embed
    client = get_client()
    resp = await client.embeddings.create(model=model, input=texts)
    in_tokens = resp.usage.total_tokens if resp.usage else 0
    get_meter().add(model, get_meter().estimate_text(model, in_tokens, 0))
    return [d.embedding for d in resp.data]


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@_retry()
async def _chat_openai(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 500,
    temperature: float = 0.3,
    response_format: dict | None = None,
) -> str:
    model = model or config.openai_model_chat
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format:
        kwargs["response_format"] = response_format
    resp = await get_client().chat.completions.create(**kwargs)
    if resp.usage:
        cost = get_meter().estimate_text(model, resp.usage.prompt_tokens, resp.usage.completion_tokens)
        get_meter().add(model, cost)
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Vision (image → text)
# ---------------------------------------------------------------------------

# OpenAI vision accepts these MIME types only. SVG and AVIF are rejected.
VISION_SUPPORTED_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class UnsupportedImageError(ValueError):
    """Raised when an image's MIME type isn't accepted by OpenAI vision."""


def _image_to_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        mime = "image/jpeg"
    if mime not in VISION_SUPPORTED_MIMES:
        raise UnsupportedImageError(
            f"OpenAI vision doesn't accept {mime!r} ({path.name})"
        )
    b = path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(b).decode('ascii')}"


def is_vision_supported(path: Path) -> bool:
    """Cheap check before calling vision_describe — saves a wasted API call."""
    mime, _ = mimetypes.guess_type(str(path))
    return (mime or "image/jpeg") in VISION_SUPPORTED_MIMES


@_retry()
async def _vision_openai(
    image_path: Path,
    *,
    prompt: str = (
        "Describe this image in 2 sentences focusing on subject, composition, "
        "mood, color palette, and what it communicates about a brand."
    ),
    model: str | None = None,
    max_tokens: int = 200,
    detail: str = "low",
) -> str:
    model = model or config.openai_model_vision
    data_url = _image_to_data_url(image_path)
    resp = await get_client().chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url, "detail": detail}},
            ],
        }],
        max_completion_tokens=max_tokens,
    )
    if resp.usage:
        cost = get_meter().estimate_text(model, resp.usage.prompt_tokens, resp.usage.completion_tokens)
        get_meter().add(model, cost)
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Image generation — gpt-image-1 (primary) with dall-e-3 fallback
# ---------------------------------------------------------------------------

# Approximate per-image cost (USD, 2025-2026 pricing).
_IMAGE_PRICE_USD = {
    ("gpt-image-1", "low"):    0.011,
    ("gpt-image-1", "medium"): 0.042,
    ("gpt-image-1", "high"):   0.167,
    ("dall-e-3",    "standard"): 0.04,
    ("dall-e-3",    "hd"):       0.08,
}


@_retry()
async def _image_openai(
    prompt: str,
    *,
    size: str = "1024x1024",
    quality: str = "medium",        # gpt-image-1: low|medium|high; dall-e-3: standard|hd
    model: str = "gpt-image-1",
    background: str = "auto",       # gpt-image-1 only: auto|transparent|opaque
) -> bytes:
    """Generate an image via OpenAI. Returns the raw image bytes.

    Tries gpt-image-1 first (better text rendering, less generic AI look,
    accepts longer prompts). Falls back to dall-e-3 if the org doesn't have
    access to gpt-image-1.
    """
    client = get_client()

    async def _call_gpt_image_1() -> bytes:
        resp = await client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size=size,
            quality=quality if quality in {"low", "medium", "high"} else "medium",
            n=1,
            background=background,
        )
        # gpt-image-1 always returns b64_json
        b64 = resp.data[0].b64_json
        return base64.b64decode(b64)

    async def _call_dall_e_3() -> bytes:
        d3_quality = "hd" if quality in {"hd", "high"} else "standard"
        # NB: dall-e-3 used to require response_format="b64_json"; the
        # parameter was removed by OpenAI — b64_json is now the default
        # for this endpoint. Passing it raises 400 "Unknown parameter".
        resp = await client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size=size if size in {"1024x1024", "1024x1792", "1792x1024"} else "1024x1024",
            quality=d3_quality,
            n=1,
        )
        b64 = resp.data[0].b64_json
        return base64.b64decode(b64)

    try:
        if model == "dall-e-3":
            data = await _call_dall_e_3()
            charged_key = ("dall-e-3", "hd" if quality in {"hd", "high"} else "standard")
        else:
            try:
                data = await _call_gpt_image_1()
                charged_key = ("gpt-image-1", quality if quality in {"low", "medium", "high"} else "medium")
            except Exception as e:
                # gpt-image-1 access denied or model missing: fall back
                log.warning("gpt-image-1 failed (%s); falling back to dall-e-3", e)
                data = await _call_dall_e_3()
                charged_key = ("dall-e-3", "hd" if quality in {"hd", "high"} else "standard")
    except Exception:
        log.exception("openai image generation failed")
        raise

    cost = _IMAGE_PRICE_USD.get(charged_key, 0.05)
    get_meter().add(charged_key[0], cost)
    log.info(
        "openai_image: model=%s quality=%s size=%s cost=$%.4f bytes=%d",
        charged_key[0], charged_key[1], size, cost, len(data),
    )
    return data


# ---------------------------------------------------------------------------
# Image EDIT — multi-image input for template + reference fusion
# ---------------------------------------------------------------------------

@_retry()
async def _image_edit_openai(
    prompt: str,
    *,
    image_paths: list[Path],        # first = canvas to edit, rest = references
    size: str = "1024x1024",
    quality: str = "high",
    model: str = "gpt-image-1",
) -> bytes:
    """Edit/compose an image via OpenAI's images.edit endpoint.

    Used by Yas Producer's template_fill mode: the first path is the
    template poster (the canvas to edit), the second path is the student
    photo (the face to drop into the template). gpt-image-1's multi-image
    edit reliably preserves text rendering but face fidelity depends on
    the prompt — be explicit about which image is which.

    Returns the image bytes (base64-decoded from b64_json).
    """
    if not image_paths:
        raise ValueError("openai_image_edit requires at least one image path")

    client = get_client()
    # Always transcode to PNG bytes so OpenAI receives a known-good MIME.
    # When we pass open file handles or path objects, httpx's MIME inference
    # defaults to 'application/octet-stream' for non-standard extensions
    # (e.g. our refs_cache files), and OpenAI rejects with 400.
    from PIL import Image
    from io import BytesIO

    image_tuples: list[tuple[str, bytes, str]] = []
    for i, p in enumerate(image_paths):
        with Image.open(p) as im:
            buf = BytesIO()
            im.convert("RGB").save(buf, format="PNG")
            data = buf.getvalue()
        # Stable, valid filename for each image (image_0.png, image_1.png, ...)
        image_tuples.append((f"image_{i}.png", data, "image/png"))

    image_arg: Any = (
        image_tuples if len(image_tuples) > 1 else image_tuples[0]
    )
    resp = await client.images.edit(
        model=model,
        image=image_arg,
        prompt=prompt,
        size=size,
        quality=quality if quality in {"low", "medium", "high"} else "high",
        n=1,
    )

    b64 = resp.data[0].b64_json
    data = base64.b64decode(b64)
    # Charge at the same per-quality rate as openai_image
    charged_key = (model, quality if quality in {"low", "medium", "high"} else "high")
    cost = _IMAGE_PRICE_USD.get(charged_key, 0.10)
    get_meter().add(charged_key[0], cost)
    log.info(
        "openai_image_edit: model=%s quality=%s images=%d cost=$%.4f bytes=%d",
        model, charged_key[1], len(image_paths), cost, len(data),
    )
    return data


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------

@_retry()
async def _transcribe_openai(audio_path: Path, *, model: str | None = None) -> dict:
    """Returns OpenAI's verbose_json dict with segment timestamps."""
    model = model or config.openai_model_whisper
    client = get_client()
    with audio_path.open("rb") as f:
        resp = await client.audio.transcriptions.create(
            model=model,
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    duration = float(getattr(resp, "duration", 0.0)) or 0.0
    cost = (duration / 60.0) * 0.006  # whisper-1: $0.006/min
    get_meter().add(model, cost)
    return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)


# ---------------------------------------------------------------------------
# The provider switch
# ---------------------------------------------------------------------------

def uses_vertex() -> bool:
    """Does `LLM_PROVIDER` send these calls to Gemini? Anything but `openai` does."""
    return str(getattr(config, "llm_provider", "vertex") or "vertex").strip().lower() != "openai"


def vertex_model_for(openai_model: str | None) -> str:
    """The Gemini that does the job an OpenAI model name was asked for.

    A "mini" was chosen for speed and price, so it becomes the fast Gemini;
    everything else (gpt-4o, the synthesis model) was chosen for quality and
    becomes the big one. Both names come from the .env, like everything else.
    """
    name = (openai_model or "").lower()
    if "mini" in name or "nano" in name or "3.5" in name:
        return config.vertex_gemini_fast_model
    return config.vertex_gemini_model


# Room for the big model's hidden thinking. Gemini 2.5 spends thinking tokens
# out of the SAME budget as the answer, and a starved call returns a cut reply
# rather than an error. The fast model has its thinking switched off instead,
# because these are short, plain jobs and the wait was the complaint.
THINKING_HEADROOM = 4096


def vertex_budget(model_name: str, max_tokens: int) -> tuple[int, int | None]:
    """(max_output_tokens, thinking_budget) for a call an OpenAI caller sized."""
    wanted = max(int(max_tokens or 0), 256)
    if "flash" in (model_name or "").lower():
        return wanted, 0
    return wanted + THINKING_HEADROOM, None


# The keys Gemini's response schema understands. OpenAI's structured-output
# schemas carry `additionalProperties`, `strict` and `$schema`, and Vertex
# rejects the request outright over any one of them.
_SCHEMA_KEYS = frozenset({
    "type", "properties", "required", "items", "enum", "description",
    "nullable", "format", "minimum", "maximum", "minItems", "maxItems",
    "minLength", "maxLength", "anyOf", "propertyOrdering",
})


def gemini_schema(schema: Any) -> Any:
    """An OpenAI response schema with only the keys Gemini accepts. Pure."""
    if isinstance(schema, list):
        return [gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: gemini_schema(sub) for name, sub in value.items()}
        elif key in ("items", "anyOf"):
            out[key] = gemini_schema(value)
        else:
            out[key] = value
    return out


def _schema_from_response_format(fmt: Any) -> dict | None:
    """The schema inside an OpenAI `response_format`, cleaned, or None."""
    if not isinstance(fmt, dict) or fmt.get("type") != "json_schema":
        return None
    inner = fmt.get("json_schema")
    schema = inner.get("schema") if isinstance(inner, dict) else None
    return gemini_schema(schema) if isinstance(schema, dict) else None


def _wants_json(fmt: Any) -> bool:
    return isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema")


_JSON_ONLY = "Reply with one JSON object and nothing else: no prose, no code fences."


def unfence(text: str) -> str:
    """The reply without the ```json fence a model sometimes wraps it in."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
        if t.endswith("```"):
            t = t[:-3].strip()
    return t


def json_text(text: str) -> str:
    """The best JSON a reply holds, as text: unfenced, else the outermost braces,
    else the reply as it came so the caller's own parser reports it."""
    t = unfence(text)
    try:
        json.loads(t)
        return t
    except ValueError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if 0 <= a < b:
        candidate = t[a:b + 1]
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            pass
    return t


async def _chat_vertex(prompt: str, *, system: str | None, model: str | None,
                       max_tokens: int, temperature: float,
                       response_format: dict | None) -> str:
    # The same default the OpenAI side has (a mini), so a caller that named
    # no model gets the fast Gemini and not a 20-second wait on the big one.
    model_name = vertex_model_for(model or config.openai_model_chat)
    budget, thinking = vertex_budget(model_name, max_tokens)
    schema = _schema_from_response_format(response_format)
    sys_text = system or ""
    if _wants_json(response_format) and not schema:
        # OpenAI's json_object mode has no schema to enforce; Gemini is told
        # in words instead and the reply is unfenced below.
        sys_text = f"{sys_text}\n\n{_JSON_ONLY}" if sys_text else _JSON_ONLY
    result = await gemini_text(
        prompt, system=sys_text or None, model=model_name, json_schema=schema,
        temperature=temperature, max_tokens=budget, thinking_budget=thinking,
    )
    if schema is not None:
        # `gemini_text` parses a schema'd reply; the callers here parse for
        # themselves, so it goes back as text.
        if isinstance(result, dict) and result.get("_raw") is not None:
            return json_text(str(result["_raw"]))
        return json.dumps(result)
    text = str(result or "")
    return json_text(text) if _wants_json(response_format) else text.strip()


async def _vision_vertex(image_path: Path, *, prompt: str, model: str | None,
                         max_tokens: int) -> str:
    model_name = vertex_model_for(model or config.openai_model_vision)
    budget, thinking = vertex_budget(model_name, max_tokens)
    out = await gemini_multimodal(
        prompt, images=[Path(image_path)], model=model_name, temperature=0.3,
        max_tokens=budget, thinking_budget=thinking,
    )
    return str(out or "").strip()


# OpenAI's `text-embedding-3-small` is 1536 wide and so are the columns it
# filled (brand_documents, text_chunks, assets, video_transcripts). Gemini's
# embedding model can be asked for any width up to 3072, so it is asked for
# this one and the columns keep working.
OPENAI_EMBED_DIM = 1536


async def _embed_vertex(texts: list[str]) -> list[list[float]]:
    return await gemini_embed(texts, dim=OPENAI_EMBED_DIM)


_TRANSCRIBE_PROMPT = (
    "Transcribe every word spoken in this audio, in the language it is spoken "
    "in. Split it into short segments of one sentence or phrase each, with the "
    "start and end time of each in seconds from the beginning of the audio. If "
    "nothing is spoken, return an empty list of segments."
)

_TRANSCRIBE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "seconds from the start"},
                    "end": {"type": "number", "description": "seconds from the start"},
                    "text": {"type": "string"},
                },
                "required": ["start", "end", "text"],
            },
        },
    },
    "required": ["segments"],
}


def shape_transcript(out: Any) -> dict:
    """Gemini's segments in Whisper's `verbose_json` shape. Pure.

    The two callers read `text`, and `segments[].id/start/end/text`, so those
    are what is promised. Segments with nothing in them are dropped, an end
    before its start is pulled up to it, and `duration` is the last end.
    """
    segments: list[dict] = []
    raw = out.get("segments") if isinstance(out, dict) else None
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        text = " ".join(str(s.get("text") or "").split())
        if not text:
            continue
        try:
            start = float(s.get("start") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(s.get("end") or start)
        except (TypeError, ValueError):
            end = start
        segments.append({"id": len(segments), "start": start,
                         "end": max(end, start), "text": text})
    return {
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "duration": segments[-1]["end"] if segments else 0.0,
        "language": "",
    }


async def _transcribe_vertex(audio_path: Path) -> dict:
    out = await gemini_multimodal(
        _TRANSCRIBE_PROMPT, audio=[Path(audio_path)],
        model=config.vertex_gemini_fast_model, json_schema=_TRANSCRIBE_SCHEMA,
        temperature=0.1, max_tokens=8000, thinking_budget=0,
    )
    return shape_transcript(out)


def aspect_for_size(size: str) -> str:
    """The aspect a gpt-image size was standing in for.

    The callers asked for 1024x1536 because gpt-image-1 has no 9:16 and that
    was the closest, and 1536x1024 for the same reason the other way. Gemini
    has the real ratios, so they get what they meant.
    """
    try:
        w, h = (int(x) for x in str(size or "").lower().split("x"))
    except ValueError:
        return "1:1"
    if w == h:
        return "1:1"
    return "16:9" if w > h else "9:16"


async def _image_vertex(prompt: str, *, size: str) -> bytes:
    return await gemini_image(prompt, aspect_ratio=aspect_for_size(size))


async def _image_edit_vertex(prompt: str, *, image_paths: list[Path], size: str) -> bytes:
    return await gemini_image(
        prompt, aspect_ratio=aspect_for_size(size),
        source_images=[Path(p) for p in image_paths],
    )


# ---------------------------------------------------------------------------
# The public names, dispatching on the switch
# ---------------------------------------------------------------------------

async def embed_batch(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    """Embed up to ~100 texts in one request."""
    if not texts:
        return []
    if uses_vertex():
        return await _embed_vertex(list(texts))
    return await _embed_openai(texts, model=model)


async def chat(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 500,
    temperature: float = 0.3,
    response_format: dict | None = None,
) -> str:
    if uses_vertex():
        return await _chat_vertex(
            prompt, system=system, model=model, max_tokens=max_tokens,
            temperature=temperature, response_format=response_format,
        )
    return await _chat_openai(
        prompt, system=system, model=model, max_tokens=max_tokens,
        temperature=temperature, response_format=response_format,
    )


async def vision_describe(
    image_path: Path,
    *,
    prompt: str = (
        "Describe this image in 2 sentences focusing on subject, composition, "
        "mood, color palette, and what it communicates about a brand."
    ),
    model: str | None = None,
    max_tokens: int = 200,
    detail: str = "low",
) -> str:
    if uses_vertex():
        return await _vision_vertex(image_path, prompt=prompt, model=model,
                                    max_tokens=max_tokens)
    return await _vision_openai(image_path, prompt=prompt, model=model,
                                max_tokens=max_tokens, detail=detail)


async def openai_image(
    prompt: str,
    *,
    size: str = "1024x1024",
    quality: str = "medium",
    model: str = "gpt-image-1",
    background: str = "auto",
) -> bytes:
    """One picture from a prompt. Named for where it used to go; see the switch."""
    if uses_vertex():
        return await _image_vertex(prompt, size=size)
    return await _image_openai(prompt, size=size, quality=quality, model=model,
                               background=background)


async def openai_image_edit(
    prompt: str,
    *,
    image_paths: list[Path],
    size: str = "1024x1024",
    quality: str = "high",
    model: str = "gpt-image-1",
) -> bytes:
    """A picture made from other pictures plus a prompt. Same switch."""
    if not image_paths:
        raise ValueError("openai_image_edit requires at least one image path")
    if uses_vertex():
        return await _image_edit_vertex(prompt, image_paths=image_paths, size=size)
    return await _image_edit_openai(prompt, image_paths=image_paths, size=size,
                                    quality=quality, model=model)


async def transcribe(audio_path: Path, *, model: str | None = None) -> dict:
    """A transcript in Whisper's `verbose_json` shape, whoever wrote it."""
    if uses_vertex():
        return await _transcribe_vertex(audio_path)
    return await _transcribe_openai(audio_path, model=model)
