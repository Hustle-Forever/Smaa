"""Vertex AI (Gemini + Veo) async wrapper with cost-meter + budget guard.

Auth: Application Default Credentials. Run once on the host:
    gcloud auth application-default login

Functions:
  await gemini_text(prompt, system=..., json_schema=...) -> str | dict
  await gemini_text(prompt, search=True)                 -> str  (Google-grounded)
  await gemini_search(prompt, ...)             -> (str, [{title, uri}])
  await gemini_multimodal(prompt, images=[Path], ...)    -> str | dict
  await gemini_embed(texts, dim=768)                     -> list[list[float]]
  await veo_generate(prompt, ...)                        -> dict (name, model, cost_usd)
  await veo_poll(operation_name)                         -> dict (done, gcs_uri?, error?)
  await download_gcs(gs_uri, dest_path)                  -> Path

The cost meter tracks Gemini + Veo spend per process. A single client is reused
across the process (`get_client()`); ADC credentials are picked up automatically.

Veo is a long-running operation — `veo_generate` returns immediately with an
operation name that must be polled. The background `veo_poller` worker handles
that lifecycle.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from ..config import config

log = logging.getLogger("archivist.vertex")


# -----------------------------------------------------------------------------
# Pricing (USD, May 2026). Approximate — NOT authoritative billing.
# Sources: Vertex AI public pricing page for Gemini & Veo on Vertex.
# -----------------------------------------------------------------------------

# Per-1M-token rates for Gemini text/embedding models.
_GEMINI_RATES = {
    "gemini-2.5-pro":        {"in": 1.25, "out": 10.00},
    "gemini-2.5-flash":      {"in": 0.30, "out": 2.50},
    "gemini-2.5-flash-lite": {"in": 0.10, "out": 0.40},
    "gemini-embedding-001":  {"in": 0.15, "out": 0.0},
}

# Per-second rates for Veo (video). Audio adds a separate per-second charge.
_VEO_RATES = {
    "veo-3.1-fast-generate-001": {"video_per_sec": 0.10, "audio_per_sec": 0.075},
    "veo-3.1-generate-001":      {"video_per_sec": 0.50, "audio_per_sec": 0.25},
    "veo-3-fast-generate-001":   {"video_per_sec": 0.10, "audio_per_sec": 0.075},
    "veo-3-generate-001":        {"video_per_sec": 0.50, "audio_per_sec": 0.25},
}


# -----------------------------------------------------------------------------
# Cost meter
# -----------------------------------------------------------------------------

class VeoQuotaExhaustedError(RuntimeError):
    """Raised when Vertex returns 429 / RESOURCE_EXHAUSTED for Veo even after
    retry-with-backoff. The caller should surface this as a user-facing
    "wait + retry" message rather than a generic 502.
    """


class VertexBudgetExceededError(RuntimeError):
    """NOTHING RAISES THIS ANY MORE. Kept because about forty call sites catch
    it, and because a real cap may come back one day (see `VertexCostMeter`).

    It used to fire when cumulative Vertex spend crossed VEO_BUDGET_USD. The
    handlers that catch it are now unreachable rather than wrong, so they are
    left where they are: each one is the correct behaviour for the day a cap is
    switched back on, and deleting forty of them to chase an unreachable branch
    would be a much bigger change than this one.
    """


class GeminiNoImageError(RuntimeError):
    """Raised when an image model returns a response with no image bytes.

    Two very different causes hide behind "no image", and the caller must tell
    them apart:

      * ``blocked=True`` — the model refused / a safety or recitation filter
        fired (``finish_reason`` like ``SAFETY`` / ``IMAGE_SAFETY`` /
        ``PROHIBITED_CONTENT`` / ``RECITATION``, or a ``prompt_feedback``
        ``block_reason``). This is PERMANENT for the same input — retrying the
        identical request is pointless, so ``_image_retry`` fails fast on it and
        edit callers must NOT re-issue.

      * ``blocked=False`` — the model replied with TEXT instead of an image
        (common on image *edit* prompts: it describes the edit or asks a
        question rather than rendering). This is prompt-sensitive, so an edit
        caller can recover by re-issuing with a blunter "image only" directive.

    Attributes carry the diagnostics (``finish_reason``, ``block_reason``, and
    any ``text`` the model returned) so the failure is actually debuggable
    instead of a blind "no image bytes".
    """

    def __init__(
        self,
        message: str,
        *,
        blocked: bool = False,
        finish_reason: str | None = None,
        block_reason: str | None = None,
        text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.blocked = blocked
        self.finish_reason = finish_reason
        self.block_reason = block_reason
        self.text = text


@dataclass
class VertexCostMeter:
    """Adds up estimated Vertex spend. It COUNTS, it does not police.

    IT USED TO REFUSE WORK, AND IT WAS REMOVED BY THE OWNER, for three separate
    reasons that were all true at once:

      - The name lied. VEO_BUDGET_USD reads like a cap on video, and it was
        really one shared pot for every Google call in the app: Veo, images,
        Gemini text, embeddings and the news search. Generating video starved
        the news feed, which then reported "no budget" on a day nobody had
        touched a video.
      - It was never a real cap. `spent_usd` lives in this process and resets on
        every restart, so three restarts spend three times the "budget". It
        stopped honest work without preventing the runaway it was aimed at.
      - Nobody could act on it. It is not a Google limit. Google's own caps live
        in Cloud Console, where they belong and where they actually hold.

    So `add` never raises now, and `enforced` is False. The number is still
    counted, still broken down by model and still reported by the producer
    status endpoint, because knowing what a run cost is useful even when
    nothing is gated on it.

    TO PUT A CAP BACK, set `enforced = True` here. Every gate in the app already
    asks `over_budget()` and every caller already catches
    `VertexBudgetExceededError`, so that one line is the whole switch. Do it
    with a real, persisted total rather than this one, which forgets everything
    on restart.
    """
    spent_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)
    budget_usd: float = config.veo_budget_usd
    op_charges: dict[str, tuple[str, float]] = field(default_factory=dict)
    enforced: bool = False

    def add(self, model: str, usd: float) -> None:
        if usd <= 0:
            return
        self.spent_usd += usd
        self.by_model[model] = self.by_model.get(model, 0.0) + usd
        if self.enforced and self.spent_usd >= self.budget_usd:
            raise VertexBudgetExceededError(
                f"VEO_BUDGET_USD=${self.budget_usd:.2f} reached "
                f"(spent ${self.spent_usd:.4f}); aborting Vertex calls"
            )
        if self.enforced and self.spent_usd >= 0.8 * self.budget_usd:
            log.warning("Vertex spend at $%.4f / $%.2f budget", self.spent_usd, self.budget_usd)

    def over_budget(self, fraction: float = 1.0) -> bool:
        """Is spend past `fraction` of the cap? Always False while unenforced.

        THE ONE QUESTION EVERY GATE IN THE APP ASKS. They each used to compare
        `spent_usd` against `budget_usd` themselves, at five different
        percentages in five different files, so turning the cap off meant
        finding all five. Now there is one place.
        """
        if not self.enforced or self.budget_usd <= 0:
            return False
        return self.spent_usd >= self.budget_usd * fraction

    def refund(self, model: str, usd: float) -> None:
        """Decrement the meter when a pre-charged Vertex call ends up failing
        before any actual API work happens (e.g. 429 quota, 4xx auth). Never
        raises — only subtracts. Use only when GCP did NOT bill us."""
        if usd <= 0:
            return
        self.spent_usd = max(0.0, self.spent_usd - usd)
        self.by_model[model] = max(0.0, self.by_model.get(model, 0.0) - usd)

    def note_op(self, op_name: str, model: str, usd: float) -> None:
        """Record a submitted Veo op's charge so it can be refunded if the op is
        later BLOCKED/filtered at poll time (GCP does not bill blocked videos —
        otherwise the budget gets eaten by content-filter false positives)."""
        if op_name and usd > 0:
            self.op_charges[op_name] = (model, usd)

    def refund_op(self, op_name: str) -> None:
        """Refund a recorded op's charge (its video was blocked/filtered → not
        billed by GCP). Idempotent — a no-op if the op wasn't recorded."""
        rec = self.op_charges.pop(op_name, None)
        if rec:
            self.refund(rec[0], rec[1])

    def estimate_gemini(self, model: str, in_tokens: int, out_tokens: int) -> float:
        rates = _GEMINI_RATES.get(model)
        if not rates:
            return 0.0
        return (in_tokens * rates["in"] + out_tokens * rates["out"]) / 1_000_000

    def estimate_veo(self, model: str, duration_sec: float, with_audio: bool) -> float:
        rates = _VEO_RATES.get(model)
        if not rates:
            return 0.0
        cost = rates["video_per_sec"] * duration_sec
        if with_audio:
            cost += rates["audio_per_sec"] * duration_sec
        return cost


