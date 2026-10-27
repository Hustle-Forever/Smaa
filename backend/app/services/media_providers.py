"""External media providers — voice change + music — behind a clean interface.

Three providers, two factories:

  Voice (``get_voice_provider()``)
    * ``ElevenLabsVoiceProvider`` — ElevenLabs **speech-to-speech** REST call.
      Takes a recorded narration file, returns a converted file in a different
      voice that PRESERVES the original timing/prosody (so downstream
      word-level subtitle timings stay valid). Needs ``ELEVENLABS_API_KEY``.

  Music (``get_music_provider(mode)``)
    * ``SunoMusicProvider`` — Suno-style generative music adapter. Submits a
      generation job and returns a *pending* handle for the orchestrator's
      poller to resolve + import. Needs ``SUNO_API_KEY`` (and optionally
      ``SUNO_API_BASE`` to point at your vendor's gateway).
    * ``LibraryMusicProvider`` — no keys needed; embeds the style brief
      (gemini-embedding-001, 768d) and picks the closest enabled
      ``producer_assets`` row with ``asset_kind='audio'`` by cosine distance
      (same ranking as ``POST /api/producer-assets/search``).

``MusicProvider.make_music`` returns a **MusicResult** dict — one of:

    {"kind": "asset",   "asset_id": int}          # library pick — usable today
    {"kind": "upload",  "upload_id": int}         # generated + already imported
    {"kind": "pending", "provider_job_id": str}   # submit-then-poll (Suno)

Design rules:
  * Missing API keys NEVER crash the process — ``available()`` returns False
    and the action methods raise ``ProviderError`` with a human message the
    UI can show verbatim.
  * Self-contained: stdlib + httpx + ``..db`` + ``.vertex_client`` only.
  * Suno HTTP specifics follow the official sunoapi.org contract
    (``POST /api/v1/generate`` + ``GET /api/v1/generate/record-info``);
    they are isolated in small private methods.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from ..db import get_conn
from .vertex_client import gemini_embed

log = logging.getLogger("archivist.media_providers")


class ProviderError(Exception):
    """Human-readable provider failure (missing key, API error, empty library).

    Raised instead of letting raw httpx/DB exceptions escape so callers can
    surface ``str(exc)`` directly in the UI.
    """


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class VoiceProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    async def speech_to_speech(
        self, audio: Path, *, voice_id: str | None = None, out_dir: Path
    ) -> Path: ...


@runtime_checkable
class MusicProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    async def make_music(self, *, style_brief: str, duration_sec: float) -> dict: ...


# ---------------------------------------------------------------------------
# Voice — ElevenLabs speech-to-speech
# ---------------------------------------------------------------------------

ELEVENLABS_API_BASE = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io").rstrip("/")
# "Adam" — a stock ElevenLabs premade voice available to every account.
ELEVENLABS_DEFAULT_VOICE_ID = os.getenv("ELEVENLABS_DEFAULT_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
# STS model that preserves source timing/prosody across languages.
ELEVENLABS_STS_MODEL = os.getenv("ELEVENLABS_STS_MODEL", "eleven_multilingual_sts_v2")
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"


class ElevenLabsVoiceProvider:
    """ElevenLabs `POST /v1/speech-to-speech/{voice_id}` (multipart).

    The converted audio keeps the source clip's pacing and emphasis — only the
    voice identity changes — so existing word timestamps stay aligned.
    """

    name = "elevenlabs"

    @staticmethod
    def _api_key() -> str | None:
        return (os.getenv("ELEVENLABS_API_KEY") or "").strip() or None

    def available(self) -> bool:
        return self._api_key() is not None

    async def speech_to_speech(
        self, audio: Path, *, voice_id: str | None = None, out_dir: Path
    ) -> Path:
        key = self._api_key()
        if not key:
            raise ProviderError(
                "Voice changer needs ELEVENLABS_API_KEY — add it to backend/.env and retry."
            )
        audio = Path(audio)
        if not audio.is_file():
            raise ProviderError(f"Voice changer: source audio not found: {audio}")

        vid = (voice_id or "").strip() or ELEVENLABS_DEFAULT_VOICE_ID
        url = (
            f"{ELEVENLABS_API_BASE}/v1/speech-to-speech/{vid}"
            f"?output_format={ELEVENLABS_OUTPUT_FORMAT}"
        )
        files = {"audio": (audio.name, audio.read_bytes(), "application/octet-stream")}
        data = {
            "model_id": ELEVENLABS_STS_MODEL,
            # Keep the source clip's style/pacing — that is the whole point of
            # STS for us (subtitle timings must survive the voice swap).
            "voice_settings": json.dumps(
                {"stability": 0.5, "similarity_boost": 0.8, "use_speaker_boost": True}
            ),
        }

        log.info("[media] elevenlabs sts voice=%s src=%s", vid, audio.name)
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                r = await client.post(url, headers={"xi-api-key": key}, data=data, files=files)
        except httpx.HTTPError as e:
            log.exception("[media] elevenlabs sts request failed")
            raise ProviderError(f"Voice changer (ElevenLabs) request failed: {e}") from e

        if r.status_code >= 300:
            detail = r.text[:300]
            log.warning("[media] elevenlabs sts %s: %s", r.status_code, detail)
            if r.status_code in (401, 403):
                raise ProviderError(
                    "Voice changer: ElevenLabs rejected the API key (401/403) — "
                    "check ELEVENLABS_API_KEY."
                )
            raise ProviderError(f"Voice changer (ElevenLabs) error {r.status_code}: {detail}")

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{audio.stem}_voiced_{secrets.token_hex(4)}.mp3"
        out_path.write_bytes(r.content)
        log.info("[media] elevenlabs sts done → %s (%d bytes)", out_path.name, len(r.content))
        return out_path

    # -- Instant voice cloning (the editor's single app-wide "my voice") -----

    async def clone_voice(self, *, name: str, samples: list[tuple[str, bytes]]) -> str:
        """`POST /v1/voices/add` (multipart) — create an instant voice clone
        from one or more audio samples. Returns the new ElevenLabs voice_id.

        Multipart contract: form field ``name`` + one repeated ``files`` part
        per sample (filename, bytes, audio/mpeg).
        """
        key = self._api_key()
        if not key:
            raise ProviderError(
                "Voice cloning needs ELEVENLABS_API_KEY — add it to backend/.env and retry."
            )
        if not samples:
            raise ProviderError("Voice cloning needs at least one audio sample.")

        url = f"{ELEVENLABS_API_BASE}/v1/voices/add"
        files = [
            ("files", (fn or "sample.mp3", data, "audio/mpeg"))
            for fn, data in samples
        ]
        log.info("[media] elevenlabs clone name=%r samples=%d", name, len(samples))
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                r = await client.post(
                    url, headers={"xi-api-key": key}, data={"name": name}, files=files
                )
        except httpx.HTTPError as e:
            log.exception("[media] elevenlabs clone request failed")
            raise ProviderError(f"Voice cloning (ElevenLabs) request failed: {e}") from e

        if r.status_code >= 300:
            detail = r.text[:300]
            log.warning("[media] elevenlabs clone %s: %s", r.status_code, detail)
            low = detail.lower()
            if "voice_limit_reached" in low:
                raise ProviderError(
                    "ElevenLabs voice slots are full — remove an old voice and retry."
                )
            if "can_not_use_instant_voice_cloning" in low:
                raise ProviderError("Voice cloning needs a paid ElevenLabs plan.")
            if r.status_code in (401, 403):
                raise ProviderError(
                    "Voice cloning: ElevenLabs rejected the API key (401/403) — "
                    "check ELEVENLABS_API_KEY."
                )
            raise ProviderError(f"Voice cloning (ElevenLabs) error {r.status_code}: {detail}")

        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        body = parsed if isinstance(parsed, dict) else {}
        voice_id = str(body.get("voice_id") or "").strip()
        if not voice_id:
            raise ProviderError(
                f"Voice cloning (ElevenLabs): no voice_id in the response: {r.text[:200]}"
            )
        log.info("[media] elevenlabs clone done → voice_id=%s", voice_id)
        return voice_id

    async def delete_voice(self, voice_id: str) -> None:
        """`DELETE /v1/voices/{id}` — free the slot. An already-gone voice
        (404 / voice_not_found) is treated as success."""
        key = self._api_key()
        if not key:
            raise ProviderError(
                "Voice cloning needs ELEVENLABS_API_KEY — add it to backend/.env and retry."
            )
        vid = (voice_id or "").strip()
        if not vid:
            return
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.delete(
                    f"{ELEVENLABS_API_BASE}/v1/voices/{vid}", headers={"xi-api-key": key}
                )
        except httpx.HTTPError as e:
            log.exception("[media] elevenlabs delete-voice request failed")
            raise ProviderError(f"Voice delete (ElevenLabs) request failed: {e}") from e
        if (
            r.status_code < 300
            or r.status_code == 404
            or "voice_not_found" in r.text[:300].lower()
        ):
            log.info("[media] elevenlabs voice %s deleted (HTTP %s)", vid, r.status_code)
            return
        if r.status_code in (401, 403):
            raise ProviderError(
                "Voice delete: ElevenLabs rejected the API key (401/403) — "
                "check ELEVENLABS_API_KEY."
            )
        raise ProviderError(
            f"Voice delete (ElevenLabs) error {r.status_code}: {r.text[:300]}"
        )

    async def voice_exists(self, voice_id: str) -> bool:
        """`GET /v1/voices/{id}` → 200 means the voice is still there."""
        key = self._api_key()
        if not key:
            raise ProviderError(
                "Voice cloning needs ELEVENLABS_API_KEY — add it to backend/.env and retry."
            )
        vid = (voice_id or "").strip()
        if not vid:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    f"{ELEVENLABS_API_BASE}/v1/voices/{vid}", headers={"xi-api-key": key}
                )
        except httpx.HTTPError as e:
            raise ProviderError(f"Voice lookup (ElevenLabs) request failed: {e}") from e
        return r.status_code == 200

    async def generate_sfx(self, *, prompt: str, duration_seconds: float = 0.9) -> bytes:
        """`POST /v1/sound-generation` — text-to-sound-effect. Synchronous:
        returns the mp3 bytes directly (no polling). `prompt` is a short
        description like 'quick swish transition' or 'soft bubble pop';
        `duration_seconds` is clamped to ElevenLabs' 0.5-22s range. Raises
        ProviderError if the key is missing or the API rejects the request."""
        key = self._api_key()
        if not key:
            raise ProviderError(
                "Sound effects need ELEVENLABS_API_KEY — add it to backend/.env and retry."
            )
        text = (prompt or "").strip()
        if not text:
            raise ProviderError("Sound effect: empty prompt.")
        dur = max(0.5, min(22.0, float(duration_seconds or 0.9)))
        url = f"{ELEVENLABS_API_BASE}/v1/sound-generation?output_format={ELEVENLABS_OUTPUT_FORMAT}"
        body = {"text": text, "duration_seconds": dur, "prompt_influence": 0.3}
        log.info("[media] elevenlabs sfx prompt=%r dur=%.2fs", text[:60], dur)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(url, headers={"xi-api-key": key}, json=body)
        except httpx.HTTPError as e:
            log.exception("[media] elevenlabs sfx request failed")
            raise ProviderError(f"Sound effect (ElevenLabs) request failed: {e}") from e
        if r.status_code >= 300:
            detail = r.text[:300]
            log.warning("[media] elevenlabs sfx %s: %s", r.status_code, detail)
            if r.status_code in (401, 403):
                raise ProviderError(
                    "Sound effect: ElevenLabs rejected the API key (401/403) — check ELEVENLABS_API_KEY."
                )
            raise ProviderError(f"Sound effect (ElevenLabs) error {r.status_code}: {detail}")
        if not r.content:
            raise ProviderError("Sound effect (ElevenLabs): empty audio response.")
        log.info("[media] elevenlabs sfx done (%d bytes)", len(r.content))
        return r.content


def get_voice_provider() -> VoiceProvider:
    """The configured voice changer (currently always ElevenLabs)."""
    return ElevenLabsVoiceProvider()


# ---------------------------------------------------------------------------
# Music — Suno-style generation (submit-then-poll)
# ---------------------------------------------------------------------------

SUNO_API_BASE_DEFAULT = "https://api.sunoapi.org"
SUNO_MODEL_DEFAULT = "V4_5"
SUNO_CALLBACK_URL_DEFAULT = "https://example.com/suno-callback"

# record-info `data.status` stages (sunoapi.org). Anything in PENDING is still
# cooking; SUCCESS carries the track URLs; the rest are terminal failures
# (CALLBACK_EXCEPTION is special-cased: done if a URL is already present).
_SUNO_PENDING_STAGES = {"PENDING", "TEXT_SUCCESS", "FIRST_SUCCESS"}
_SUNO_SUCCESS_STAGES = {"SUCCESS", "COMPLETE", "COMPLETED", "DONE"}


class SunoMusicProvider:
    """Adapter for the sunoapi.org music-generation API.

    Submit-then-poll model: ``make_music`` returns
    ``{"kind": "pending", "provider_job_id": ...}`` and the orchestrator's
    poller calls :meth:`check_job` until an audio URL appears, then downloads
    + imports it (this module deliberately does NOT import audio).

    Contract (docs.sunoapi.org):
      * submit — ``POST {base}/api/v1/generate`` (Bearer auth, JSON body,
        customMode+instrumental ⇒ style+title required, prompt not needed);
        task id at ``data.taskId``.
      * poll — ``GET {base}/api/v1/generate/record-info?taskId=...``;
        stage at ``data.status``, tracks at ``data.response.sunoData[]``.
      * Both wrap results as ``{"code": int, "msg": str, "data": {...}}`` —
        ``code != 200`` is a failure even on HTTP 200.
    """

    name = "suno"

    # Set after the first record-info body is logged at INFO (so live testing
    # can confirm the vendor's exact field names without log spam).
    _logged_first_poll_body = False

    @staticmethod
    def _api_key() -> str | None:
        return (os.getenv("SUNO_API_KEY") or "").strip() or None

    @staticmethod
    def _api_base() -> str:
        return (os.getenv("SUNO_API_BASE") or SUNO_API_BASE_DEFAULT).rstrip("/")

    @staticmethod
    def _model() -> str:
        return (os.getenv("SUNO_MODEL") or SUNO_MODEL_DEFAULT).strip()

    @staticmethod
    def _callback_url() -> str:
        return (os.getenv("SUNO_CALLBACK_URL") or SUNO_CALLBACK_URL_DEFAULT).strip()

    def available(self) -> bool:
        return self._api_key() is not None

    async def make_music(self, *, style_brief: str, duration_sec: float) -> dict:
        key = self._api_key()
        if not key:
            raise ProviderError(
                "Music generation needs SUNO_API_KEY — add it to backend/.env, "
                "or use the library picker instead (mode='library')."
            )
        brief = (style_brief or "").strip()
        if not brief:
            raise ProviderError("Music generation needs a style brief (got empty text).")

        job_id = await self._submit_generation(key, brief, duration_sec)
        log.info("[media] suno submitted job=%s (%.0fs, brief=%r)", job_id, duration_sec, brief[:80])
        return {"kind": "pending", "provider_job_id": job_id}

    # -- HTTP specifics (sunoapi.org) ----------------------------------------

    @staticmethod
    def _map_code_error(code: int, msg: str) -> str:
        """Human message for a sunoapi.org business ``code`` (!= 200)."""
        if code in (401, 403):
            return "Suno rejected the API key — check SUNO_API_KEY."
        if code in (405, 413):
            return f"Suno rejected the request parameters: {msg}"
        if code == 429:
            return "Suno: insufficient credits — top up at sunoapi.org."
        if code == 430:
            return "Suno: rate limited — try again shortly."
        if code == 455:
            return "Suno is under maintenance — try again later."
        return f"Suno error {code}: {msg}"

    @staticmethod
    def _body_code(body: dict) -> int | None:
        """The vendor's business code (``body.code``), or None if absent/odd."""
        raw = body.get("code")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _title_from_brief(brief: str) -> str:
        """First clause/sentence of the brief, <=70 chars (Suno title cap=80)."""
        first = re.split(r"[.!?;\n—]", brief, maxsplit=1)[0]
        return first.strip()[:70].strip() or "AI music bed"

    async def _submit_generation(self, key: str, brief: str, duration_sec: float) -> str:
        """``POST /api/v1/generate`` (custom-mode instrumental); returns
        ``data.taskId``.

        ``duration_sec`` is accepted for interface symmetry but sunoapi.org
        has no duration parameter — the orchestrator trims/loops the track.
        """
        url = f"{self._api_base()}/api/v1/generate"
        payload = {
            # customMode + instrumental ⇒ style + title required, prompt unused.
            "customMode": True,
            "instrumental": True,
            "model": self._model(),
            "style": brief[:900],
            "title": self._title_from_brief(brief),
            # Required by the API even when we poll instead of listening.
            "callBackUrl": self._callback_url(),
            "negativeTags": "vocals, singing, rap, spoken word",
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    url, json=payload, headers={"Authorization": f"Bearer {key}"}
                )
        except httpx.HTTPError as e:
            log.exception("[media] suno submit failed")
            raise ProviderError(f"Music generation (Suno) request failed: {e}") from e

        if r.status_code >= 300:
            detail = r.text[:300]
            log.warning("[media] suno submit HTTP %s: %s", r.status_code, detail)
            raise ProviderError(self._map_code_error(r.status_code, detail))

        body: dict[str, Any] = {}
        try:
            parsed = r.json()
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            pass

        # sunoapi.org signals failure via body.code even on HTTP 200.
        code = self._body_code(body)
        if code is not None and code != 200:
            msg = str(body.get("msg") or "")[:300]
            log.warning("[media] suno submit code=%s msg=%s", code, msg)
            raise ProviderError(self._map_code_error(code, msg))

        data = body.get("data") if isinstance(body.get("data"), dict) else body
        job_id = data.get("taskId") or data.get("task_id") or data.get("id")
        if not job_id:
            raise ProviderError(
                f"Music generation (Suno): could not find a task id in the response: {str(body)[:300]}"
            )
        return str(job_id)

    @staticmethod
    def _extract_audio_urls(data: dict) -> list[str]:
        """All downloadable mirror URLs from a record-info ``data`` dict, in
        priority order. sunoapi returns several per track: ``sourceAudioUrl``
        (cdn1.suno.ai — Suno's own CDN, most reliable) first, then ``audioUrl``
        (a gateway tempfile host that some networks can't reach), then the
        remaining variants. The poller tries each until one downloads."""
        resp = data.get("response") if isinstance(data.get("response"), dict) else {}
        tracks = resp.get("sunoData") or data.get("sunoData") or []
        out: list[str] = []
        if isinstance(tracks, list) and tracks and isinstance(tracks[0], dict):
            t0 = tracks[0]
            for field in ("sourceAudioUrl", "audioUrl", "audio_url",
                          "sourceStreamAudioUrl", "streamAudioUrl"):
                u = t0.get(field)
                if u and str(u) not in out:
                    out.append(str(u))
        for field in ("audioUrl", "audio_url"):  # last-resort: url on data itself
            u = data.get(field)
            if u and str(u) not in out:
                out.append(str(u))
        return out

    async def check_job(self, provider_job_id: str) -> dict:
        """Poll a pending generation (orchestrator/poller helper, not part of
        the MusicProvider protocol).

        Returns ``{"status": "pending"|"done"|"failed",
                   "audio_url": str|None, "error": str|None}``.
        Raises :class:`ProviderError` only for *transient* problems (network,
        rate limit, maintenance) — the poller retries those next cycle;
        definitive failures come back as ``status="failed"``.
        """
        key = self._api_key()
        if not key:
            raise ProviderError("Music generation needs SUNO_API_KEY to poll job status.")
        body = await self._fetch_job(key, provider_job_id)

        # Log the first raw poll body once so live testing can confirm the
        # vendor's exact field names/casing (status stages, sunoData, URLs).
        if not SunoMusicProvider._logged_first_poll_body:
            SunoMusicProvider._logged_first_poll_body = True
            log.info("[media] suno first record-info raw body: %s",
                     json.dumps(body, default=str)[:2000])

        code = self._body_code(body)
        if code is not None and code != 200:
            msg = str(body.get("msg") or "")[:300]
            if code in (430, 455):  # rate limit / maintenance — retryable.
                raise ProviderError(self._map_code_error(code, msg))
            return {"status": "failed", "audio_url": None,
                    "error": self._map_code_error(code, msg)}

        data = body.get("data") if isinstance(body.get("data"), dict) else body
        # Stage field is data.status per the docs; taskStatus kept as a
        # defensive alias in case the gateway names it differently.
        status = str(data.get("status") or data.get("taskStatus") or "").upper()

        if status in _SUNO_SUCCESS_STAGES:
            urls = self._extract_audio_urls(data)
            if not urls:
                return {"status": "failed", "audio_url": None, "audio_urls": [],
                        "error": "Suno reported success but returned no audio URL."}
            return {"status": "done", "audio_url": urls[0],
                    "audio_urls": urls, "error": None}

        if status == "CALLBACK_EXCEPTION":
            # Generation finished but the callback delivery failed — if the
            # track is already there, take it; otherwise treat as failed.
            urls = self._extract_audio_urls(data)
            if urls:
                return {"status": "done", "audio_url": urls[0],
                        "audio_urls": urls, "error": None}
            return {"status": "failed", "audio_url": None, "audio_urls": [],
                    "error": str(data.get("errorMessage") or body.get("msg") or status)}

        if status.endswith("_FAILED") or "ERROR" in status or status == "FAILED":
            # CREATE_TASK_FAILED / GENERATE_AUDIO_FAILED / SENSITIVE_WORD_ERROR / ...
            return {"status": "failed", "audio_url": None,
                    "error": str(data.get("errorMessage") or body.get("msg") or status)}

        # PENDING / TEXT_SUCCESS / FIRST_SUCCESS — and any unknown stage —
        # keep waiting (the poller enforces its own 30-minute timeout).
        return {"status": "pending", "audio_url": None, "error": None}

    async def _fetch_job(self, key: str, provider_job_id: str) -> dict:
        """``GET /api/v1/generate/record-info?taskId=...`` — raw body dict.

        All raises here are :class:`ProviderError`, which the poller treats
        as transient (retry next cycle, 30-minute cap).
        """
        url = f"{self._api_base()}/api/v1/generate/record-info"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    url,
                    params={"taskId": provider_job_id},
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.HTTPError as e:
            log.exception("[media] suno poll failed job=%s", provider_job_id)
            raise ProviderError(f"Music generation (Suno) poll failed: {e}") from e
        if r.status_code >= 300:
            raise ProviderError(
                f"Music generation (Suno) poll error {r.status_code}: {r.text[:300]}"
            )
        try:
            parsed = r.json()
        except ValueError as e:
            raise ProviderError("Music generation (Suno) poll returned non-JSON.") from e
        if not isinstance(parsed, dict):
            raise ProviderError(
                f"Music generation (Suno) poll returned an unexpected shape: {str(parsed)[:200]}"
            )
        return parsed


