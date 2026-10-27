"""Gemini Omni Flash (video-to-video) wrapper for the Rebrand experiment.

Omni is reached through the Interactions API (`client.aio.interactions`), which
google-genai 2.2.0 already ships.

WHERE OMNI RUNS: **Vertex** (ADC, the project's own billing) and/or the Gemini
API (`GEMINI_API_KEY`). Vertex is tried first — it needs no extra key.

THE MODEL ID DIFFERS PER PROVIDER. Vertex only knows the PREVIEW names; the bare
`gemini-omni-1.1-flash` is the Gemini-API name and Vertex answers 400
"Unsupported model interaction" for it. Probed against the live Vertex endpoint
on 2026-09-20 (project at location `global`, requests with no `input` so nothing
was generated — "Missing input." means the rest of the body was accepted):

    gemini-omni-1.1-flash-preview   -> accepted (Vertex default here)
    gemini-omni-flash-preview       -> accepted
    gemini-omni-flash               -> accepted
    gemini-omni-1.1-flash           -> 400 Unsupported model interaction
    gemini-2.5-flash                -> 400 Unsupported (interactions = video models)

Vertex endpoint: POST https://aiplatform.googleapis.com/v1beta1/projects/<p>/
locations/global/interactions (v1 is 404; `global` only — no regional host).

Body contract, all probed on Vertex:
  - snake_case ONLY. `aspectRatio` -> 400 "Did you mean 'aspect_ratio'?",
    `responseFormat`/`generationConfig` -> 400 "Unknown parameter".
  - response_format.delivery: 'inline' | 'uri' ('base64', the name in the
    ai.google.dev docs, is REJECTED on Vertex). `gcs_uri` is accepted with uri.
  - generation_config.video_config.task is a validated enum (text_to_video,
    image_to_video, reference_to_video, edit, extend).
  - background / store / system_instruction are accepted top-level fields.

Request facts (ai.google.dev/gemini-api/docs/omni):
  - input: a list of {"type": "video"|"image"|"text", ...} parts
  - generation_config.video_config.task: text_to_video | image_to_video |
    reference_to_video | edit | extend   (optional — inferred when omitted)
  - response_format: {"type": "video", "aspect_ratio": "9:16"|"16:9",
    "resolution": "360p"|"720p"|"1080p"|"4k", "duration": "3s".."10s",
    "delivery": "base64"(default, <~4 MB)|"uri", "gcs_uri": (Vertex + uri)}
    — SNAKE_CASE on the wire. The 2.2.0 SDK's typed param camelCases
    aspect_ratio/gcs_uri and the API 400s, so it is sent via extra_body.
  - Vertex serves it at location `global` (it validated our params there).
  - input video must be ≤10 s; the output carries generated audio, but Omni
    does not edit voice — the pipeline re-muxes the original audio.
  - output: interaction.output_video.{data|uri} (newer SDKs) or, in 2.2.0,
    steps[model_output].content[type=video] — both are handled by walking the
    dumped payload for the first video block.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
import secrets
from pathlib import Path
from typing import Any, Optional

from ..config import config

log = logging.getLogger("archivist.rebrand.omni")

OMNI_MODEL = os.getenv("OMNI_MODEL", "gemini-omni-1.1-flash")           # Gemini API
OMNI_MODEL_VERTEX = os.getenv("OMNI_MODEL_VERTEX", "gemini-omni-1.1-flash-preview")


def model_for(provider: str) -> str:
    """Vertex publishes Omni under the preview id; the Gemini API uses the
    stable one. Override either with OMNI_MODEL / OMNI_MODEL_VERTEX."""
    return OMNI_MODEL_VERTEX if provider == "vertex" else OMNI_MODEL
OMNI_PROVIDER = os.getenv("OMNI_PROVIDER", "auto").strip().lower()
OMNI_VERTEX_LOCATION = os.getenv("OMNI_VERTEX_LOCATION", "global")
OMNI_RESOLUTION = os.getenv("OMNI_RESOLUTION", "720p")
# "" = the API default (inline base64, falls back to uri when refused) | "uri".
OMNI_DELIVERY = os.getenv("OMNI_DELIVERY", "").strip().lower()
OMNI_TIMEOUT_SEC = float(os.getenv("OMNI_TIMEOUT_SEC", "900"))
OMNI_POLL_SEC = 8.0
NOT_CONFIGURED_MSG = (
    "Omni has no provider on this server. Set GOOGLE_CLOUD_PROJECT (Vertex, billing on, "
    "Vertex AI API enabled) or a paid-tier GEMINI_API_KEY in backend/.env, then restart "
    "the backend — or turn Omni off and run Veo on its own."
)
# Inline base64 is the simplest transport (+33% on the wire, so keep the raw
# file well under the ~20 MB request cap); beyond this the video goes through
# the Files API (API key) or our GCS bucket (Vertex).
INLINE_MAX_BYTES = int(os.getenv("OMNI_INLINE_MAX_BYTES", str(8 * 1024 * 1024)))
# Cap concurrent Omni generations per process (default 2) — each is a heavy
# video job and bursts are what trip per-minute quotas.
_SEM = asyncio.Semaphore(max(1, int(os.getenv("OMNI_MAX_CONCURRENT", "2"))))

_preferred: Optional[str] = None
_api_key_client: Any = None
# Flipped the first time Vertex says it cannot serve this model, so the other
# arm of a run does not repeat a call that cannot work.
_vertex_unsupported = False


class OmniError(RuntimeError):
    """Omni ran but did not return a usable video."""


class OmniUnavailable(OmniError):
    """No provider can serve Omni (not configured, or model not enabled)."""


def _api_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def providers() -> list[str]:
    """Providers worth trying, in order: Vertex first (no extra key, the
    project's own billing), then a Gemini API key. A provider that has already
    answered "unsupported model" this process is dropped."""
    have_vertex = bool(config.google_cloud_project) and not _vertex_unsupported
    have_key = bool(_api_key())
    if OMNI_PROVIDER == "vertex":
        return ["vertex"] if have_vertex else []
    if OMNI_PROVIDER in ("api_key", "gemini", "apikey"):
        return ["api_key"] if have_key else []
    order = [p for p, ok in (("vertex", have_vertex), ("api_key", have_key)) if ok]
    if _preferred in order:
        order.remove(_preferred)
        order.insert(0, _preferred)
    return order


def status() -> dict:
    """What the UI shows next to the Omni arm before a run."""
    order = providers()
    return {
        "model": model_for(order[0]) if order else OMNI_MODEL,
        "providers": order,
        "preferred": _preferred,
        "configured": bool(order),
        "needs_api_key": not bool(_api_key()),
        "note": None if order else NOT_CONFIGURED_MSG,
    }


def _client(provider: str):
    global _api_key_client
    if provider == "vertex":
        from .vertex_client import get_client

        return get_client(OMNI_VERTEX_LOCATION)
    if _api_key_client is None:
        from google import genai

        # vertexai=False explicitly: .env sets GOOGLE_GENAI_USE_VERTEXAI=True,
        # which would otherwise flip this key client into Vertex mode.
        _api_key_client = genai.Client(vertexai=False, api_key=_api_key())
    return _api_key_client


def _looks_unavailable(exc: BaseException) -> bool:
    """Errors that mean "this provider cannot serve Omni at all" (so the next
    provider should be tried) — as opposed to a bad request or a content block,
    which would fail identically everywhere."""
    msg = str(exc).lower()
    markers = (
        "404", "not found", "not_found", "permission_denied", "403",
        "is not supported", "not supported", "unsupported", "not enabled",
        "has not been used", "unknown model", "does not have access",
        "not available", "billing",
    )
    return any(m in msg for m in markers)


# ---------------------------------------------------------------------------
# Input parts
# ---------------------------------------------------------------------------

def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def image_part(path: Path) -> dict:
    mime, _ = mimetypes.guess_type(str(path))
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        mime = "image/png"
    return {"type": "image", "data": _b64(path), "mime_type": mime}


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


async def _upload_video_gcs(path: Path) -> str:
    """Vertex path for a video too big to inline: stage it in the Veo bucket."""
    bucket_name = config.veo_output_bucket
    if not bucket_name:
        raise OmniError("video too large to inline and VEO_OUTPUT_BUCKET is not set")
    key = f"rebrand/omni-inputs/{secrets.token_hex(6)}-{path.name}"

    def _sync() -> str:
        from google.cloud import storage

        client = storage.Client(project=config.google_cloud_project)
        client.bucket(bucket_name).blob(key).upload_from_filename(
            str(path), content_type="video/mp4",
        )
        return f"gs://{bucket_name}/{key}"

    return await asyncio.to_thread(_sync)


async def _upload_video_files_api(client: Any, path: Path) -> str:
    """API-key path: Files API upload, then wait for ACTIVE."""
    f = await client.aio.files.upload(file=str(path), config={"mime_type": "video/mp4"})
    for _ in range(60):
        state = str(getattr(getattr(f, "state", None), "name", getattr(f, "state", "")) or "")
        if "ACTIVE" in state.upper():
            return f.uri
        if "FAILED" in state.upper():
            raise OmniError(f"Files API rejected {path.name}")
        await asyncio.sleep(2)
        f = await client.aio.files.get(name=f.name)
    raise OmniError(f"Files API never activated {path.name}")


async def _video_part(provider: str, client: Any, path: Path) -> dict:
    if path.stat().st_size <= INLINE_MAX_BYTES:
        return {"type": "video", "data": _b64(path), "mime_type": "video/mp4"}
    if provider == "vertex":
        uri = await _upload_video_gcs(path)
    else:
        uri = await _upload_video_files_api(client, path)
    return {"type": "video", "uri": uri, "mime_type": "video/mp4"}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    return obj


def _find_video(node: Any) -> Optional[dict]:
    """Depth-first search for the first {"type": "video", data|uri} block.
    Walks backwards through lists so the model's LAST output wins over any
    echoed input video."""
    if isinstance(node, dict):
        if node.get("type") == "video" and (node.get("data") or node.get("uri")):
            return node
        for key in ("output_video", "steps", "outputs", "content"):
            if key in node:
                hit = _find_video(node[key])
                if hit:
                    return hit
        return None
    if isinstance(node, list):
        for item in reversed(node):
            hit = _find_video(item)
            if hit:
                return hit
    return None


def _failure_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:400]
    for key in ("error", "status_details", "failure_reason", "incomplete_details"):
        if payload.get(key):
            return str(payload[key])[:400]
    texts: list[str] = []

    def _walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("type") == "text" and n.get("text"):
                texts.append(str(n["text"]))
            for v in n.values():
                _walk(v)
        elif isinstance(n, list):
            for v in n:
                _walk(v)

    _walk(payload.get("steps") or payload.get("outputs"))
    return (" ".join(texts) or f"status={payload.get('status')}")[:400]


async def _download(provider: str, client: Any, uri: str) -> bytes:
    if uri.startswith("gs://"):
        import tempfile

        from .vertex_client import download_gcs

        tmp = Path(tempfile.mkdtemp()) / "omni.mp4"
        await download_gcs(uri, tmp)
        return tmp.read_bytes()
    import httpx

    headers = {"x-goog-api-key": _api_key()} if provider == "api_key" else {}
    if provider == "api_key" and "alt=media" not in uri:
        uri += ("&" if "?" in uri else "?") + "alt=media"
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as http:
        r = await http.get(uri, headers=headers)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------
# Core call
# ---------------------------------------------------------------------------

# Fields the API refuses for a given task. An `edit` (and `extend`) inherits its
# shape from the SOURCE video, so asking for one is an error:
#   400 "Aspect ratio cannot be set in response format for edit task."
_SHAPE_FIELDS = ("aspect_ratio", "resolution", "duration")


def _response_format(
    provider: str, aspect: str, duration_sec: Optional[int], *, uri_delivery: bool,
    task: Optional[str] = None, drop: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The wire shape, snake_case exactly as the live API wants it.

    google-genai 2.2.0 types this block with camelCase aliases
    (`aspect_ratio` → `aspectRatio`, `gcs_uri` → `gcsUri`) that the API rejects
    with 400 "Unknown parameter 'response_format.aspectRatio'". So it is NEVER
    passed as the typed `response_format=` kwarg: it rides in `extra_body`,
    which the client merges into the JSON after its alias transform, i.e. it
    goes on the wire verbatim."""
    source_shaped = task in ("edit", "extend")
    rf: dict[str, Any] = {"type": "video"}
    if not source_shaped:
        rf["aspect_ratio"] = aspect
        if OMNI_RESOLUTION:
            rf["resolution"] = OMNI_RESOLUTION
        if duration_sec:
            rf["duration"] = f"{max(3, min(10, int(duration_sec)))}s"
    for f in drop:
        rf.pop(f, None)
    if uri_delivery:
        rf["delivery"] = "uri"
        if provider == "vertex":
            if not config.veo_output_bucket:
                raise OmniError("uri delivery on Vertex needs VEO_OUTPUT_BUCKET for gcs_uri")
            rf["gcs_uri"] = (
                f"gs://{config.veo_output_bucket}/rebrand/omni-out/{secrets.token_hex(6)}/"
            )
    return rf


def _rejected_field(exc: BaseException) -> Optional[str]:
    """The response_format field a 400 says cannot be set (e.g. "Aspect ratio
    cannot be set in response format for edit task." → "aspect_ratio"), so the
    call can be retried without it instead of failing the run."""
    m = re.search(r"([A-Za-z ]+?) cannot be set in response format", str(exc))
    if not m:
        return None
    field = m.group(1).strip().lower().replace(" ", "_")
    return field if field in _SHAPE_FIELDS else None


def _wants_uri_delivery(exc: BaseException) -> bool:
    """Inline (base64) output is capped (~4 MB per the docs); a bigger video
    comes back as an error that names delivery/size — retry with uri."""
    msg = str(exc).lower()
    return any(m in msg for m in (
        "delivery", "too large", "exceeds", "4mb", "4 mb", "payload size", "response size",
    ))


async def _generate_once(
    provider: str,
    *,
    parts_builder,
    task: Optional[str],
    aspect: str,
    duration_sec: Optional[int],
) -> bytes:
    client = _client(provider)
    parts = await parts_builder(provider, client)
    uri_delivery = OMNI_DELIVERY == "uri"
    drop: tuple[str, ...] = ()
    for attempt in range(4):
        body: dict[str, Any] = {
            "response_format": _response_format(
                provider, aspect, duration_sec, uri_delivery=uri_delivery,
                task=task, drop=drop,
            ),
        }
        if task:
            body["generation_config"] = {"video_config": {"task": task}}
        try:
            interaction = await client.aio.interactions.create(
                model=model_for(provider),
                input=parts,
                extra_body=body,
                timeout=OMNI_TIMEOUT_SEC,
            )
            break
        except Exception as e:  # noqa: BLE001
            field = _rejected_field(e)
            if field and field not in drop:
                log.warning("[omni] %s rejected for task=%s, retrying without it", field, task)
                drop = drop + (field,)
                continue
            if not uri_delivery and _wants_uri_delivery(e):
                log.warning("[omni] inline delivery refused, retrying with uri: %s", e)
                uri_delivery = True
                continue
            raise
    payload = _dump(interaction)
    status = str((payload or {}).get("status") or "") if isinstance(payload, dict) else ""

    # Synchronous by default; if the service answers with a still-running
    # interaction, poll it to a terminal state.
    waited = 0.0
    while status in ("in_progress", "requires_action") and waited < OMNI_TIMEOUT_SEC:
        await asyncio.sleep(OMNI_POLL_SEC)
        waited += OMNI_POLL_SEC
        interaction = await client.aio.interactions.get(payload["id"])
        payload = _dump(interaction)
        status = str(payload.get("status") or "")
    if status in ("in_progress", "requires_action"):
        raise OmniError(f"Omni did not finish within {int(OMNI_TIMEOUT_SEC)}s")
    if status in ("failed", "cancelled", "incomplete"):
        raise OmniError(f"Omni {status}: {_failure_text(payload)}")

    video = _find_video(payload)
    if not video:
        raise OmniError(f"Omni returned no video ({_failure_text(payload)})")
    if video.get("data"):
        return base64.b64decode(video["data"])
    return await _download(provider, client, str(video["uri"]))


async def _generate(
    *,
    parts_builder,
    task: Optional[str],
    aspect: str,
    duration_sec: Optional[int],
    out: Path,
) -> dict:
    global _preferred
    order = providers()
    if not order:
        raise OmniUnavailable(NOT_CONFIGURED_MSG)
    errors: list[str] = []
    async with _SEM:
        for provider in order:
            try:
                data = await _generate_once(
                    provider, parts_builder=parts_builder, task=task,
                    aspect=aspect, duration_sec=duration_sec,
                )
            except OmniError:
                raise
            except Exception as e:  # noqa: BLE001
                errors.append(f"{provider}: {str(e)[:300]}")
                if provider == "vertex" and _looks_unavailable(e):
                    globals()["_vertex_unsupported"] = True
                    log.warning("[omni] Vertex cannot serve %s — not retrying it this process",
                                model_for("vertex"))
                if _looks_unavailable(e) and provider != order[-1]:
                    log.warning("[omni] %s cannot serve %s, trying next provider: %s",
                                provider, OMNI_MODEL, e)
                    continue
                if _looks_unavailable(e):
                    detail = "; ".join(errors)
                    raise OmniUnavailable(
                        f"{NOT_CONFIGURED_MSG} (provider said: {detail})"
                        if not _api_key() else detail
                    ) from e
                raise OmniError("; ".join(errors)) from e
            if not data:
                raise OmniError("Omni returned an empty video")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            _preferred = provider
            log.info("[omni] %s via %s (%s) → %s (%d bytes)", task or "auto", provider,
                 model_for(provider), out.name, len(data))
            return {"provider": provider, "model": model_for(provider), "path": out}
    raise OmniUnavailable("; ".join(errors) or "no Omni provider answered")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def edit_video(
    video: Path,
    prompt: str,
    *,
    aspect: str,
    out: Path,
    images: list[Path] | None = None,
) -> dict:
    """Edit a ≤10 s clip. `images` (e.g. the logo) ride along as references,
    each introduced by the prompt text that precedes them."""
    imgs = [p for p in (images or []) if p and p.exists()]

    async def _parts(provider: str, client: Any) -> list[dict]:
        parts = [await _video_part(provider, client, video)]
        for p in imgs:
            parts.append(image_part(p))
        parts.append(text_part(prompt))
        return parts

    return await _generate(
        parts_builder=_parts, task="edit", aspect=aspect, duration_sec=None, out=out,
    )


async def interpolate(
    first: Path,
    last: Path,
    prompt: str,
    *,
    aspect: str,
    duration_sec: int,
    out: Path,
) -> dict:
    """First/last-frame generation: two images + text → a clip that starts on
    `first` and lands on `last` (the documented Omni interpolation shape)."""

    async def _parts(provider: str, client: Any) -> list[dict]:
        return [image_part(first), image_part(last), text_part(prompt)]

    return await _generate(
        parts_builder=_parts, task="image_to_video", aspect=aspect,
        duration_sec=duration_sec, out=out,
    )