_meter: VertexCostMeter | None = None


def get_meter() -> VertexCostMeter:
    global _meter
    if _meter is None:
        _meter = VertexCostMeter()
    return _meter


# -----------------------------------------------------------------------------
# Client (lazy — first call instantiates; ADC is read from env / ~/.config)
# -----------------------------------------------------------------------------

_clients: dict[str, Any] = {}


def get_client(location: str | None = None):
    """Return a google-genai Client for the requested location. Clients are
    cached per location because some Gemini 3 image preview models are only
    available on the `global` endpoint while everything else uses the
    project's default location.
    """
    loc = location or config.google_cloud_location
    cached = _clients.get(loc)
    if cached is not None:
        return cached
    if not config.google_cloud_project:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is not set — run "
            "`gcloud auth application-default login` and set it in backend/.env"
        )
    try:
        from google import genai
    except ImportError as e:
        raise RuntimeError(
            "google-genai not installed. Run: pip install -r requirements.txt"
        ) from e
    client = genai.Client(
        vertexai=True,
        project=config.google_cloud_project,
        location=loc,
    )
    _clients[loc] = client
    log.info(
        "Vertex client ready (project=%s, location=%s)",
        config.google_cloud_project, loc,
    )
    return client


def _retry():
    return retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_random_exponential(min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )


# -----------------------------------------------------------------------------
# Model gateway: region failover + global concurrency cap
#
# `RESOURCE_EXHAUSTED` (429) is Vertex saying "too many requests this minute in
# THIS region". Instead of sleeping (which makes the user wait), we instantly
# retry the SAME call in the next region — quota walls become invisible. Only
# when every region is exhausted does the outer @_retry()'s backoff kick in as
# a last resort. The semaphore smooths bursts under the per-minute ceiling.
# -----------------------------------------------------------------------------

_gateway_sem: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _gateway_sem
    if _gateway_sem is None:
        _gateway_sem = asyncio.Semaphore(max(1, config.vertex_max_concurrency))
    return _gateway_sem


def _is_quota_error(exc: BaseException) -> bool:
    msg = str(exc)
    return (
        " 429 " in f" {msg} "
        or "RESOURCE_EXHAUSTED" in msg
        or "Quota exceeded" in msg
    )


def _failover_locations() -> list[str]:
    """Primary location first, then the configured fallbacks (deduped)."""
    locs = [config.google_cloud_location, *config.vertex_fallback_locations]
    seen: set[str] = set()
    ordered: list[str] = []
    for loc in locs:
        if loc and loc not in seen:
            seen.add(loc)
            ordered.append(loc)
    return ordered


async def _generate_with_failover(make_call):
    """Run `make_call(client)` under the concurrency cap, rotating regions on a
    quota 429. `make_call` takes a google-genai client and returns the awaitable
    generate/embed coroutine. Non-quota errors propagate immediately (the outer
    @_retry handles transient ones). Raises the last quota error only after
    every region is exhausted."""
    locations = _failover_locations()
    last_quota: Exception | None = None
    async with _get_sem():
        for i, loc in enumerate(locations):
            try:
                return await make_call(get_client(loc))
            except Exception as exc:  # noqa: BLE001
                if not _is_quota_error(exc):
                    raise
                last_quota = exc
                if i + 1 < len(locations):
                    log.warning(
                        "Vertex quota 429 in %s — failing over to %s",
                        loc, locations[i + 1],
                    )
                    continue
    # All regions exhausted on quota: let the outer @_retry back off + retry.
    raise last_quota if last_quota is not None else RuntimeError("no Vertex locations configured")


