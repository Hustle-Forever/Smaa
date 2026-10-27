"""Model gateway — the reasoning brain behind the agentic video editor.

The editor needs a model that can LOOK (vision), THINK (real reasoning tokens)
and ACT (tool calls) in a loop. This module owns that capability and hides which
provider actually serves it, because provider availability varies per
deployment:

    1. vertex-claude    — Claude on Vertex AI via the project's existing ADC /
                          service account. Preferred: no extra key, separate
                          quota pool from Gemini. Requires billing + Claude
                          enabled in the project's Model Garden.
    2. anthropic-claude — the direct Anthropic API, used when ANTHROPIC_API_KEY
                          is set. The escape hatch when Vertex Claude is not
                          enabled for the project.
    3. gemini           — Gemini with native function-calling and thinking ON,
                          driving the SAME tool loop. Always available wherever
                          the rest of the app already works, so the editor never
                          hard-fails just because Claude is unreachable.

`resolve_reasoner()` probes these in order and caches the verdict, so a
misconfigured Claude never costs more than one probe per TTL. Callers use
`reasoner_tool_loop()` and get identical semantics whichever provider answers.

Design notes that matter:

  * Tools are declared ONCE in a neutral JSON-Schema shape and translated per
    provider — no duplicated tool definitions.
  * `tool_dispatch` may return plain text OR {"text": ..., "images": [Path]}.
    Images are delivered to the model as real image content, which is what makes
    "go look at the video before you edit it" work.
  * The loop is BOUNDED (`max_steps`) and every step is recorded, so a confused
    model degrades into a short transcript instead of an infinite bill.
  * Claude requests carry prompt-cache breakpoints on the stable prefix (tools +
    system), which is most of the payload in a long agent loop.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..config import config

log = logging.getLogger("archivist.gateway")

# Providers, in preference order.
VERTEX_CLAUDE = "vertex-claude"
ANTHROPIC_CLAUDE = "anthropic-claude"
GEMINI = "gemini"

# How long a successful/failed provider probe stays cached.
_PROBE_TTL_SEC = 15 * 60
# A failing provider is re-probed sooner than a healthy one would be, but not so
# often that every request pays a doomed round-trip.
_PROBE_FAIL_TTL_SEC = 5 * 60
# How long a provider that failed a REAL run non-retryably stays out of the
# rotation. Long enough that a broken capability doesn't get retried every
# request; short enough that a fixed config recovers on its own.
_DEMOTE_TTL_SEC = 30 * 60

# Hard ceilings for one agent run. `max_steps` bounds tool round-trips;
# `_MAX_TOOL_RESULT_CHARS` bounds how much any single tool can inject.
DEFAULT_MAX_STEPS = 24
# Wall-clock ceiling for one agent run. The project's single job slot is held
# for the whole run, so a wedged loop must not block the user indefinitely.
_RUN_DEADLINE_SEC = 12 * 60
_MAX_TOOL_RESULT_CHARS = 12000
_MAX_IMAGES_PER_RESULT = 4


@dataclass
class ReasonerInfo:
    """Which brain is live, and why."""

    provider: str
    model: str
    ok: bool
    reason: str
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "ok": self.ok,
            "reason": self.reason,
            "checked_at": self.checked_at,
        }


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

_vertex_claude_client: Any | None = None
_anthropic_client: Any | None = None


def _get_vertex_claude_client() -> Any:
    global _vertex_claude_client
    if _vertex_claude_client is not None:
        return _vertex_claude_client
    if not config.google_cloud_project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set")
    try:
        from anthropic import AsyncAnthropicVertex
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "anthropic[vertex] not installed. Run: pip install -r requirements.txt"
        ) from e
    _vertex_claude_client = AsyncAnthropicVertex(
        project_id=config.google_cloud_project,
        region=config.claude_vertex_region,
    )
    log.info(
        "Claude-on-Vertex client ready (project=%s region=%s)",
        config.google_cloud_project, config.claude_vertex_region,
    )
    return _vertex_claude_client


def _get_anthropic_client() -> Any:
    global _anthropic_client
    if _anthropic_client is not None:
        return _anthropic_client
    if not config.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "anthropic not installed. Run: pip install -r requirements.txt"
        ) from e
    _anthropic_client = AsyncAnthropic(api_key=config.anthropic_api_key)
    log.info("Claude client ready (direct Anthropic API)")
    return _anthropic_client


def reset_clients() -> None:
    """Drop cached clients + the probe verdict (tests, and after a config change)."""
    global _vertex_claude_client, _anthropic_client, _cached_reasoner
    _vertex_claude_client = None
    _anthropic_client = None
    _cached_reasoner = None
    _demoted.clear()


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

_cached_reasoner: Optional[ReasonerInfo] = None
_probe_lock: asyncio.Lock | None = None
# provider -> when it last failed a real run non-retryably. Demoted providers
# are skipped during resolution so the chain can fall through to the next one.
_demoted: dict[str, float] = {}


def _is_demoted(provider: str) -> bool:
    at = _demoted.get(provider)
    if at is None:
        return False
    if time.time() - at >= _DEMOTE_TTL_SEC:
        _demoted.pop(provider, None)
        return False
    return True


def _get_probe_lock() -> asyncio.Lock:
    global _probe_lock
    if _probe_lock is None:
        _probe_lock = asyncio.Lock()
    return _probe_lock


def _short(exc: BaseException, limit: int = 180) -> str:
    return f"{type(exc).__name__}: {exc}"[:limit]


async def _probe_claude(client: Any, model: str) -> tuple[bool, str]:
    """One tiny request that exercises the SAME request shape the agent loop
    sends — tools, adaptive thinking, effort and a cache breakpoint.

    A reachability-only probe is worse than useless here: a provider that is
    reachable but rejects the real payload (old SDK without `output_config`, a
    model that refuses adaptive thinking, caching not enabled) would pass, then
    400 on every actual request, and the loop would keep re-selecting it."""
    try:
        await client.messages.create(
            model=model,
            max_tokens=16,
            system=[{"type": "text", "text": "Reply with the single word ok.",
                     "cache_control": {"type": "ephemeral"}}],
            tools=[{
                "name": "noop",
                "description": "Probe tool. Do not call it.",
                "input_schema": {"type": "object",
                                 "properties": {"x": {"type": "string"}}},
            }],
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "ok"}],
        )
        return True, "reachable"
    except Exception as exc:  # noqa: BLE001 — any failure disqualifies the provider
        return False, _short(exc)


async def _probe_gemini(model: str) -> tuple[bool, str]:
    try:
        from .vertex_client import gemini_text

        await gemini_text("ok", max_tokens=8, thinking_budget=0, model=model)
        return True, "reachable"
    except Exception as exc:  # noqa: BLE001
        return False, _short(exc)


async def resolve_reasoner(force: bool = False) -> ReasonerInfo:
    """Pick the best available reasoning backend, cached for `_PROBE_TTL_SEC`.

    Order: Vertex Claude -> direct Anthropic -> Gemini. Gemini is returned even
    if its own probe fails, because it is the last resort and its failure is
    better surfaced by the real call than by refusing to return a provider."""
    global _cached_reasoner

    now = time.time()
    cached = _cached_reasoner
    if not force and cached is not None:
        ttl = _PROBE_TTL_SEC if cached.ok else _PROBE_FAIL_TTL_SEC
        if now - cached.checked_at < ttl:
            return cached

    async with _get_probe_lock():
        # Another coroutine may have refreshed while we waited for the lock.
        cached = _cached_reasoner
        if not force and cached is not None:
            ttl = _PROBE_TTL_SEC if cached.ok else _PROBE_FAIL_TTL_SEC
            if time.time() - cached.checked_at < ttl:
                return cached

        reasons: list[str] = []
        if force:
            # An explicit re-probe is the operator saying "I fixed it" — give
            # every provider another chance.
            _demoted.clear()

        # 1. Claude on Vertex — preferred (no extra key, separate quota pool).
        if _is_demoted(VERTEX_CLAUDE):
            reasons.append("vertex-claude demoted after a failed run")
        elif config.ve_agent_provider in ("", "auto", VERTEX_CLAUDE) and config.google_cloud_project:
            try:
                client = _get_vertex_claude_client()
                ok, why = await _probe_claude(client, config.claude_model)
                if ok:
                    info = ReasonerInfo(VERTEX_CLAUDE, config.claude_model, True,
                                        "Claude on Vertex AI (ADC)")
                    _cached_reasoner = info
                    log.info("[gateway] reasoner = %s (%s)", info.provider, info.model)
                    return info
                reasons.append(f"vertex-claude unavailable ({why})")
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"vertex-claude unavailable ({_short(exc)})")

        # 2. Direct Anthropic API — the escape hatch when Vertex has no Claude.
        if _is_demoted(ANTHROPIC_CLAUDE):
            reasons.append("anthropic-claude demoted after a failed run")
        elif config.ve_agent_provider in ("", "auto", ANTHROPIC_CLAUDE) and config.anthropic_api_key:
            try:
                client = _get_anthropic_client()
                ok, why = await _probe_claude(client, config.anthropic_model)
                if ok:
                    info = ReasonerInfo(ANTHROPIC_CLAUDE, config.anthropic_model, True,
                                        "Claude via the Anthropic API")
                    _cached_reasoner = info
                    log.info("[gateway] reasoner = %s (%s)", info.provider, info.model)
                    return info
                reasons.append(f"anthropic-claude unavailable ({why})")
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"anthropic-claude unavailable ({_short(exc)})")
        elif config.ve_agent_provider in ("", "auto") and not config.anthropic_api_key:
            reasons.append("anthropic-claude unavailable (ANTHROPIC_API_KEY not set)")

        # 3. Gemini — always the floor, even when the operator pinned a Claude
        # provider that turned out to be unreachable. A dead editor is worse
        # than a downgraded one, and the reason string (surfaced on the panel's
        # brain badge) still names exactly why the pin didn't take.
        if config.ve_agent_provider in (VERTEX_CLAUDE, ANTHROPIC_CLAUDE):
            log.warning("[gateway] pinned reasoner %s unavailable (%s) — "
                        "falling back to Gemini", config.ve_agent_provider,
                        "; ".join(reasons) or "not configured")
        model = config.ve_agent_gemini_model
        ok, why = await _probe_gemini(model)
        info = ReasonerInfo(
            GEMINI, model, ok,
            ("Gemini function-calling agent" if ok else f"Gemini probe failed ({why})")
            + (" — " + "; ".join(reasons) if reasons else ""),
        )
        _cached_reasoner = info
        log.info("[gateway] reasoner = %s (%s) ok=%s — %s",
                 info.provider, info.model, info.ok, info.reason)
        return info


async def reasoner_status() -> dict:
    """Diagnostic blob for the UI: which brain is live and what was tried."""
    info = await resolve_reasoner()
    return {
        **info.as_dict(),
        "configured": {
            "google_cloud_project": bool(config.google_cloud_project),
            "claude_vertex_region": config.claude_vertex_region,
            "claude_model": config.claude_model,
            "anthropic_api_key_set": bool(config.anthropic_api_key),
            "anthropic_model": config.anthropic_model,
            "gemini_model": config.ve_agent_gemini_model,
            "forced_provider": config.ve_agent_provider or "auto",
        },
    }


# ---------------------------------------------------------------------------
# Tool-result normalisation (shared by both providers)
# ---------------------------------------------------------------------------

def _normalise_tool_output(out: Any) -> tuple[str, list[Path]]:
    """A dispatch result is either text, or {"text":..., "images":[Path,...]}."""
    if isinstance(out, dict):
        text = str(out.get("text") or "")
        raw = out.get("images") or []
        images = []
        for p in raw[:_MAX_IMAGES_PER_RESULT]:
            try:
                path = Path(p)
                if path.is_file():
                    images.append(path)
            except (TypeError, ValueError):
                continue
        return text[:_MAX_TOOL_RESULT_CHARS], images
    return str(out)[:_MAX_TOOL_RESULT_CHARS], []


def _trim_input(inp: dict) -> dict:
    """A compact echo of a tool's arguments for the step log.

    The step list is mirrored into the job blob, which the editor polls every
    two seconds — so an `edit` call carrying fifty ops must not be replayed in
    full on every poll. Scalars survive; anything bulky becomes a summary."""
    out: dict[str, Any] = {}
    for k, v in list(inp.items())[:12]:
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:120]
        elif isinstance(v, list):
            out[k] = f"[{len(v)} item(s)]"
        elif isinstance(v, dict):
            out[k] = "{…}"
    return out


def _image_b64(path: Path) -> tuple[str, str]:
    media = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if media not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media = "image/jpeg"
    return media, base64.b64encode(path.read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Claude tool loop
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    msg = f" {exc} ".lower()
    return (
        " 429 " in msg
        or "rate_limit" in msg
        or "resource_exhausted" in msg
        or "overloaded" in msg
        or " 529 " in msg
        or " 500 " in msg
        or "internal server" in msg
    )


async def _claude_create(client: Any, **kwargs: Any) -> Any:
    last: BaseException | None = None
    for attempt in range(4):
        try:
            return await client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable(exc) or attempt == 3:
                raise
            last = exc
            await asyncio.sleep(1.5 * (2 ** attempt))
    raise last if last is not None else RuntimeError("claude create failed")


def _claude_tools(tools: list[dict]) -> list[dict]:
    """Neutral tool specs -> Claude tool definitions, with a cache breakpoint on
    the last one so the (large, stable) tool block is cached across steps."""
    out: list[dict] = []
    for t in tools:
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("input_schema") or {"type": "object", "properties": {}},
        })
    if out:
        out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out


async def _run_claude_loop(
    *, client: Any, model: str, system: str, user_message: str,
    tools: list[dict], tool_dispatch, max_steps: int, max_tokens: int,
    effort: str, on_step, deadline: float = 0.0,
) -> dict:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    steps: list[dict] = []
    text_out = ""
    stopped = "done"

    base: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        # Cache the system prompt: it is identical on every step of the loop.
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "tools": _claude_tools(tools),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }

    for step in range(max_steps):
        if deadline and time.monotonic() > deadline:
            stopped = "deadline"
            break
        resp = await _claude_create(client, messages=messages, **base)
        messages.append({"role": "assistant", "content": resp.content})

        # Collect this turn's visible text (the final one becomes the reply).
        turn_text = "".join(
            b.text for b in (resp.content or [])
            if getattr(b, "type", None) == "text"
        ).strip()
        if turn_text:
            text_out = turn_text

        if getattr(resp, "stop_reason", None) != "tool_use":
            break

        results: list[dict] = []
        for block in resp.content or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = block.name
            inp = dict(block.input or {})
            try:
                raw = await tool_dispatch(name, inp)
                text, images = _normalise_tool_output(raw)
                content: Any
                if images:
                    content = [{"type": "text", "text": text or "(see image)"}]
                    for img in images:
                        media, data = _image_b64(img)
                        content.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": media, "data": data},
                        })
                else:
                    # An empty content block is rejected by the API and would
                    # fail the whole run, so a tool that legitimately returns
                    # nothing still has to say so.
                    content = text or "(no result)"
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content})
                steps.append({"step": step + 1, "tool": name, "input": _trim_input(inp),
                              "ok": True, "result": text[:400],
                              "images": len(images)})
            except Exception as exc:  # noqa: BLE001 — surface to the model, keep going
                log.warning("[gateway] tool %s failed: %s", name, exc)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"error: {exc}", "is_error": True})
                steps.append({"step": step + 1, "tool": name, "input": _trim_input(inp),
                              "ok": False, "result": _short(exc)})
            if on_step:
                try:
                    on_step(steps[-1])
                except Exception:  # noqa: BLE001 — progress reporting is best-effort
                    pass

        messages.append({"role": "user", "content": results})
    else:
        stopped = "max_steps"

    return {"text": text_out, "steps": steps, "stopped": stopped}


# ---------------------------------------------------------------------------
# Gemini tool loop (same contract, native function calling)
# ---------------------------------------------------------------------------

_JSON_TO_GEMINI = {
    "object": "OBJECT", "string": "STRING", "integer": "INTEGER",
    "number": "NUMBER", "boolean": "BOOLEAN", "array": "ARRAY",
}


def _schema_to_gemini(schema: Any) -> dict:
    """JSON Schema -> the OpenAPI-ish dialect google-genai expects (uppercased
    types, no unsupported keywords)."""
    if not isinstance(schema, dict):
        return {"type": "STRING"}
    out: dict[str, Any] = {}
    t = schema.get("type")
    if isinstance(t, list):  # {"type": ["string","null"]} -> first concrete type
        t = next((x for x in t if x != "null"), "string")
    if isinstance(t, str):
        out["type"] = _JSON_TO_GEMINI.get(t.lower(), "STRING")
    if schema.get("description"):
        out["description"] = str(schema["description"])
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        out["enum"] = [str(v) for v in schema["enum"]]
        out.setdefault("type", "STRING")
    if out.get("type") == "OBJECT":
        props = schema.get("properties")
        out["properties"] = {
            k: _schema_to_gemini(v) for k, v in (props or {}).items()
        } if isinstance(props, dict) else {}
        req = schema.get("required")
        if isinstance(req, list) and req:
            out["required"] = [str(r) for r in req]
    if out.get("type") == "ARRAY":
        out["items"] = _schema_to_gemini(schema.get("items") or {"type": "string"})
    if out.get("type") == "OBJECT" and not out.get("properties"):
        # Vertex rejects an OBJECT with no properties ANYWHERE in the tree, not
        # just at the top level. Nothing sensible can be declared for a
        # free-form object, so degrade it to a JSON string rather than emit a
        # declaration the API will refuse.
        desc = out.get("description") or schema.get("description") or ""
        return {"type": "STRING",
                "description": (desc + " (a JSON object, encoded as a string)").strip()}
    return out or {"type": "STRING"}


async def _run_gemini_loop(
    *, model: str, system: str, user_message: str, tools: list[dict],
    tool_dispatch, max_steps: int, max_tokens: int, effort: str, on_step,
    deadline: float = 0.0,
) -> dict:
    from google.genai import types  # noqa: PLC0415 — optional heavy import

    from .vertex_client import (  # noqa: PLC0415
        _charge_gemini_usage,
        _generate_with_failover,
    )

    declarations = []
    for t in tools:
        schema = t.get("input_schema") or {}
        kwargs: dict[str, Any] = {
            "name": t["name"], "description": t.get("description", ""),
        }
        # A parameter-less tool must omit `parameters` entirely — Vertex rejects
        # an OBJECT schema with no properties.
        if isinstance(schema.get("properties"), dict) and schema["properties"]:
            kwargs["parameters"] = _schema_to_gemini(schema)
        declarations.append(types.FunctionDeclaration(**kwargs))
    gem_tools = [types.Tool(function_declarations=declarations)] if declarations else None

    # Thinking budget scales with the requested effort — this is the whole point
    # of the rebuild, so never let it be 0 here.
    budget = {"low": 1024, "medium": 4096, "high": 8192,
              "xhigh": 16384, "max": 24576}.get(effort, 8192)

    contents: list[Any] = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    ]
    steps: list[dict] = []
    text_out = ""
    stopped = "done"

    cfg = types.GenerateContentConfig(
        system_instruction=system,
        tools=gem_tools,
        max_output_tokens=max_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=budget),
    )

    for step in range(max_steps):
        if deadline and time.monotonic() > deadline:
            stopped = "deadline"
            break
        resp = await _generate_with_failover(
            lambda client: client.aio.models.generate_content(
                model=model, contents=contents, config=cfg,
            )
        )
        # Keep the Vertex budget meter honest — the agent is now the heaviest
        # Gemini consumer in the app and would otherwise be invisible to it.
        try:
            _charge_gemini_usage(model, resp)
        except Exception:  # noqa: BLE001 — metering must never break a run
            pass

        calls: list[Any] = []
        parts_out: list[Any] = []
        turn_text = ""
        for cand in (resp.candidates or []):
            content = getattr(cand, "content", None)
            for part in (getattr(content, "parts", None) or []):
                parts_out.append(part)
                if getattr(part, "function_call", None):
                    calls.append(part.function_call)
                elif getattr(part, "text", None):
                    turn_text += part.text
        if turn_text.strip():
            text_out = turn_text.strip()

        if not calls:
            break

        # Echo the model turn back verbatim, then answer every call.
        contents.append(types.Content(role="model", parts=parts_out))

        response_parts: list[Any] = []
        image_parts: list[Any] = []
        for call in calls:
            name = call.name
            inp = dict(call.args or {})
            try:
                raw = await tool_dispatch(name, inp)
                text, images = _normalise_tool_output(raw)
                response_parts.append(types.Part.from_function_response(
                    name=name, response={"result": text or "(ok)"},
                ))
                for img in images:
                    media, _ = _image_b64(img)
                    image_parts.append(types.Part.from_bytes(
                        data=img.read_bytes(), mime_type=media,
                    ))
                steps.append({"step": step + 1, "tool": name, "input": _trim_input(inp),
                              "ok": True, "result": text[:400],
                              "images": len(images)})
            except Exception as exc:  # noqa: BLE001
                log.warning("[gateway] tool %s failed: %s", name, exc)
                response_parts.append(types.Part.from_function_response(
                    name=name, response={"error": str(exc)[:400]},
                ))
                steps.append({"step": step + 1, "tool": name, "input": _trim_input(inp),
                              "ok": False, "result": _short(exc)})
            if on_step:
                try:
                    on_step(steps[-1])
                except Exception:  # noqa: BLE001
                    pass

        contents.append(types.Content(role="user", parts=response_parts))
        # Gemini can't carry images inside a function response, so any frames the
        # tool returned ride in their own user turn right after it.
        if image_parts:
            contents.append(types.Content(
                role="user",
                parts=[types.Part.from_text(
                    text="Frames returned by the tool call above:")] + image_parts,
            ))
    else:
        stopped = "max_steps"

    return {"text": text_out, "steps": steps, "stopped": stopped}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def reasoner_tool_loop(
    *,
    system: str,
    user_message: str,
    tools: list[dict],
    tool_dispatch: Callable[[str, dict], Awaitable[Any]],
    max_steps: int | None = None,
    max_tokens: int = 16000,
    effort: str = "high",
    on_step: Callable[[dict], None] | None = None,
) -> dict:
    """Run a look -> think -> act loop on whichever brain is available.

    `tools` are neutral specs: {"name", "description", "input_schema"}.
    `tool_dispatch(name, input)` returns a string, or {"text":..., "images":[Path]}
    to show the model actual frames.

    Returns {"text", "steps", "stopped", "provider", "model"} — `stopped` is
    "done" when the model finished on its own and "max_steps" when the cap cut
    it off.
    """
    steps_cap = max_steps if max_steps is not None else config.ve_agent_max_steps
    steps_cap = max(1, min(int(steps_cap or DEFAULT_MAX_STEPS), 60))

    info = await resolve_reasoner()
    common = dict(system=system, user_message=user_message, tools=tools,
                  tool_dispatch=tool_dispatch, max_steps=steps_cap,
                  max_tokens=max_tokens, effort=effort, on_step=on_step,
                  # A step cap alone doesn't bound wall-clock: one step can
                  # render a still or shell out to ffmpeg. The job slot is
                  # per-project and blocks the user, so cap the run in time too.
                  deadline=time.monotonic() + _RUN_DEADLINE_SEC)

    try:
        if info.provider == VERTEX_CLAUDE:
            out = await _run_claude_loop(
                client=_get_vertex_claude_client(), model=info.model, **common)
        elif info.provider == ANTHROPIC_CLAUDE:
            out = await _run_claude_loop(
                client=_get_anthropic_client(), model=info.model, **common)
        else:
            out = await _run_gemini_loop(model=info.model, **common)
    except Exception as exc:
        # A provider that passed its probe but died mid-run is a stale verdict.
        # Clearing the cache alone is not enough: the probe would pass again and
        # re-select the same broken provider forever. So a NON-retryable failure
        # (a capability/permission 4xx) demotes the provider for a while, which
        # lets resolution actually fall through to the next one. Transient
        # failures (429/5xx) only invalidate the cache.
        global _cached_reasoner
        _cached_reasoner = None
        if not _is_retryable(exc):
            _demoted[info.provider] = time.time()
            log.warning("[gateway] %s failed non-retryably (%s) — demoting for %ds",
                        info.provider, _short(exc), _DEMOTE_TTL_SEC)
        else:
            log.warning("[gateway] %s failed transiently (%s) — re-probing next run",
                        info.provider, _short(exc))
        raise

    out["provider"] = info.provider
    out["model"] = info.model
    return out


# ---------------------------------------------------------------------------
# Back-compat: the old single-provider entry point.
# ---------------------------------------------------------------------------

async def claude_tool_loop(
    *, system: Any, messages: list[dict], tools: list[dict],
    tool_dispatch: Callable[[str, dict], Awaitable[Any]],
    model: str | None = None, max_tokens: int = 8000,
    effort: str = "high", extra: dict | None = None,
) -> dict:
    """Deprecated shim kept so any older caller still imports cleanly. Routes
    through `reasoner_tool_loop`, flattening `messages` to the opening user
    text (the only shape the previous callers ever used)."""
    opening = ""
    for m in messages or []:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            opening = m["content"]
            break
    out = await reasoner_tool_loop(
        system=str(system), user_message=opening, tools=tools,
        tool_dispatch=tool_dispatch, max_tokens=max_tokens, effort=effort,
    )
    return {"final": None, "text": out.get("text", ""), "messages": [], **out}