# ---------------------------------------------------------------------------
# Music — pick from the Assets Library (works today, no keys)
# ---------------------------------------------------------------------------

class LibraryMusicProvider:
    """Semantic pick of the best ``producer_assets`` audio row for a brief.

    Same ranking as the Assets Library search endpoint
    (`routers/producer_assets.py POST /search`): embed the query with
    gemini-embedding-001 (768d) and order by pgvector cosine distance.
    Falls back to the newest enabled audio asset if nothing is embedded yet.
    """

    name = "library"

    def available(self) -> bool:
        return True  # local DB pick — no external keys involved.

    async def make_music(self, *, style_brief: str, duration_sec: float) -> dict:
        brief = (style_brief or "").strip() or "background music"
        query = f"{brief} (background music, about {int(duration_sec)} seconds)"

        qvec: list[float] | None = None
        try:
            vecs = await gemini_embed([query])
            qvec = vecs[0] if vecs else None
        except Exception:  # noqa: BLE001 — degrade to recency pick below.
            log.exception("[media] library music: embed failed, falling back to newest")

        row = None
        if qvec:
            row = await self._top_by_distance(qvec)
        if row is None:
            row = await self._newest_audio()
        if row is None:
            raise ProviderError(
                "No music in the library to pick from — upload audio files in the "
                "Assets Library first (or configure SUNO_API_KEY to generate music)."
            )
        log.info(
            "[media] library music pick asset=%s (%s) for brief=%r",
            row["id"], row.get("display_name"), brief[:80],
        )
        return {"kind": "asset", "asset_id": int(row["id"])}

    @staticmethod
    async def _top_by_distance(qvec: list[float]) -> dict | None:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, display_name, duration_sec,
                           (embedding <=> %s::vector) AS distance
                      FROM producer_assets
                     WHERE embedding IS NOT NULL AND enabled AND asset_kind = 'audio'
                     ORDER BY embedding <=> %s::vector
                     LIMIT 1
                    """,
                    (qvec, qvec),
                )
                return await cur.fetchone()

    @staticmethod
    async def _newest_audio() -> dict | None:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, display_name, duration_sec
                      FROM producer_assets
                     WHERE enabled AND asset_kind = 'audio'
                     ORDER BY created_at DESC
                     LIMIT 1
                    """
                )
                return await cur.fetchone()


def get_music_provider(mode: str = "auto") -> MusicProvider:
    """Pick the music provider.

    * ``"auto"``    — Suno when SUNO_API_KEY is present, else the library.
    * ``"suno"``    — Suno, ProviderError if the key is missing.
    * ``"library"`` — always the library picker.
    """
    m = (mode or "auto").strip().lower()
    if m == "library":
        return LibraryMusicProvider()
    if m == "suno":
        suno = SunoMusicProvider()
        if not suno.available():
            raise ProviderError(
                "Music generation needs SUNO_API_KEY — add it to backend/.env, "
                "or switch to the library picker (mode='library')."
            )
        return suno
    if m == "auto":
        suno = SunoMusicProvider()
        if suno.available():
            return suno
        log.info("[media] no SUNO_API_KEY — music falls back to the library picker")
        return LibraryMusicProvider()
    raise ProviderError(f"Unknown music provider mode {mode!r} (use auto | suno | library).")