# -----------------------------------------------------------------------------
# Image-generation throttle + patient 429 retry
# -----------------------------------------------------------------------------
# The Gemini image models (Nano Banana / gemini-*-image) have a TIGHT
# per-minute quota. A single "generate" fans out N variants (4 by default) with
# asyncio.gather, so all N hit Vertex at the same instant and trip
# 429 RESOURCE_EXHAUSTED. The generic `_retry()` (4 attempts, ≤30s) is too weak:
# the variants retry in lockstep and re-collide before the 1-minute window
# resets. Two defences below — bound concurrency, and a patient quota-aware
# retry — make 429s self-heal instead of surfacing as "stage2_failed".

try:
    # How many image requests may hit Vertex at once (per process). Lower this
    # if you still see 429s; raise it after a quota increase. Default 2.
    _IMAGE_CONCURRENCY = max(1, int(os.getenv("VERTEX_IMAGE_CONCURRENCY", "2")))
except ValueError:
    _IMAGE_CONCURRENCY = 2

_image_sem: asyncio.Semaphore | None = None


def _image_semaphore() -> asyncio.Semaphore:
    """Lazily-created (binds to the running loop on first use) so module import
    never needs an event loop. One shared gate for every image call."""
    global _image_sem
    if _image_sem is None:
        _image_sem = asyncio.Semaphore(_IMAGE_CONCURRENCY)
    return _image_sem


# Long backoff for 429s (outlast the ~1-minute quota window); quick retry for
# other transient blips so a genuinely-broken request still fails fast.
_quota_wait = wait_random_exponential(multiplier=1, min=4, max=60)
_other_wait = wait_random_exponential(multiplier=1, min=1, max=8)


def _image_wait(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if exc is not None and _is_quota_error(exc):
        return _quota_wait(retry_state)
    return _other_wait(retry_state)


def _image_should_retry(exc: BaseException) -> bool:
    """Retry predicate for image generation. Retries any error EXCEPT a permanent
    safety/policy block — a ``GeminiNoImageError(blocked=True)`` won't change on an
    identical retry, so re-issuing it 6× just wastes ~60s and Vertex quota. Every
    other failure (429s, transient blips, a text-only no-image reply) stays
    retryable, same as before."""
    if isinstance(exc, GeminiNoImageError) and exc.blocked:
        return False
    return isinstance(exc, Exception)


def _image_retry():
    """Patient retry for image generation: up to 6 attempts. 429s wait the long
    backoff (so a retry survives the per-minute window); everything else retries
    quickly and then re-raises. A permanent safety block fails fast (see
    ``_image_should_retry``)."""
    return retry(
        retry=retry_if_exception(_image_should_retry),
        wait=_image_wait,
        stop=stop_after_attempt(6),
        reraise=True,
    )


# Vertex finish reasons (and prompt_feedback block reasons) that mean the model
# REFUSED — a safety / policy / recitation block. These are permanent for the same
# input, so callers must not retry or escalate; they surface a clear message.
_BLOCKING_FINISH_REASONS = {
    "SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST", "SPII",
}

# Phrases that mark a SOFT refusal — the model finished normally (finish_reason
# STOP, no safety block) but declined the task in conversational text instead of
# rendering ("I'm just a language model and can't help with that"). This is the
# image model balking at the *task* (e.g. it reads "remove the logo/brand mark" as
# watermark removal), so it is permanent for the same prompt: re-issuing the
# identical request or appending an "image only" directive cannot change its mind.
# Treated like a block so callers fail fast with a clear message instead of
# burning the retry budget (and tripping a 429).
_REFUSAL_MARKERS = (
    "i'm just a language model",
    "i am just a language model",
    "as a language model",
    "can't help with that",
    "cannot help with that",
    "can't assist with that",
    "cannot assist with that",
    "i'm not able to",
    "i am not able to",
    "i'm unable to",
    "i am unable to",
    "i can't create",
    "i cannot create",
    "i can't generate",
    "i cannot generate",
    "i can't edit",
    "i cannot edit",
    "unable to help",
)


def _looks_like_refusal(text: str | None) -> bool:
    """True when a no-image text reply is the model DECLINING the task (see
    ``_REFUSAL_MARKERS``), as opposed to merely describing the edit. A refusal is
    permanent for the same prompt, so it must not be retried or escalated."""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _REFUSAL_MARKERS)


def _extract_image_bytes(resp: Any, model_name: str) -> bytes:
    """Pull the first inline image out of a Gemini image response, or raise a typed
    ``GeminiNoImageError`` that explains the absence (safety block vs text-only
    reply). Shared by ``gemini_image`` and ``gemini_image_multipart`` so both paths
    diagnose "no image" identically instead of raising a blind, misleading
    "check the model ID" string."""
    candidates = getattr(resp, "candidates", None) or []
    finish_reason: str | None = None
    texts: list[str] = []
    for c in candidates:
        fr = getattr(c, "finish_reason", None)
        if fr is not None and finish_reason is None:
            finish_reason = getattr(fr, "name", None) or str(fr)
        content = getattr(c, "content", None)
        if not content:
            continue
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                return inline.data
            txt = getattr(part, "text", None)
            if txt:
                texts.append(str(txt))

    # No image part. Work out whether the model refused (safety/policy) or simply
    # replied with text — the caller treats those very differently.
    fb = getattr(resp, "prompt_feedback", None)
    block_reason = None
    if fb is not None:
        br = getattr(fb, "block_reason", None)
        if br is not None:
            block_reason = getattr(br, "name", None) or str(br)
    text = " ".join(texts).strip() or None
    hard_block = bool(block_reason) or (finish_reason or "").upper() in _BLOCKING_FINISH_REASONS
    soft_refusal = _looks_like_refusal(text)
    # Both are permanent for the same prompt → treat as "blocked" so the retry
    # predicate fails fast and the edit escalation is skipped.
    blocked = hard_block or soft_refusal

    snippet = (text[:200] + "…") if text and len(text) > 200 else text
    if hard_block:
        msg = (
            f"{model_name} refused to return an image "
            f"(finish_reason={finish_reason}, block_reason={block_reason})."
        )
    elif soft_refusal:
        msg = (
            f"{model_name} declined the task — it replied with a refusal instead of "
            f"an image (finish_reason={finish_reason}). Text: {snippet!r}"
        )
    else:
        msg = (
            f"{model_name} returned no image — the model replied with text instead "
            f"(finish_reason={finish_reason}). Text: {snippet!r}"
        )
    log.warning("%s", msg)
    raise GeminiNoImageError(
        msg,
        blocked=blocked,
        finish_reason=finish_reason,
        block_reason=block_reason,
        text=text,
    )


# -----------------------------------------------------------------------------
# Gemini — text & multimodal generation
# -----------------------------------------------------------------------------

# Google Search grounding. Attaching the search tool makes the model LOOK the
# subject up instead of answering from its training memory — the difference
# between "#spidermanbrandnewday is about a brand new day" and "it is the 2026
# Spider-Man film". Two facts about it are load-bearing:
#
#   * It is INCOMPATIBLE with structured output. Vertex rejects (or silently
#     degrades) a request that carries both `tools=[google_search]` and a
#     `response_schema`, so the two are refused loudly here rather than
#     discovered in production. A grounded call returns prose; parse it yourself.
#   * The sources come back on `candidates[].grounding_metadata`, NOT in the
#     text. `gemini_text(search=True)` keeps its existing "returns str" contract
#     and drops them; `gemini_search()` is the one that hands them back.

_SEARCH_WITH_SCHEMA = (
    "gemini_text(search=True) cannot be combined with json_schema — Vertex "
    "rejects or degrades structured output when the Google Search tool is "
    "attached. Make the grounded call return prose and parse it, or drop "
    "search=True."
)


def _text_config_kwargs(
    *,
    system: str | None,
    json_schema: dict | None,
    temperature: float,
    max_tokens: int,
    thinking_budget: int | None,
    search: bool = False,
) -> dict[str, Any]:
    """The GenerateContentConfig kwargs shared by `gemini_text`/`gemini_search`.

    With `search=False` (every pre-existing caller) the returned mapping is
    byte-identical to what `gemini_text` always built.
    """
    from google.genai import types

    if search and json_schema:
        raise ValueError(_SEARCH_WITH_SCHEMA)

    cfg_kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }
    if system:
        cfg_kwargs["system_instruction"] = system
    if json_schema:
        cfg_kwargs["response_mime_type"] = "application/json"
        cfg_kwargs["response_schema"] = json_schema
    if thinking_budget is not None:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=thinking_budget,
        )
    if search:
        cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    return cfg_kwargs


def grounding_sources(resp: Any) -> list[dict]:
    """The web pages a grounded answer was actually built from.

    Reads `candidates[].grounding_metadata.grounding_chunks[].web` — deduped by
    uri (the same page is routinely cited by several chunks) and defensive
    throughout: no grounding metadata simply means an empty list, never a
    crash, because a citation-less answer is still an answer.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for cand in getattr(resp, "candidates", None) or []:
        meta = getattr(cand, "grounding_metadata", None)
        for chunk in (getattr(meta, "grounding_chunks", None) or []):
            web = getattr(chunk, "web", None) or getattr(chunk, "retrieved_context", None)
            if web is None:
                continue
            uri = str(getattr(web, "uri", "") or "").strip()
            title = str(getattr(web, "title", "") or "").strip()
            key = uri or title
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"title": title or uri, "uri": uri})
    return out


async def gemini_text(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    json_schema: dict | None = None,
    temperature: float = 0.4,
    max_tokens: int = 2000,
    thinking_budget: int | None = None,
    search: bool = False,
) -> Any:
    """Generate text (or structured JSON). Returns str unless json_schema is set,
    in which case the parsed object is returned.

    `thinking_budget` (Gemini 2.5 only): set to 0 to disable hidden thinking
    tokens. Useful for short-text tasks where thinking tokens silently eat
    the output budget and cause truncation around 80 chars.

    `search` (default False — every existing call site is untouched) attaches
    the Google Search tool so the model looks the subject up instead of
    recalling it. It CANNOT be combined with `json_schema` (ValueError), and the
    return value stays a plain str: use `gemini_search` when you also want the
    sources it read.

    The bad-arguments check sits OUTSIDE the retry (`_gemini_text_call` is the
    retried half): a request that can never be valid must fail on the first
    attempt, not after four rounds of exponential backoff.
    """
    if search and json_schema:
        raise ValueError(_SEARCH_WITH_SCHEMA)
    return await _gemini_text_call(
        prompt, system=system, model=model, json_schema=json_schema,
        temperature=temperature, max_tokens=max_tokens,
        thinking_budget=thinking_budget, search=search,
    )


@_retry()
async def _gemini_text_call(
    prompt: str,
    *,
    system: str | None,
    model: str | None,
    json_schema: dict | None,
    temperature: float,
    max_tokens: int,
    thinking_budget: int | None,
    search: bool,
) -> Any:
    """`gemini_text`'s body, unchanged, with the retry/failover/meter it always
    had. Split out only so argument validation can fail fast."""
    from google.genai import types

    model_name = model or config.vertex_gemini_model
    cfg_kwargs = _text_config_kwargs(
        system=system, json_schema=json_schema, temperature=temperature,
        max_tokens=max_tokens, thinking_budget=thinking_budget, search=search,
    )

    resp = await _generate_with_failover(
        lambda client: client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
    )
    _charge_gemini_usage(model_name, resp)
    return _parse_response(resp, json_schema is not None)


@_retry()
async def gemini_search(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 3000,
    thinking_budget: int | None = None,
) -> tuple[str, list[dict]]:
    """A Google-grounded answer AND the pages it was built from.

    Returns `(text, sources)` where `sources` is `[{"title": …, "uri": …}]` — the
    one place in this module that hands grounding metadata back, so a caller
    that needs to PROVE it looked something up does not have to reach into the
    raw SDK response. Same failover, same cost meter, same budget guard as every
    other call here.

    There is deliberately no `json_schema` parameter: grounding and structured
    output are mutually exclusive on Vertex (see `_SEARCH_WITH_SCHEMA`), so the
    parameter simply does not exist rather than existing and always failing.
    """
    from google.genai import types

    model_name = model or config.vertex_gemini_model
    cfg_kwargs = _text_config_kwargs(
        system=system, json_schema=None, temperature=temperature,
        max_tokens=max_tokens, thinking_budget=thinking_budget, search=True,
    )

    resp = await _generate_with_failover(
        lambda client: client.aio.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
    )
    _charge_gemini_usage(model_name, resp)
    return str(_parse_response(resp, False) or ""), grounding_sources(resp)


@_retry()
async def gemini_multimodal(
    prompt: str,
    *,
    images: list[Path] | None = None,
    image_uris: list[str] | None = None,
    audio: list[Path] | None = None,
    system: str | None = None,
    model: str | None = None,
    json_schema: dict | None = None,
    temperature: float = 0.4,
    max_tokens: int = 2000,
    thinking_budget: int | None = None,
) -> Any:
    """Generate text from a prompt + images and/or audio (local files and/or
    GCS/HTTPS URIs).

    `audio` lets callers pass local audio clips (e.g. a video's extracted WAV)
    so Gemini can transcribe + reason over speech natively in one call — used by
    the brand-video ingest to get a transcript + brand analysis without a
    separate Whisper round-trip.

    `thinking_budget` (Gemini 2.5 only): set to 0 to disable hidden thinking
    tokens. Use this for short-text outputs where thinking eats the budget.
    Leave unset (None) for structured-schema calls that benefit from thinking.
    """
    from google.genai import types

    model_name = model or config.vertex_gemini_model
    parts: list[Any] = [types.Part.from_text(text=prompt)]
    for p in (images or []):
        mime, _ = mimetypes.guess_type(str(p))
        parts.append(types.Part.from_bytes(data=p.read_bytes(), mime_type=mime or "image/png"))
    for uri in (image_uris or []):
        mime, _ = mimetypes.guess_type(uri)
        parts.append(types.Part.from_uri(file_uri=uri, mime_type=mime or "image/png"))
    for a in (audio or []):
        mime, _ = mimetypes.guess_type(str(a))
        parts.append(types.Part.from_bytes(data=a.read_bytes(), mime_type=mime or "audio/wav"))

    cfg_kwargs: dict[str, Any] = {
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }
    if system:
        cfg_kwargs["system_instruction"] = system
    if json_schema:
        cfg_kwargs["response_mime_type"] = "application/json"
        cfg_kwargs["response_schema"] = json_schema
    if thinking_budget is not None:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=thinking_budget,
        )

    resp = await _generate_with_failover(
        lambda client: client.aio.models.generate_content(
            model=model_name,
            contents=parts,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
    )
    _charge_gemini_usage(model_name, resp)
    return _parse_response(resp, json_schema is not None)


def _charge_gemini_usage(model: str, resp: Any) -> None:
    usage = getattr(resp, "usage_metadata", None)
    if not usage:
        return
    in_tokens = getattr(usage, "prompt_token_count", 0) or 0
    out_tokens = getattr(usage, "candidates_token_count", 0) or 0
    cost = get_meter().estimate_gemini(model, in_tokens, out_tokens)
    get_meter().add(model, cost)


def _parse_response(resp: Any, expect_json: bool) -> Any:
    """Pull text out of a GenerateContentResponse; parse as JSON if requested."""
    import json as _json

    text = getattr(resp, "text", None)
    if text is None:
        # Fall back to digging through candidates[0].content.parts[0].text
        try:
            text = resp.candidates[0].content.parts[0].text
        except Exception:
            text = ""
    text = (text or "").strip()
    if not expect_json:
        return text

    # parsed may be present on the SDK response (when response_schema given)
    parsed = getattr(resp, "parsed", None)
    if parsed is not None:
        return parsed
    # Strip code fences if the model wrapped its output
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        # Most common cause: the response was truncated by max_tokens (Pro
        # burns thinking tokens too). Capture more of the raw so callers can
        # actually debug the failure mode.
        log.warning(
            "Gemini JSON parse failed (text_len=%d); returning raw fragment",
            len(text),
        )
        return {"_raw": text[:4000]}


# -----------------------------------------------------------------------------
# Embeddings
# -----------------------------------------------------------------------------

@_retry()
async def gemini_embed(
    texts: list[str],
    *,
    model: str | None = None,
    dim: int | None = None,
    task_type: str = "SEMANTIC_SIMILARITY",
) -> list[list[float]]:
    """Embed one or more strings. Returns a list of vectors (768d by default)."""
    from google.genai import types

    if not texts:
        return []
    model_name = model or config.vertex_embed_model
    output_dim = dim or config.vertex_embed_dim

    resp = await _generate_with_failover(
        lambda client: client.aio.models.embed_content(
            model=model_name,
            contents=texts,
            config=types.EmbedContentConfig(
                output_dimensionality=output_dim,
                task_type=task_type,
            ),
        )
    )
    # Cost: estimate by character count → rough token approximation
    total_chars = sum(len(t) for t in texts)
    approx_tokens = max(1, total_chars // 4)
    cost = get_meter().estimate_gemini(model_name, approx_tokens, 0)
    get_meter().add(model_name, cost)

    vecs: list[list[float]] = []
    for emb in resp.embeddings:
        values = getattr(emb, "values", None) or getattr(emb, "embedding", None)
        vecs.append(list(values or []))
    return vecs


# -----------------------------------------------------------------------------
# Image generation (Nano Banana / Imagen)
# -----------------------------------------------------------------------------

# Approximate cost per image, used by the meter. Real billing is per-request
# at GCP rates; this is a conservative estimate for budget gating.
_IMAGE_PRICE_USD = {
    "gemini-2.5-flash-image":  0.04,
    "gemini-3-pro-image-preview": 0.13,  # "Nano Banana Pro" — higher-fidelity tier
    "gemini-3-pro-image": 0.134,         # Nano Banana Pro, stable id (1K/2K output, 2026-09 list price)
    "imagen-3.0-generate-002": 0.04,
    "imagen-3.0-fast-generate-001": 0.02,
}


# Access to the premium image tier is granted PER GCP PROJECT. On a project
# without it, every call to it returns a hard `404 NOT_FOUND: Publisher model
# … was not found`, which is not retryable and not transient — retrying just
# burns the attempt budget and then fails the user's generation outright.
# So: notice it once, then quietly serve the standard tier for the rest of the
# process. Flipped back only by a restart (i.e. after access is granted).
_pro_image_unavailable = False


def _is_model_not_found(exc: BaseException) -> bool:
    """A 404 for the model itself — a provisioning fact, not a blip."""
    s = str(exc)
    return "NOT_FOUND" in s and "Publisher model" in s


def resolve_image_target(
    model: str | None, location: str | None,
) -> tuple[str, str | None]:
    """The (model, location) an image call should actually use.

    Degrades the premium pair to the standard one once this project has proven
    it has no access. Both axes move together: preview image models live on a
    single endpoint, so keeping `global` while dropping to a non-preview model
    would 404 on the location instead.
    """
    model_name = model or config.vertex_image_model
    if _pro_image_unavailable and model_name == config.vertex_image_pro_model:
        return config.vertex_image_model, config.google_cloud_location
    return model_name, location


def _note_pro_image_unavailable(model_name: str) -> tuple[str, str | None]:
    """Record the 404 and hand back the pair to retry with."""
    global _pro_image_unavailable
    if not _pro_image_unavailable:
        _pro_image_unavailable = True
        log.warning(
            "Image model %r is not available to project %s — falling back to %r "
            "on %s for the rest of this process. Grant access in Vertex Model "
            "Garden, or set VERTEX_IMAGE_PRO_MODEL / VERTEX_IMAGE_PRO_LOCATION "
            "to a pair this project can reach.",
            model_name, config.google_cloud_project,
            config.vertex_image_model, config.google_cloud_location,
        )
    return config.vertex_image_model, config.google_cloud_location


@_image_retry()
async def gemini_image(
    prompt: str,
    *,
    model: str | None = None,
    aspect_ratio: str | None = None,
    source_images: list[Path] | None = None,
    system_instruction: str | None = None,
    location: str | None = None,
) -> bytes:
    """Generate a still image. Returns PNG/JPEG bytes (model-dependent).

    Routes between two SDK shapes based on the model ID:
      - imagen-*  → `client.aio.models.generate_images(...)` (text-to-image only;
                    source_images are ignored — Imagen has separate edit APIs)
      - anything else (default: gemini-2.5-flash-image / Nano Banana)
        → `client.aio.models.generate_content(...)` with IMAGE modality;
          source_images are passed as `Part.from_bytes(...)` BEFORE the text
          prompt (images-first ordering improves conditioning).
          Pass `system_instruction` to set brand/style rules globally.
    """
    from google.genai import types

    model_name, location = resolve_image_target(model, location)
    client = get_client(location)
    price = _IMAGE_PRICE_USD.get(model_name, 0.05)

    if model_name.startswith("imagen"):
        if source_images:
            log.warning(
                "Imagen text-to-image path ignores source_images; switch "
                "VERTEX_IMAGE_MODEL to a Gemini image model for variations."
            )
        async with _image_semaphore():
            resp = await client.aio.models.generate_images(
                model=model_name,
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio=aspect_ratio or "16:9",
                ),
            )
        get_meter().add(model_name, price)
        imgs = getattr(resp, "generated_images", None) or []
        if not imgs:
            raise RuntimeError(f"Imagen returned no images for prompt: {prompt[:120]}")
        return imgs[0].image.image_bytes

    # Gemini multimodal — images first, then the text prompt.
    # Images-first ordering gives the model better visual grounding before
    # reading the instructions.
    parts: list[Any] = []
    for p in (source_images or []):
        if not p.exists():
            continue
        mime, _ = mimetypes.guess_type(str(p))
        parts.append(
            types.Part.from_bytes(data=p.read_bytes(), mime_type=mime or "image/png")
        )
    parts.append(types.Part.from_text(text=prompt))

    cfg_kwargs: dict[str, Any] = {"response_modalities": ["TEXT", "IMAGE"]}
    if system_instruction and system_instruction.strip():
        cfg_kwargs["system_instruction"] = system_instruction.strip()
    # The shape must be forced at the API level, as gemini_image_multipart
    # does: without it the model picks its own and follows its reference
    # photos — TH v2 asked for 9:16 and got 1248x832 stills, which Veo then
    # letterboxed into a 9:16 frame. Only when the caller asks for a shape:
    # a caller that passes none (the template fill) wants the model to keep
    # its template's own.
    if aspect_ratio and hasattr(types, "ImageConfig"):
        cfg_kwargs["image_config"] = types.ImageConfig(aspect_ratio=aspect_ratio)
    gen_config = types.GenerateContentConfig(**cfg_kwargs)

    try:
        async with _image_semaphore():
            resp = await client.aio.models.generate_content(
                model=model_name,
                contents=parts,
                config=gen_config,
            )
    except Exception as exc:  # noqa: BLE001 — narrowed on the next line
        if not (_is_model_not_found(exc)
                and model_name == config.vertex_image_pro_model):
            raise
        model_name, location = _note_pro_image_unavailable(model_name)
        client = get_client(location)
        price = _IMAGE_PRICE_USD.get(model_name, 0.05)
        async with _image_semaphore():
            resp = await client.aio.models.generate_content(
                model=model_name,
                contents=parts,
                config=gen_config,
            )
    _charge_gemini_usage(model_name, resp)
    get_meter().add(model_name, price)
    return _extract_image_bytes(resp, model_name)


@_image_retry()
async def gemini_image_multipart(
    parts: list[tuple[str, Any]],
    *,
    system_instruction: str | None = None,
    model: str | None = None,
    location: str | None = None,
    aspect_ratio: str | None = None,
) -> bytes:
    """Generate an image from a STRICTLY-ORDERED sequence of text/image parts.

    Unlike `gemini_image` (which forces images-first, single-text-last), this
    preserves the exact order the caller specifies — required when the position
    of an instruction relative to its image matters (e.g. a logo-replication
    instruction that must immediately precede the logo bytes).

    `parts` is a list of (kind, value) tuples:
      - ("text",  str)        → a text Part
      - ("image", Path | str) → an image Part loaded from disk (skipped if the
                                 file is missing)

    Example:
      parts = [
        ("text",  "Extract this flat 2D logo and place it in a corner:"),
        ("image", logo_path),
        ("text",  "Use this strictly as a STYLE REFERENCE:"),
        ("image", ref_path),
        ("text",  expanded_prompt),
      ]

    Returns raw image bytes (PNG/JPEG/WebP). Reuses the same Vertex client,
    cost meter, and response parsing as `gemini_image`.
    """
    from google.genai import types

    model_name, location = resolve_image_target(model, location)
    client = get_client(location)
    price = _IMAGE_PRICE_USD.get(model_name, 0.05)

    built: list[Any] = []
    for kind, value in parts:
        if kind == "text":
            text = "" if value is None else str(value)
            if text.strip():
                built.append(types.Part.from_text(text=text))
        elif kind == "image":
            p = value if isinstance(value, Path) else Path(value)
            if not p.exists():
                log.warning("gemini_image_multipart: image part missing on disk: %s", p)
                continue
            mime, _ = mimetypes.guess_type(str(p))
            built.append(
                types.Part.from_bytes(data=p.read_bytes(), mime_type=mime or "image/png")
            )
        else:
            raise ValueError(f"unknown part kind {kind!r} (expected 'text' or 'image')")

    if not built:
        raise RuntimeError("gemini_image_multipart: no usable parts to send")

    cfg_kwargs: dict[str, Any] = {"response_modalities": ["TEXT", "IMAGE"]}
    if system_instruction and system_instruction.strip():
        cfg_kwargs["system_instruction"] = system_instruction.strip()
    # Force the output aspect at the API level (a text directive alone is NOT
    # honoured by Gemini image models). Gemini 3 Pro Image (Nano Banana Pro)
    # accepts ImageConfig(aspect_ratio=...) e.g. "9:16" / "16:9". Guarded so an
    # older google-genai without ImageConfig silently falls back to text-only.
    if aspect_ratio and hasattr(types, "ImageConfig"):
        cfg_kwargs["image_config"] = types.ImageConfig(aspect_ratio=aspect_ratio)
    gen_config = types.GenerateContentConfig(**cfg_kwargs)

    try:
        async with _image_semaphore():
            resp = await client.aio.models.generate_content(
                model=model_name,
                contents=built,
                config=gen_config,
            )
    except Exception as exc:  # noqa: BLE001 — narrowed on the next line
        if not (_is_model_not_found(exc)
                and model_name == config.vertex_image_pro_model):
            raise
        # First call on a project without the premium tier. Degrade instead of
        # failing the user's render; every later call skips straight to the
        # standard pair via resolve_image_target().
        model_name, location = _note_pro_image_unavailable(model_name)
        client = get_client(location)
        price = _IMAGE_PRICE_USD.get(model_name, 0.05)
        async with _image_semaphore():
            resp = await client.aio.models.generate_content(
                model=model_name,
                contents=built,
                config=gen_config,
            )
    _charge_gemini_usage(model_name, resp)
    get_meter().add(model_name, price)
    return _extract_image_bytes(resp, model_name)


# -----------------------------------------------------------------------------
# Veo — long-running video generation
# -----------------------------------------------------------------------------

@_retry()
async def veo_generate(
    prompt: str,
    *,
    model: str | None = None,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    sample_count: int = 1,
    with_audio: bool = False,
    reference_images: list[Path] | None = None,
    image_uris: list[str] | None = None,
    gcs_uri_prefix: str | None = None,
    start_image: Path | None = None,
    last_image: Path | None = None,
    negative_prompt: str | None = None,
    person_generation: str | None = None,
    seed: int | None = None,
) -> dict:
    """Submit a Veo generation. Returns immediately with operation metadata.

    The caller is responsible for persisting `operation_name` and polling via
    `veo_poll`. Veo writes the finished MP4 to GCS (the bucket configured in
    `gcs_uri_prefix` or `VEO_OUTPUT_BUCKET`).
    """
    from google.genai import types

    model_name = model or config.veo_model
    duration = int(duration_sec or config.veo_max_duration_sec)
    aspect = aspect_ratio or config.veo_default_aspect

    # Pre-charge for the requested duration. If Veo fails, we mark the row's
    # metadata.cost_refunded=true; the meter is not actually decremented (we
    # paid GCP regardless of whether the artifact is usable).
    cost = get_meter().estimate_veo(model_name, duration, with_audio)
    get_meter().add(model_name, cost)

    output_gcs = gcs_uri_prefix
    if not output_gcs and config.veo_output_bucket:
        output_gcs = f"gs://{config.veo_output_bucket}/producer/"
    if not output_gcs:
        raise RuntimeError("VEO_OUTPUT_BUCKET is not set")

    # Build optional reference-image list. Veo 3.1 accepts up to 3.
    refs: list[Any] = []
    for p in (reference_images or [])[:3]:
        mime, _ = mimetypes.guess_type(str(p))
        img = types.Image(
            image_bytes=p.read_bytes(),
            mime_type=mime or "image/png",
        )
        refs.append(types.VideoGenerationReferenceImage(image=img, reference_type="asset"))
    for uri in (image_uris or [])[:max(0, 3 - len(refs))]:
        img = types.Image(gcs_uri=uri)
        refs.append(types.VideoGenerationReferenceImage(image=img, reference_type="asset"))

    cfg_kwargs: dict[str, Any] = {
        "aspect_ratio": aspect,
        "duration_seconds": duration,
        "number_of_videos": sample_count,
        "output_gcs_uri": output_gcs,
        "generate_audio": with_audio,
    }
    # Permit adult person generation. Veo's RAI blocks reference-image and
    # image-to-video jobs whose input depicts a person UNLESS this is set —
    # surfacing as "...prompt contains words... usage guidelines (29310472)"
    # even when the text prompt is clean. "allow_adult" is the documented value
    # for image inputs (no minors). Override per-call or via VEO_PERSON_GENERATION;
    # pass "default" to omit it and fall back to Vertex's server-side default.
    pgen = person_generation or os.getenv("VEO_PERSON_GENERATION", "allow_adult")
    if pgen and pgen.lower() != "default":
        cfg_kwargs["person_generation"] = pgen
    if refs:
        cfg_kwargs["reference_images"] = refs
    if negative_prompt:
        # Veo allows up to ~1000 chars of negative prompt. Truncate to be safe.
        cfg_kwargs["negative_prompt"] = negative_prompt[:1000]
    if seed is not None:
        # Veo accepts a uint32 seed; same seed + same inputs ⇒ more consistent
        # output (docs: improves, not guarantees, determinism).
        cfg_kwargs["seed"] = int(seed) & 0x7FFFFFFF

    # Image-to-video conditioning — Veo generates starting from this frame
    # (`image=`, top level). Optionally ALSO pin the END frame via
    # GenerateVideosConfig.last_frame: passing the SAME keyframe as both first
    # and last frame anchors identity at both ends of the clip, which bounds the
    # face drift Veo otherwise accumulates mid-clip (a policy-safe, image-channel
    # identity lock — no prompt risk).
    start_img = None
    if start_image is not None and start_image.exists():
        mime, _ = mimetypes.guess_type(str(start_image))
        start_img = types.Image(
            image_bytes=start_image.read_bytes(),
            mime_type=mime or "image/png",
        )
    if last_image is not None and last_image.exists():
        lmime, _ = mimetypes.guess_type(str(last_image))
        cfg_kwargs["last_frame"] = types.Image(
            image_bytes=last_image.read_bytes(),
            mime_type=lmime or "image/png",
        )

    client = get_client()
    gen_kwargs: dict[str, Any] = {
        "model": model_name,
        "prompt": prompt,
        "config": types.GenerateVideosConfig(**cfg_kwargs),
    }
    if start_img is not None:
        gen_kwargs["image"] = start_img

    # Retry-with-backoff on 429 RESOURCE_EXHAUSTED. Veo's per-base-model
    # concurrent-submission quota is tight; bursts of N+2 parallel submits
    # (multi-scene runs) routinely trip it. Up to 4 attempts total, 2s/4s/8s
    # between, refunding the cost meter on every failed attempt.
    op = None
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            op = await client.aio.models.generate_videos(**gen_kwargs)
            last_exc = None
            break
        except Exception as e:
            get_meter().refund(model_name, cost)
            msg = str(e)
            is_quota = (
                " 429 " in f" {msg} "
                or "RESOURCE_EXHAUSTED" in msg
                or "Quota exceeded" in msg
            )
            log.warning(
                "Veo submit failed (model=%s duration=%ds cost=$%.4f refunded "
                "attempt=%d/4 quota=%s): %s",
                model_name, duration, cost, attempt + 1, is_quota, e,
            )
            last_exc = e
            if not is_quota or attempt == 3:
                break
            # Re-charge for the next attempt (we'll refund again if it fails).
            get_meter().add(model_name, cost)
            await asyncio.sleep(2 * (2 ** attempt))  # 2s, 4s, 8s
    if op is None:
        # Map terminal 429 to a typed error the caller can convert to a 503.
        if last_exc is not None and (
            " 429 " in f" {str(last_exc)} "
            or "RESOURCE_EXHAUSTED" in str(last_exc)
            or "Quota exceeded" in str(last_exc)
        ):
            raise VeoQuotaExhaustedError(str(last_exc)) from last_exc
        # Non-quota error — re-raise verbatim.
        raise last_exc if last_exc is not None else RuntimeError("Veo submit returned no op")
    op_name = getattr(op, "name", None) or str(op)
    # Record the charge against this op so it's refunded if the video is later
    # blocked/filtered at poll time (GCP doesn't bill blocked videos).
    get_meter().note_op(op_name, model_name, cost)
    log.info(
        "Veo submitted: model=%s duration=%ds audio=%s ratio=%s image_to_video=%s "
        "negative_prompt=%s op=%s cost=$%.4f",
        model_name, duration, with_audio, aspect, start_img is not None,
        bool(negative_prompt), op_name, cost,
    )
    return {
        "operation_name": op_name,
        "model": model_name,
        "duration_sec": duration,
        "with_audio": with_audio,
        "aspect_ratio": aspect,
        "cost_usd": cost,
    }


@_retry()
async def veo_poll(operation_name: str) -> dict:
    """Poll a Veo long-running operation by name.

    Returns:
      {done: True,  gcs_uri: 'gs://...mp4'}
      {done: True,  error:   '...'}
      {done: False}
    """
    from google.genai import types

    client = get_client()
    # operations.get expects an Operation-shaped object (it reads `.name`),
    # not a bare string. Wrap our stored name into the matching stub.
    stub = types.GenerateVideosOperation(name=operation_name)
    op = await client.aio.operations.get(stub)
    done = bool(getattr(op, "done", False))
    if not done:
        return {"done": False}

    err = getattr(op, "error", None)
    if err:
        # `error` is typically a dict with `{code, message}` on Vertex.
        if isinstance(err, dict):
            msg = err.get("message") or str(err)
        else:
            msg = getattr(err, "message", None) or str(err)
        log.warning("Veo op %s failed: %s", operation_name, msg)
        return {"done": True, "error": msg}

    response = getattr(op, "response", None)
    if not response:
        result = getattr(op, "result", None)
        response = result

    # response is GenerateVideosResponse: .generated_videos[i].video.uri
    gcs_uri: str | None = None
    try:
        videos = getattr(response, "generated_videos", None) or []
        if videos:
            video = getattr(videos[0], "video", None)
            gcs_uri = getattr(video, "uri", None) or getattr(video, "gcs_uri", None)
    except Exception:
        log.exception("Unexpected Veo response shape")

    if not gcs_uri:
        # The op "succeeded" but no video came back — almost always an RAI OUTPUT
        # filter. Veo 3.x audio-RAI is documented as NON-deterministic (a fresh
        # identical submit usually works); the celebrity/likeness detector can also
        # fire on a clean photoreal face. Surface the reason so the poller can
        # retry and the user sees WHY (not a cryptic "missing URI").
        # GCP does NOT bill blocked videos → refund the pre-charged cost so a run
        # of content-filter false positives can't exhaust the Vertex budget.
        get_meter().refund_op(operation_name)
        reasons = getattr(response, "rai_media_filtered_reasons", None) or []
        count = getattr(response, "rai_media_filtered_count", None)
        if reasons:
            detail = "; ".join(str(r) for r in reasons)[:400]
            log.warning("Veo op %s output filtered (count=%s): %s",
                        operation_name, count, detail)
            return {"done": True, "error": f"Veo output blocked by content filter: {detail}"}
        log.warning("Veo op %s done but produced no video and no filter reason "
                    "(response=%r)", operation_name, response)
        return {"done": True, "error": "Veo produced no video (output empty or filtered) — try again"}

    return {"done": True, "gcs_uri": gcs_uri}


# -----------------------------------------------------------------------------
# GCS download
# -----------------------------------------------------------------------------

async def download_gcs(gs_uri: str, dest_path: Path) -> Path:
    """Download a gs://bucket/object URI to a local path.

    Uses google-cloud-storage with ADC. Synchronous under the hood; we offload
    to a thread so we don't block the event loop.
    """
    if not gs_uri.startswith("gs://"):
        raise ValueError(f"Not a GCS URI: {gs_uri!r}")
    bucket_name, _, object_path = gs_uri[5:].partition("/")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    def _sync_download() -> Path:
        from google.cloud import storage
        client = storage.Client(project=config.google_cloud_project)
        blob = client.bucket(bucket_name).blob(object_path)
        blob.download_to_filename(str(dest_path))
        return dest_path

    return await asyncio.to_thread(_sync_download)
