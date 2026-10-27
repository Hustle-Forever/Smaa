"""/api/yas-producer/* — Yas Producer feature.

Legacy text-only Gemini chat at /chat (used by the Poster (no-human) mode).

New Poster+Human generative flow at /poster-human/*:
  - upload-ref            POST multipart      → store an uploaded ref, return token
  - generate              POST JSON           → Nano Banana image gen, sync ~15-30s
  - {id}/save-to-assets   POST JSON           → copy to producer_assets, embed
  - {id}/file             GET                 → stream the generated image
  - upload/{token}/file   GET                 → stream an uploaded ref
  - recent                GET                 → last 24h unsaved generations (rehydrate)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as FastAPIPath
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from ..config import config
from ..db import get_conn
from ..services import asset_storage
from ..services import gen_errors
from ..services import thumbs
from ..services import yas_local_store as ystore
from ..services import yas_animation_store as anim_store
from ..services import yas_general_video_store as gv_store
from ..services import higgsfield_client as higgsfield
from ..services import yas_video_edit_ai as ve_ai_service
from ..services import yas_video_editor as ve_service
from ..services import yas_voice_clone as voice_clone_service
from ..services.media_providers import (
    ProviderError as MediaProviderError,
    get_voice_provider as media_get_voice_provider,
)
from ..services.vertex_client import (
    GeminiNoImageError,
    VertexBudgetExceededError,
    gemini_embed,
    gemini_image,
    gemini_image_multipart,
    gemini_multimodal,
    gemini_text,
    get_meter,
    veo_generate,
)
from ..services.openai_client import (
    BudgetExceededError as OpenAIBudgetExceededError,
    chat as openai_chat,
)
from ..services.yas_poster_prompts import (
    ANIMATE_NEGATIVE_PROMPT,
    POSTER_HUMAN_STYLE,
    STYLE_EXTRACT_SCHEMA,
    STYLE_EXTRACT_SYSTEM,
    build_poster_human_user_message,
    build_template_fill_prompt,
    describe_poster_prompt,
    style_extract_user_prompt,
)

log = logging.getLogger("archivist.router.yas_producer")

router = APIRouter(prefix="/api/yas-producer", tags=["yas-producer"])


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_REFERENCES = 2          # original refs cap (retries can add prev attempt)
MAX_RETRY_DEPTH = 8         # chain length from root before we refuse
GEN_TIMEOUT_TEMPLATE_FILL_SEC = 150.0  # OpenAI image_edit at quality=high is slow
SUPPORTED_REF_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
# Video clips a user can drop straight into the Talking Head intro/outro slot —
# used AS-IS (no card-gen, no Veo). mime -> file extension.
SUPPORTED_CLIP_MIMES = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}

# Local disk root for yas-producer artefacts. Project-relative so the data
# dir can be moved as a unit (mirrors INSTAGRAM_ROOT, PRODUCER_ROOT pattern).
YAS_ROOT = (config.asset_root.parent / "yas_producer").resolve()
UPLOADS_DIR = YAS_ROOT / "uploads"
GEN_DIR = YAS_ROOT / "generations"

# Poster-pipeline names now OWNED by services/poster_pipeline.py (Stage 1 +
# Stage 2 moved there so non-request callers — e.g. the campaign worker — can
# drive them without importing this router). Re-bound here so every existing
# call site in this file keeps working against a single definition.
from ..services.poster_pipeline import (  # noqa: E402
    ENGINE_GEMINI_PRO,
    ENGINE_OPENAI,
    GEMINI_PRO_LOCATION,
    GEMINI_PRO_MODEL,
    GEN_TIMEOUT_GEMINI_PRO_SEC,
    GEN_TIMEOUT_SEC,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_READY,
    STATUS_SAVED,
    _ext_for_image_bytes,
    _image_dims,
    _is_permission_denied,
)

# Generated media is immutable per id (a new generation = a new id; refine /
# remove-logo create NEW ids), and the `?v=created_at` buster guards SERIAL-id
# reuse — so media can be cached HARD. `immutable` tells the browser never to
# revalidate, so a revisit paints from disk cache with zero network. Applied to
# the local FileResponse fallback (see `_media_response`; its redirects get
# _REDIRECT_CACHE_HEADERS) and to the streaming TH / video-editor endpoints.
# `private` (not `public`): these proxy routes can still 307 to a per-user signed
# URL for local-fallback/private rows, which a shared cache must never store.
_MEDIA_CACHE_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable"}
# The TH v2 final reel: one URL whose file changes when the reel is finalized
# again, so it is revalidated (FileResponse sends ETag / Last-Modified).
_REEL_CACHE_HEADERS = {"Cache-Control": "private, no-cache"}
# How long media proxy routes sign their Supabase target for.
_MEDIA_SIGN_TTL = 604800  # 7 days
# Freshness for a 307 whose target is a SIGNED url. Must stay well under
# _MEDIA_SIGN_TTL, and must NOT be `immutable` — the redirect has to be
# revalidatable once the signature it points at expires. One cheap revalidation
# per hour, and zero image bytes: the target itself is still hard-cached.
_REDIRECT_CACHE_HEADERS = {
    "Cache-Control": f"private, max-age={min(3600, _MEDIA_SIGN_TTL - 600)}"
}
# Talking Head keyframes are written to a per-RUN path (`.../{run_id}/main.png`)
# and UPSERTED in place when the user regenerates, so `immutable` would pin the
# old art for a year. A day of freshness with revalidation allowed instead.
_TH_FILE_CACHE_HEADERS = {"Cache-Control": "private, max-age=86400"}


def _cache_bust(url: str, rec: dict) -> str:
    """Append a content version (created_at) to an id-keyed media URL.

    Poster/animation rows use SERIAL ids; if a store is wiped and recreated the
    sequence restarts at 1, so a NEW record can inherit an OLD record's id — and
    the browser, which caches by the /…/{id}/file URL, would keep serving the old
    record's image. Tying the URL to created_at gives a reused id a fresh URL.
    The file routes ignore the query param; this mirrors the frontend buster in
    api.ts so the Library/Dashboard tiles and the creator grid agree on one URL.

    That agreement was BROKEN for SQL rows: `created_at` arrives as a datetime and
    `str(datetime)` spells the separator as a SPACE ("2026-07-26 17:03:25+00:00"),
    while the frontend builds its buster from the JSON-serialised ISO form with a
    "T". Same image, two different URLs, so the browser cached it twice and a tile
    could never reuse the creator grid's copy. Normalising to isoformat() here
    makes both sides agree.

    NOTE: this changes the buster VALUE, so every currently-cached media URL is
    invalidated once. Harmless (one refetch per image) but it is why this is not
    worth pairing with another cache-affecting change on the same day.
    """
    v = rec.get("created_at")
    if not v:
        return url
    if hasattr(v, "isoformat"):
        v = v.isoformat()
    from urllib.parse import quote
    # `&` when the URL already carries a query string (e.g. a transform URL with
    # ?width=&quality=), `?` otherwise.
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}v={quote(str(v), safe='')}"


# Grid thumbnail transform target. The Library/creator tiles render at ~200-400px;
# a ~480px WebP (content-negotiated by Supabase) is 10-50x smaller than the
# full-res original PNG. Detail views use the full public_url (no transform).
#
# HEIGHT + RESIZE ARE NOT OPTIONAL. Supabase's transformer defaults to
# `resizing_type: fill`, which with height unset keeps the SOURCE height and
# slices the width — a 1024x1024 poster came back 480x1024 with the headline cut
# off. `contain` scales the whole image into the box (and is *smaller*: measured
# 34,646 B vs 42,226 B for the cropped variant).
_THUMB_WIDTH = 480
_THUMB_HEIGHT = 480
_THUMB_QUALITY = 70
_THUMB_RESIZE = "contain"

# TTL for signed thumbnail URLs embedded in list JSON. Signed transform responses
# carry NO Cache-Control (only `expires`), so BROWSER CACHEABILITY == THIS VALUE.
# 24h: a full working day of cache hits, while bounding how long an auth-free
# bearer link to a PRIVATE asset lives in the DOM. Deliberately not 7 days.
_SIGNED_THUMB_TTL = 86400


def _public_media_url(
    rec: dict,
    *,
    bucket: str | None,
    path: str | None,
    proxy_url: str,
    thumb: bool = False,
) -> str:
    """Build the best URL for a stored media object.

    Prefers a PERMANENT PUBLIC Supabase CDN URL (no backend hop, no per-request
    signing, immutable-cacheable) when the object lives in Supabase Storage; for
    image thumbnails it uses the on-the-fly transform endpoint (~480px WebP). Falls
    back to the backend `proxy_url` for local-disk rows (no bucket/path) or if
    SUPABASE_URL isn't configured. Always carries the `?v=created_at` buster so a
    reused SERIAL id can't serve stale art.

    TWO gates, ANDed:
      * `config.producer_media_public` — the operator's kill switch. A public CDN
        URL bypasses the app access gate in main.py entirely, so turning the flag
        off must keep media behind the backend proxy.
      * `asset_storage.bucket_is_public(bucket)` — whether THIS bucket is really
        public. The flag alone was the bug: it describes one bucket, but `bucket`
        comes from the ROW, and every yas_generations row names the PRIVATE
        producer-assets bucket → /object/public/producer-assets/... → HTTP 400
        {"error":"Bucket not found"} → <img onError> → the words "no preview".
    """
    if (
        config.producer_media_public
        and bucket and path
        and asset_storage.bucket_is_public(bucket)
    ):
        try:
            # The transform endpoint is a paid add-on; without it this URL would
            # 403 in the browser and blank the tile. Falling back to the plain
            # public URL costs bytes, not correctness (#37).
            if thumb and asset_storage.image_transform_enabled():
                base = asset_storage.public_render_url(
                    bucket, path,
                    width=_THUMB_WIDTH, height=_THUMB_HEIGHT,
                    quality=_THUMB_QUALITY, resize=_THUMB_RESIZE,
                )
            else:
                base = asset_storage.public_url(bucket, path)
            return _cache_bust(base, rec)
        except Exception:
            # SUPABASE_URL unset / helper failure → degrade to the proxy route.
            pass
    return _cache_bust(proxy_url, rec)


def _needs_signed_thumb(url: str | None, bucket: str | None, path: str | None) -> bool:
    """True when a tile fell back to the backend proxy but its bytes DO live in
    Supabase — i.e. the bucket is private. Those are the rows a signed transform
    can rescue from a full-resolution proxy hop."""
    return bool(url and bucket and path and not url.startswith("http"))


async def _apply_signed_thumbs(items: list[dict]) -> None:
    """Upgrade proxy thumbnails to signed IMAGE-TRANSFORM URLs, in place.

    `/render/image/public` cannot serve a private bucket, so private rows would
    otherwise be stuck on the backend proxy at FULL resolution (~1.4 MB for a
    200px tile). A token with the transform baked in serves the same object as a
    ~20 kB WebP straight from the CDN — no backend hop, no DB round trip.

    Signing is BATCHED — one request per bucket, not one per tile. Signing 100
    objects individually measured ~26s and began tripping "Server disconnected"
    under concurrency; one batch call signs 100 in ~0.67s. Any failure leaves the
    proxy URL untouched, so this is strictly additive: a Supabase or quota problem
    degrades a tile to "slow", never to "blank".

    S3 has no transformer, so there a tile gets its stored 480px derivative
    (thumbs.derivative_urls, ~20-40 kB) when one exists. Otherwise it gets the
    signed ORIGINAL this once, and the derivative is made in the background.
    Originals are minted with no HEAD per tile: a missing object 404s in the
    browser and the tile's onError falls back to the proxy, which checks the
    bucket and the local copy.
    """
    pairs: list[tuple[str, str]] = [
        (src[0], str(src[1]))
        for it in items
        if (src := it.get("_thumb_src")) and src[0] and src[1]
    ]
    if pairs and asset_storage.backend_name() == "s3":
        by_src_bucket: dict[str, list[str]] = {}
        for b, p in pairs:
            by_src_bucket.setdefault(b, []).append(p)
        found = await asyncio.gather(
            *(
                thumbs.derivative_urls(b, paths, "thumb", expires=_SIGNED_THUMB_TTL)
                for b, paths in by_src_bucket.items()
            ),
            return_exceptions=True,
        )
        small: dict[tuple[str, str], str] = {}
        for b, res in zip(by_src_bucket, found):
            if isinstance(res, dict):
                small.update({(b, p): u for p, u in res.items()})
        for it in items:
            src = it.get("_thumb_src")
            if src and (url := small.get((src[0], str(src[1])))):
                it["thumbnail_url"] = url
                it.pop("_thumb_src", None)
        pairs = [bp for bp in pairs if bp not in small]
    # Decide the URL SHAPE before signing anything. The probe needs a real object
    # (Supabase resolves the key before checking the entitlement), and this is the
    # first place in the request that has one.
    await asset_storage.ensure_image_transform_capability(pairs[0] if pairs else None)

    by_bucket: dict[str, list[str]] = {}
    for b, p in pairs:
        # Supabase's transformer 400s on SVG/GIF, which would render as a broken
        # tile. Only relevant while transforms are actually in play — the plain
        # /object path serves every type — but the assets grid has always screened
        # these out and the two paths should not disagree.
        if asset_storage.image_transform_enabled() and p.lower().endswith((".svg", ".gif")):
            continue
        by_bucket.setdefault(b, []).append(p)
    if not by_bucket:
        for it in items:
            it.pop("_thumb_src", None)
        return

    # One batch per bucket, buckets in parallel (there is realistically one).
    results = await asyncio.gather(
        *(
            asset_storage.signed_render_urls(
                b, sorted(set(paths)), expires=_SIGNED_THUMB_TTL,
            )
            for b, paths in by_bucket.items()
        ),
        return_exceptions=True,
    )
    signed_by_bucket: dict[str, dict[str, str]] = {}
    for bucket, res in zip(by_bucket, results):
        signed_by_bucket[bucket] = res if isinstance(res, dict) else {}

    for it in items:
        src = it.pop("_thumb_src", None)
        if not src:
            continue
        url = signed_by_bucket.get(src[0], {}).get(src[1])
        if url:
            # No ?v= buster on a signed URL: on S3 every query parameter is
            # signed, so an appended one is a 403. A reused SERIAL id still gets
            # a fresh URL, because its new upload evicts the cached signature.
            it["thumbnail_url"] = url


def _etag_response(request: Request, payload: Any):
    """Return ``payload`` as JSON with a content ETag, or a bare 304 when the
    client's ``If-None-Match`` already matches. Lets the Library list + the
    creator poll loops (which re-request the same JSON every few seconds) skip the
    payload entirely when nothing changed. Serialization matches FastAPI's default
    (jsonable_encoder), so behaviour is identical apart from the conditional 304."""
    safe = jsonable_encoder(payload)
    body = json.dumps(safe, separators=(",", ":"), sort_keys=True).encode()
    etag = 'W/"' + hashlib.md5(body).hexdigest() + '"'  # weak: content identity
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return JSONResponse(content=safe, headers={"ETag": etag, "Cache-Control": "no-cache"})


async def _media_bytes(
    *, storage_bucket: str | None, storage_path: str | None, local_path: str | None,
) -> bytes | None:
    """Read generation-media bytes from the object store, else the local copy.

    A bucket read that fails or comes back empty falls through to `local_path`:
    a row can carry both (the S3 backfill keeps local_path), and a storage
    hiccup must not lose a save or a refine that the disk copy can serve."""
    if storage_bucket and storage_path:
        try:
            data = await asset_storage.download_bytes(
                storage_bucket=storage_bucket, storage_path=storage_path,
            )
            if data:
                return data
        except Exception as e:  # noqa: BLE001
            log.warning(
                "media read of %s/%s failed, trying the local copy: %s",
                storage_bucket, storage_path, e,
            )
    if local_path:
        try:
            p = asset_storage.local_to_abs(local_path)
            if p.exists():
                return p.read_bytes()
        except Exception:
            return None
    return None


async def _media_response(
    *, storage_bucket: str | None, storage_path: str | None, local_path: str | None,
    mime: str, headers: dict | None = None, download_name: str | None = None,
):
    """Serve a stored file: 307-redirect to a signed object-store URL, else a
    local FileResponse fallback. Returns None when nothing is retrievable.

    `download_name` forces a save-to-disk: url_for signs it into the URL (so the
    store answers with `Content-Disposition: attachment`), and the local
    FileResponse fallback sets the same header. Without it, the bare `download`
    attribute on a frontend <a> is ignored across a cross-origin redirect and the
    browser just plays/opens the file. Never append to the signed URL itself: on
    S3 every query parameter is signed, so an extra one is a 403.

    `headers` apply to the local FileResponse only. The 307 always carries
    _REDIRECT_CACHE_HEADERS, whatever the caller passes."""
    # Sign with a long TTL: the signed-URL cache (asset_storage) reuses it across
    # requests, so a 7-day signature keeps the redirect target stable.
    url = await asset_storage.url_for(
        storage_bucket=storage_bucket, storage_path=storage_path,
        local_path=local_path, expires=_MEDIA_SIGN_TTL,
        download_name=download_name,
    )
    if url:
        # A REDIRECT is not immutable content — its target carries a signature that
        # dies in _MEDIA_SIGN_TTL. Caching the 307 for a year with `immutable`
        # (which forbids revalidation) meant that once the signature expired the
        # browser kept replaying a dead redirect -> 400 -> "no preview", for the
        # remaining ~357 days, with no recovery short of a cache clear.
        # Cap freshness safely BELOW the signature lifetime instead. The caller's
        # headers are not merged in: callers pass _MEDIA_CACHE_HEADERS for the
        # local bytes, and that would put `immutable` back on the redirect.
        return RedirectResponse(
            url, status_code=307, headers=dict(_REDIRECT_CACHE_HEADERS),
        )
    if local_path:
        p = asset_storage.local_to_abs(local_path)
        if p.exists():
            # The local branch serves the BYTES, which really are immutable per id
            # — the long immutable header is correct here.
            resp_headers = {**_MEDIA_CACHE_HEADERS, **(headers or {})}
            if download_name:
                resp_headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
            return FileResponse(str(p), media_type=mime, headers=resp_headers)
    return None

# Animate (Veo 3 i2v) — defaults + allowed values for the per-call toggles.
# Model stays a hidden default (UI doesn't expose a Fast/Pro switch yet).
# Override via YAS_ANIMATE_VEO_MODEL=veo-3.1-generate-001 in backend/.env if
# Fast variant's quota is exhausted (Pro has a separate quota bucket, ~5x cost).
import os as _os
ANIMATE_VEO_MODEL = _os.getenv("YAS_ANIMATE_VEO_MODEL", "veo-3.1-fast-generate-001")
ANIMATE_KICKOFF_TIMEOUT_SEC = 60.0   # only the LRO submit; Veo work continues async


# ---------------------------------------------------------------------------
# Legacy chat (used by Poster (no-human) mode, untouched)
# ---------------------------------------------------------------------------

POSTER_SYSTEM = (
    "You are a creative director for 42 Abu Dhabi, a tuition-free coding school "
    "in the UAE. You help create compelling poster concepts and copy. "
    "When the user describes what they want, provide: a concept description, "
    "headline text, body copy, and visual direction (colors, layout, mood). "
    "Keep responses concise and actionable."
)

POSTER_HUMAN_SYSTEM = (
    "You are a creative director for 42 Abu Dhabi, a tuition-free coding school "
    "in the UAE. You help create compelling poster concepts featuring real people. "
    "When the user describes what they want, provide: a concept description, "
    "headline text, body copy, visual direction (colors, layout, mood), and "
    "guidance on the human subject (pose, expression, wardrobe, setting). "
    "Unless the user names or describes a different person, the subject is an "
    "Emirati national in Emirati dress (a kandura and ghutra, or an abaya and "
    "shayla). Keep responses concise and actionable."
)


class ChatRequest(BaseModel):
    prompt: str
    mode: str  # "poster" or "poster_human"


class ChatResponse(BaseModel):
    response: str


# ---------------------------------------------------------------------------
# Text enhancement — polish a rough topic or feedback into something
# poster-appropriate. Used by ✨ Enhance buttons next to the textareas.
# Cheap (Gemini Flash, ~$0.0001 per call) and fast (<2s typical).
# ---------------------------------------------------------------------------

# Shared across ALL three enhance modes (topic / feedback / dialogue). This is
# the single "reuse everywhere" rule that stops the enhancer from deleting the
# concrete details a user typed (dates, times, locations, CTAs, links, prices,
# names). Improve the WORDING around the facts — never drop or invent the facts.
_ENHANCE_PRESERVE_RULE = (
    "PRESERVE EVERY CONCRETE FACT THE USER WROTE — verbatim. Never drop, "
    "change, soften, or invent them. Improve only the wording, grammar, tone, "
    "and structure AROUND these facts. Protected facts include:\n"
    "  - Dates and times (e.g. 'March 5', '6 PM', 'this Friday')\n"
    "  - Locations, venues, room/building numbers, addresses\n"
    "  - Calls-to-action (register, RSVP, apply, sign up, DM, swipe up)\n"
    "  - URLs, links, @handles, hashtags\n"
    "  - Numbers — prices, prizes, capacity, deadlines, ages, phone numbers\n"
    "  - Proper names — people, teams, events, products, sponsors, courses\n"
    "If a fact will not fit a tight word budget, KEEP THE FACT and cut filler "
    "words instead. Do NOT add any fact that was not in the user's input."
)

_ENHANCE_TOPIC_SYSTEM = (
    "You are a brand copywriter for 42 Abu Dhabi — a tuition-free, "
    "peer-learning coding school in the UAE. The audience is young, "
    "ambitious, multicultural (Emirati and international students). The "
    "brand voice is confident, modern, and inviting — never corporate, "
    "never AI-cliché. Your job is to turn a rough idea for a social-media "
    "poster into compelling, brand-on-tone poster copy.\n\n"
    f"{_ENHANCE_PRESERVE_RULE}\n\n"
    "OUTPUT SHAPE:\n"
    "  - Lead with the hook: one punchy headline + one short subtitle.\n"
    "  - THEN list every concrete detail the user gave (date, time, location, "
    "CTA, link, price, names) — each on its own short line, clear and "
    "complete. Do not cram them into the headline and do not drop them.\n"
    "  - Multi-line is fine. Length follows the facts: a bare idea stays "
    "short; a detail-rich idea keeps all of its details.\n"
    "  - Keep hashtags / emoji ONLY if the user typed them; never add your own.\n"
    "  - English only. No preamble, no explanation, no surrounding quote marks.\n"
    "  - Output ONLY the rewritten copy — nothing else."
)

_ENHANCE_FEEDBACK_SYSTEM = (
    "You are an image-edit instruction writer. The user has just generated "
    "a poster image with AI and wants to refine it. They've written rough "
    "feedback. Turn it into a SPECIFIC, ACTIONABLE instruction for an AI "
    "image generator.\n\n"
    f"{_ENHANCE_PRESERVE_RULE}\n\n"
    "OUTPUT RULES:\n"
    "  - Be concrete: name colors, positions, elements, treatments\n"
    "  - Sharpen the user's intent — do NOT change it, and do NOT drop any "
    "specific they mentioned (a colour, a piece of text, an element, a "
    "position, the exact change they asked for)\n"
    "  - As concise as possible WITHOUT losing any of those specifics\n"
    "  - No preamble. No explanation. No quote marks.\n"
    "  - Output ONLY the rewritten instruction — nothing else."
)

# Talking-head dialogue enhancer — tighten the spoken line WITHOUT
# generifying it into a slogan or dropping the facts the speaker stated.
_ENHANCE_DIALOGUE_SYSTEM = (
    "You are tightening a first-person line of dialogue for an 8-second "
    "talking-head Instagram reel. Preserve the speaker's voice, register, "
    "and phrasing wherever possible.\n\n"
    f"{_ENHANCE_PRESERVE_RULE}\n\n"
    "This is a SPOKEN line, so:\n"
    "  - Fix grammar, typos, and run-on sentences.\n"
    "  - AIM for ~20-25 spoken words (enough for ~8 s of speech) — but if "
    "keeping a fact the user said pushes slightly over, that's fine; cut "
    "filler, not facts.\n"
    "  - Keep first-person if the input is first-person; keep second-person "
    "if it is second-person.\n"
    "  - Keep concrete, specific words; do not generify into a slogan or "
    "headline.\n"
    "  - A long URL or @handle need not be read aloud verbatim — keep its "
    "meaning (e.g. 'the link in our bio') rather than spelling it out.\n"
    "  - Do NOT add hashtags, emoji, or call-to-action language unless the "
    "input already has them.\n"
    "Output ONLY the polished dialogue on one line. No preamble, no quotes, "
    "no commentary."
)


# General-AI-Video prompt enhancer — turn a short idea into a vivid, cinematic
# VIDEO generation prompt. The user's TOPIC dominates (same subject, just
# richer); brand examples flavour the tone only, never the subject.
_ENHANCE_VIDEO_SYSTEM = (
    "You write prompts for an AI VIDEO generator (Veo). Turn the user's rough "
    "idea into ONE vivid, cinematic prompt for a short clip. The user's TOPIC "
    "is the subject and MUST stay the subject — do not change what the video is "
    "about. Make it richer and more descriptive: describe the scene, the "
    "subject and its action, camera movement and shot type, lighting, mood, and "
    "setting.\n\n"
    f"{_ENHANCE_PRESERVE_RULE}\n\n"
    "OUTPUT RULES:\n"
    "  - Stay 100% on the user's topic — expand it, never replace it.\n"
    "  - One flowing prompt (1-4 sentences). Concrete, visual, filmable.\n"
    "  - Describe motion (the clip moves): subtle camera moves, what changes.\n"
    "  - Brand context is for TONE only (audience, vibe) — never inject brand "
    "names, dates, or facts the user didn't write.\n"
    "  - No headlines, no hashtags, no emoji, no on-screen text instructions "
    "unless the user asked for text.\n"
    "  - English only. No preamble, no quotes. Output ONLY the prompt."
)

# Human-character casting-brief enhancer. Feeds the Stage-1 casting director
# (human_character_prompts.py), which treats the text as a BRIEF: traits the
# user fixed are honoured by every candidate, open traits are varied.
# Deliberately does NOT reuse _ENHANCE_PRESERVE_RULE — its protected facts
# (dates, CTAs, links, hashtags) are poster copy, not person traits.
_ENHANCE_CHARACTER_SYSTEM = (
    "You enhance casting briefs for a photorealistic PERSON generator. The "
    "user describes a KIND of person to appear in social content for 42 Abu "
    "Dhabi (a tuition-free coding school in the UAE — young, ambitious, "
    "multicultural community). Rewrite the rough brief into ONE richer "
    "casting brief for ONE kind of person.\n\n"
    "TRAIT RULES:\n"
    "  - KEEP every trait the user fixed — gender, age or age band, ethnicity "
    "or nationality, role/profession, named wardrobe — never change or drop "
    "them.\n"
    "  - ENRICH only what the user left open: age band, gender presentation, "
    "ethnicity/nationality, role, wardrobe style, expression/vibe; optionally "
    "build and hair.\n"
    "  - When the user left nationality and ethnicity open, the person is an "
    "Emirati national: a man in a kandura and ghutra, or a woman in an abaya "
    "with a shayla framing a fully visible face (no niqab, no sunglasses).\n"
    "  - ONE kind of person, singular — not a crowd, not alternatives.\n\n"
    "OUTPUT RULES:\n"
    "  - 1-2 sentences of natural prose describing the person only.\n"
    "  - NO headline, NO subtitle, NO dates, NO CTAs, NO hashtags, NO emoji, "
    "NO on-image text, NO logos, NO camera or lighting jargon.\n"
    "  - English only. No preamble, no quotes. Output ONLY the brief."
)

# Poster+Human ON-POSTER copy enhancer. Distinct from _ENHANCE_TOPIC_SYSTEM
# (shared by Create Poster / Animation / both Talking Heads): this one is
# specialized for an INFORMATION poster with a real person as the hero, and it
# emits LABELLED slots (HEADLINE / SUBTITLE / CARD / DETAILS) that the image
# prompt's _poster_content_block reads as placement roles. Embeds the shared
# preserve-facts rule (poster copy DOES carry dates / CTAs / names).
_ENHANCE_POSTER_HUMAN_SYSTEM = (
    "You are a brand copywriter for 42 Abu Dhabi — a tuition-free, "
    "peer-learning coding school in the UAE. The audience is young, ambitious "
    "and multicultural (Emirati and international students). The brand voice is "
    "confident, modern, human and inviting — never corporate, never AI-cliche. "
    "You write the ON-POSTER copy for an INFORMATION POSTER that features a real "
    "person as the hero. The words you write are rendered directly onto the "
    "image by an image model, so they must be tight, clean, and typo-proof.\n\n"
    f"{_ENHANCE_PRESERVE_RULE}\n\n"
    "OUTPUT SHAPE — LABEL each part on its own line, using these EXACT labels, "
    "in this order, with no markdown and no extra commentary:\n"
    "  HEADLINE: 2-5 words, punchy, ALL-CAPS-friendly — the single big idea. "
    "No trailing period.\n"
    "  SUBTITLE: one short, calm supporting sentence (aim for 12 words or "
    "fewer).\n"
    "  CARD: include this line ONLY if the input names a specific person or "
    "gives a quote. Use either 'NAME - ROLE' (e.g. 'LAYLA AL-HASHIMI - "
    "SOFTWARE ENGINEER') or a short pull-quote in quotation marks (12 words or "
    "fewer). Omit the line entirely when there is no name and no quote.\n"
    "  DETAILS: include ONLY if the user gave concrete event facts. Put each "
    "fact the user wrote — date, time, venue, CTA, link, price, deadline, "
    "@handle — on its own short 'DETAILS:' line. Keep every one; drop none. "
    "Omit the label entirely when there are no such facts.\n\n"
    "RULES:\n"
    "  - Keep the HEADLINE and SUBTITLE tight; push specifics down into DETAILS "
    "rather than cramming them into the headline. Long copy causes rendering "
    "typos.\n"
    "  - Never invent a person, role, quote, date, price, or detail that was "
    "not in the user's input; omit any label you have no material for.\n"
    "  - ENGLISH / LATIN SCRIPT ONLY. Never output Arabic or any non-Latin "
    "script, and never suggest adding it.\n"
    "  - Do NOT write a logo line, a '42' mark, a '42 ABU DHABI' wordmark, or "
    "any brand tagline — the logo is added to the poster automatically.\n"
    "  - Do NOT describe the image, layout, colours, camera, lighting, or the "
    "person's appearance — output ONLY the words that go ON the poster.\n"
    "  - Keep hashtags / emoji ONLY if the user typed them; never add your "
    "own.\n"
    "  - No preamble, no explanation, no surrounding quote marks. Output ONLY "
    "the labelled lines above — nothing else."
)


class EnhanceRequest(BaseModel):
    text: str
    kind: str  # "topic" | "poster_human" | "feedback" | "dialogue" | "video_prompt" | "character"
    parent_generation_id: Optional[int] = None  # feedback mode: the image to reference


class EnhanceResponse(BaseModel):
    enhanced: str


async def _fetch_brand_examples(limit: int = 5) -> list[dict]:
    """Pull the most recent img_poster_human assets — display_name and a
    truncated ai_description. Used as in-context examples so the topic
    enhancer learns the brand's actual voice instead of generic platitudes.
    Truncated to keep the prompt cheap.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT display_name,
                       LEFT(COALESCE(ai_description, ''), 280) AS preview
                  FROM producer_assets
                 WHERE metadata->>'content_category' = 'img_poster_human'
                   AND COALESCE(ai_description, '') <> ''
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
    return [{"display_name": r["display_name"], "preview": r["preview"]} for r in rows]


async def _enhance_topic(text: str) -> str:
    """Topic enhance — pulls recent brand examples for context."""
    examples = await _fetch_brand_examples(limit=5)
    if examples:
        ex_block = "\n".join(
            f"  • {(e['display_name'] or 'untitled')[:80]}\n"
            f"    Caption summary: {(e['preview'] or '').strip()[:240]}"
            for e in examples
        )
        examples_clause = (
            "Here are 5 RECENT REAL posters from this brand. Study their "
            "VOICE — sentence length, punchy headlines, the way the brand "
            "speaks to its audience. Your rewrite should feel like the "
            "same voice — not a copy, but the same family.\n\n"
            f"{ex_block}\n\n"
        )
    else:
        examples_clause = ""

    system = _ENHANCE_TOPIC_SYSTEM
    user_prompt = (
        f"{examples_clause}"
        "Now enhance the user's rough idea into compelling, brand-on-tone "
        "poster copy: a punchy headline + short subtitle as the hook, then "
        "EVERY concrete detail they gave (dates, times, location, CTA, links, "
        "prices, names) kept intact on their own short lines. Do not drop or "
        "invent any detail. Output ONLY the rewritten copy.\n\n"
        f"ROUGH INPUT:\n{text}"
    )
    # thinking_budget=0 disables Flash 2.5's hidden thinking tokens that
    # otherwise eat the output budget and truncate replies. max_tokens=600
    # leaves room for a few detail lines (date / location / CTA) without trim.
    return await gemini_text(
        user_prompt,
        system=system,
        model=config.vertex_gemini_fast_model,
        temperature=0.6,
        max_tokens=600,
        thinking_budget=0,
    )


async def _enhance_poster_human(text: str) -> str:
    """Poster+Human ON-POSTER copy enhance — emits labelled HEADLINE / SUBTITLE
    / CARD / DETAILS slots for an information poster with a real person. Reuses
    the same recent-brand-examples voice context as _enhance_topic, but with its
    own specialized system prompt (the generic 'topic' enhancer stays shared by
    the other tools)."""
    examples = await _fetch_brand_examples(limit=5)
    if examples:
        ex_block = "\n".join(
            f"  • {(e['display_name'] or 'untitled')[:80]}\n"
            f"    Caption summary: {(e['preview'] or '').strip()[:240]}"
            for e in examples
        )
        examples_clause = (
            "Here are 5 RECENT REAL posters from this brand. Study their "
            "VOICE — sentence length, punchy headlines, the way the brand "
            "speaks to its audience. Your rewrite should feel like the "
            "same voice — not a copy, but the same family.\n\n"
            f"{ex_block}\n\n"
        )
    else:
        examples_clause = ""

    user_prompt = (
        f"{examples_clause}"
        "Now turn the user's rough idea into ON-POSTER copy for a 42 Abu Dhabi "
        "information poster with a real person as the hero. Return it as the "
        "labelled lines defined in the system prompt: a HEADLINE (2-5 words), "
        "a short SUBTITLE, a CARD line ONLY if a specific person or quote is "
        "given, and a DETAILS line for EACH concrete fact the user provided "
        "(date, time, venue, CTA, link, price) — keep every fact, drop or "
        "invent nothing. Output ONLY the labelled lines.\n\n"
        f"ROUGH INPUT:\n{text}"
    )
    return await gemini_text(
        user_prompt,
        system=_ENHANCE_POSTER_HUMAN_SYSTEM,
        model=config.vertex_gemini_fast_model,
        temperature=0.6,
        max_tokens=600,
        thinking_budget=0,
    )


async def _enhance_video_prompt(text: str) -> str:
    """General-AI-Video prompt enhance — richer, cinematic, but topic-faithful.
    Brand examples flavour the tone only; the user's topic stays the subject."""
    examples = await _fetch_brand_examples(limit=4)
    if examples:
        ex_block = "\n".join(
            f"  • {(e['display_name'] or 'untitled')[:80]}: {(e['preview'] or '').strip()[:160]}"
            for e in examples
        )
        examples_clause = (
            "For TONE/audience context only (do NOT borrow their subjects, "
            "names, or facts), here are a few recent brand posts:\n"
            f"{ex_block}\n\n"
        )
    else:
        examples_clause = ""

    user_prompt = (
        f"{examples_clause}"
        "Rewrite the user's idea below into ONE vivid, cinematic video-generation "
        "prompt. Keep it about the SAME subject — expand with scene, action, "
        "camera, lighting and mood. Do not change the topic or invent facts.\n\n"
        f"USER'S IDEA:\n{text}"
    )
    return await gemini_text(
        user_prompt,
        system=_ENHANCE_VIDEO_SYSTEM,
        model=config.vertex_gemini_fast_model,
        temperature=0.7,
        max_tokens=400,
        thinking_budget=0,
    )


async def _enhance_feedback(text: str, parent_generation_id: Optional[int]) -> str:
    """Feedback enhance — if parent_generation_id supplied, look at the
    actual generated image and rewrite the vague feedback into something
    specific to what's IN the image. Falls back to text-only otherwise.
    """
    # Try to load the parent image
    image_path: Path | None = None
    parent_topic: str | None = None
    if parent_generation_id is not None:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT topic, local_path, storage_bucket, storage_path, mime_type
                      FROM yas_generations
                     WHERE id = %s
                    """,
                    (parent_generation_id,),
                )
                row = await cur.fetchone()
        if row:
            parent_topic = row.get("topic")
            if row.get("local_path"):
                p = Path(row["local_path"])
                if not p.is_absolute():
                    p = config.asset_root.parent / p
                if p.exists():
                    image_path = p
            if image_path is None and row.get("storage_bucket") and row.get("storage_path"):
                # Fetch from bucket to a temp file. internal=True: the backend
                # GETs it itself and cannot reach the public /media origin.
                signed = await asset_storage.url_for(
                    storage_bucket=row["storage_bucket"],
                    storage_path=row["storage_path"],
                    local_path=None, expires=600, internal=True,
                )
                if signed:
                    import httpx
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        r = await client.get(signed)
                        if r.status_code < 300:
                            cache_dir = YAS_ROOT / "feedback_cache"
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            ext = Path(row["storage_path"]).suffix or ".png"
                            tmp = cache_dir / f"gen-{parent_generation_id}{ext}"
                            tmp.write_bytes(r.content)
                            image_path = tmp

    if image_path is not None:
        # Multimodal feedback enhance — see the actual image. max_tokens=8000
        # because Flash 2.5 burns hidden thinking tokens; small budgets
        # truncate mid-sentence. The prompt also forbids quoted strings
        # because the model otherwise echoes text from the image as quoted
        # substrings and loses the closing quote token.
        topic_line = (
            f"The poster's topic was: {parent_topic}\n\n"
            if parent_topic else ""
        )
        return await gemini_multimodal(
            (
                f"{topic_line}"
                f"USER'S ROUGH FEEDBACK ON THE ATTACHED POSTER:\n  {text}\n\n"
                "Rewrite this feedback into ONE specific actionable image-edit "
                "instruction that references what's ACTUALLY in the attached "
                "image. Describe colors, regions, and elements in natural "
                "language. DO NOT use quote marks or quote any specific text "
                "you see in the image — describe elements by their role "
                "(headline, subtitle, CTA bar, background motif, etc).\n\n"
                "STRICT FORMAT RULES:\n"
                "  - One sentence under 35 words\n"
                "  - No quote marks (single or double)\n"
                "  - No bullet points, no numbered lists\n"
                "  - No preamble, no 'Here is the instruction:' framing\n"
                "  - Output ONLY the final instruction on a single line"
            ),
            images=[image_path],
            system=_ENHANCE_FEEDBACK_SYSTEM,
            model=config.vertex_gemini_fast_model,
            temperature=0.4,
            max_tokens=800,
            thinking_budget=0,  # disable thinking to prevent ~80-char truncation
        )

    # Fallback: text-only enhance
    return await gemini_text(
        (
            "Enhance this feedback into a specific instruction for an AI image "
            "regenerator. Keep every specific the user mentioned (colours, "
            "text, elements, positions, the exact change).\n\n"
            f"ROUGH FEEDBACK:\n{text}"
        ),
        system=_ENHANCE_FEEDBACK_SYSTEM,
        model=config.vertex_gemini_fast_model,
        temperature=0.4,
        max_tokens=600,
        thinking_budget=0,
    )


async def _enhance_dialogue(text: str) -> str:
    """Talking-head dialogue enhance — tightens the spoken line while
    preserving the user's voice. No examples needed; the system prompt
    is restrictive enough that the model stays close to the input."""
    user_prompt = (
        "Polish this line of dialogue for an 8-second talking-head reel "
        "per the system rules. Output ONLY the polished line.\n\n"
        f"ROUGH DIALOGUE:\n{text}"
    )
    return await gemini_text(
        user_prompt,
        system=_ENHANCE_DIALOGUE_SYSTEM,
        model=config.vertex_gemini_fast_model,
        temperature=0.4,
        max_tokens=500,
        thinking_budget=0,
    )


async def _enhance_character(text: str) -> str:
    """Casting-brief enhance (Human Character) — enrich the person description
    while keeping every trait the user fixed. No brand examples: person briefs
    need trait fidelity, not the poster copy voice."""
    user_prompt = (
        "Enhance this rough description of a person into one richer casting "
        "brief per the system rules. Keep every trait the user fixed; enrich "
        "only what they left open. Output ONLY the brief.\n\n"
        f"ROUGH BRIEF:\n{text}"
    )
    return await gemini_text(
        user_prompt,
        system=_ENHANCE_CHARACTER_SYSTEM,
        model=config.vertex_gemini_fast_model,
        temperature=0.5,
        max_tokens=350,
        thinking_budget=0,
    )


@router.post("/enhance", response_model=EnhanceResponse)
async def enhance(body: EnhanceRequest) -> EnhanceResponse:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 600:
        raise HTTPException(400, "text too long (max 600 chars)")
    try:
        if body.kind == "topic":
            out = await _enhance_topic(text)
        elif body.kind == "poster_human":
            out = await _enhance_poster_human(text)
        elif body.kind == "feedback":
            out = await _enhance_feedback(text, body.parent_generation_id)
        elif body.kind == "dialogue":
            out = await _enhance_dialogue(text)
        elif body.kind == "video_prompt":
            out = await _enhance_video_prompt(text)
        elif body.kind == "character":
            out = await _enhance_character(text)
        else:
            raise HTTPException(400, "kind must be 'topic', 'poster_human', 'feedback', 'dialogue', 'video_prompt', or 'character'")
    except VertexBudgetExceededError as e:
        raise HTTPException(503, str(e))
    except HTTPException:
        raise
    except Exception as e:
        log.exception("enhance failed")
        raise HTTPException(502, f"enhance failed: {str(e)[:200]}")

    # Handle the case where gemini_multimodal returns dict (rare on plain text)
    if isinstance(out, dict):
        out = out.get("_raw") or str(out)
    cleaned = (out or "").strip().strip('"').strip("'").strip()
    if not cleaned:
        raise HTTPException(502, "enhancement returned empty text")
    return EnhanceResponse(enhanced=cleaned)


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    system = POSTER_HUMAN_SYSTEM if body.mode == "poster_human" else POSTER_SYSTEM
    try:
        result = await gemini_text(
            body.prompt, system=system, model="gemini-2.5-flash",
            temperature=0.7, max_tokens=1000,
        )
        return ChatResponse(response=result)
    except Exception as e:
        log.exception("Gemini chat failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    GEN_DIR.mkdir(parents=True, exist_ok=True)


def _serialise(v: Any) -> Any:
    from datetime import datetime
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


def _gen_row_to_dict(row: dict) -> dict:
    """Shape sent to the frontend. Includes a stable file URL.

    If the row was produced by the LEFT JOIN in /recent, the anim_* columns
    are folded into a nested `animation` object so the frontend doesn't have
    to do a second call to rehydrate animation state.
    """
    out = _row(row) or {}
    if row.get("status") == STATUS_READY and row.get("id") is not None:
        out["file_url"] = f"/api/yas-producer/poster-human/{row['id']}/file"
    elif row.get("status") == STATUS_SAVED and row.get("id") is not None:
        out["file_url"] = f"/api/yas-producer/poster-human/{row['id']}/file"

    if row.get("anim_id") is not None:
        anim = {
            "id": row["anim_id"],
            "yas_generation_id": row.get("id"),
            "status": row.get("anim_status"),
            "veo_model": row.get("anim_veo_model"),
            "duration_sec": row.get("anim_duration_sec"),
            "with_audio": row.get("anim_with_audio"),
            "aspect_ratio": row.get("anim_aspect_ratio"),
            "cost_usd": _serialise(row.get("anim_cost_usd")),
            "error": row.get("anim_error"),
            "created_at": _serialise(row.get("anim_created_at")),
            "updated_at": _serialise(row.get("anim_updated_at")),
        }
        if anim["status"] == STATUS_READY:
            anim["file_url"] = f"/api/yas-producer/poster-human/animations/{row['anim_id']}/file"
        out["animation"] = anim
    else:
        out["animation"] = None
    # Strip the flat anim_* keys — clients should read out["animation"] instead.
    for k in (
        "anim_id", "anim_status", "anim_veo_model", "anim_duration_sec",
        "anim_with_audio", "anim_aspect_ratio", "anim_cost_usd", "anim_error",
        "anim_created_at", "anim_updated_at",
    ):
        out.pop(k, None)
    return out


def _library_item(row: dict) -> dict:
    """Lightweight tile shape for the Library grid. Derives the generation
    TYPE from `mode`, plus a thumbnail URL and (when a finished video exists)
    a video URL. Reuses the existing file-serving routes — no hydration.

    Talking-head video is only surfaced when it's actually playable: the
    stitched composition is ready, OR the single-scene main animation is
    ready. Poster+Human video is its latest ready animation.
    """
    md = row.get("metadata")
    if not isinstance(md, dict):
        md = {}
    gid = row.get("id")
    is_th = row.get("mode") == th_service.MODE_PREVIEW
    # Talking Head v2 — isolated twin. Its preview rows surface in the same
    # Library grid but route to the /talking-head-v2/* file endpoints and a
    # distinct tile type so the detail modal fetches via the v2 service.
    is_th_v2 = row.get("mode") == th_v2_service.MODE_PREVIEW
    # Video Editor — pure assembly/render category. Its run rows carry the whole
    # editable timeline under metadata.video_edit; the tile previews the final
    # exported MP4 once the render lands.
    is_ve = row.get("mode") == ve_service.MODE_PREVIEW

    thumbnail_url: str | None = None
    video_url: str | None = None
    # A non-final video clip to preview (hover-play) when no FINAL deliverable
    # exists yet — e.g. a Talking Head's main scene clip before stitching. Kept
    # separate from `video_url`/`has_video` so the lifecycle (pending vs
    # finished) stays keyed on whether the FINAL video exists.
    preview_url: str | None = None
    # What the tile is showing: "final" | "scene" | "still" | "none".
    preview_kind: str | None = None
    composition_status: str | None = None
    # A Video Editor project can be a plain editor project, OR the headless
    # backing store for an Ideation "General video" tool — disambiguated by the
    # timeline's project.preset ("general_video" | "b_roll"). This drives a
    # distinct Library tile type so resume routes back to the right creator.
    ve_preset: str | None = None
    # Full-res download URL — ALWAYS the backend proxy route (forces a sensible
    # filename via Content-Disposition and serves the ORIGINAL, never a transform
    # thumbnail). Display URLs above may be fast public/transform CDN URLs.
    download_url: str | None = None

    if is_ve:
        ve_doc = md.get("video_edit") if isinstance(md.get("video_edit"), dict) else {}
        render = ve_doc.get("render") if isinstance(ve_doc.get("render"), dict) else {}
        proj = ve_doc.get("project") if isinstance(ve_doc.get("project"), dict) else {}
        ve_preset = proj.get("preset")
        composition_status = render.get("composition_status")
        if composition_status == ve_service.COMP_READY:
            video_url = f"/api/yas-producer/video-editor/{gid}/composition/file"
            # The <video> element renders its own first-frame poster client-side.
            thumbnail_url = video_url
            download_url = video_url
            preview_kind = "final"
        else:
            # Not exported yet — preview the first visual clip from the timeline
            # (a still/asset image, or a JPG first-frame thumb for a video
            # source), so drafts show a real frame instead of "no preview".
            thumbnail_url = ve_service.library_thumb_source(md)
            preview_kind = "still" if thumbnail_url else "none"
    elif is_th or is_th_v2:
        base = "talking-head-v2" if is_th_v2 else "talking-head"
        comp_ready = th_v2_service.COMP_READY if is_th_v2 else th_service.COMP_READY
        # scenes[0] keyframe lives on the run row itself — the tile's poster /
        # still-fallback image.
        proxy = f"/api/yas-producer/{base}/{gid}/file"
        download_url = _cache_bust(proxy, row)
        # Route TH tiles through the SAME url builder as the poster-human branch
        # below instead of hardcoding the full-resolution proxy. A run with no
        # retrievable keyframe gets thumbnail_url=None rather than a URL that is
        # guaranteed to 410 — an always-failing URL costs a round trip AND makes a
        # data state look like an outage.
        if row.get("storage_path") or row.get("local_path"):
            thumbnail_url = _public_media_url(
                row, bucket=row.get("storage_bucket"), path=row.get("storage_path"),
                proxy_url=proxy, thumb=True,
            )
        else:
            thumbnail_url = None
        composition_status = md.get("composition_status")
        # Show whatever is ready, best-first: the stitched reel, else the main
        # scene clip (Veo) once it lands, else the keyframe still. The detail
        # modal still hydrates the full run for the complete view.
        if is_th_v2 and md.get("render_owner") and md.get("composition_path"):
            # Finalized again (services/th_reel): its last good reel, even
            # while a new one renders, named by its seq (the plain URL is
            # held as immutable by browsers).
            video_url = (f"/api/yas-producer/{base}/{gid}/composition/file"
                         f"?v={int(md.get('composition_good_seq') or 0)}")
            download_url = video_url
            preview_kind = "final"
        elif composition_status == comp_ready:
            video_url = f"/api/yas-producer/{base}/{gid}/composition/file"
            download_url = video_url
            preview_kind = "final"
        elif row.get("main_anim_status") == STATUS_READY:
            preview_url = f"/api/yas-producer/{base}/{gid}/video"
            preview_kind = "scene"
        else:
            preview_kind = "still" if thumbnail_url else "none"
    else:
        if row.get("status") in (STATUS_READY, STATUS_SAVED):
            thumbnail_url = _public_media_url(
                row, bucket=row.get("storage_bucket"), path=row.get("storage_path"),
                proxy_url=f"/api/yas-producer/poster-human/{gid}/file", thumb=True,
            )
            download_url = f"/api/yas-producer/poster-human/{gid}/file"
        if row.get("anim_id") is not None and row.get("anim_status") == STATUS_READY:
            video_url = _public_media_url(
                row, bucket=row.get("anim_storage_bucket"), path=row.get("anim_storage_path"),
                proxy_url=f"/api/yas-producer/poster-human/animations/{row['anim_id']}/file",
            )
            download_url = f"/api/yas-producer/poster-human/animations/{row['anim_id']}/file"
        preview_kind = "final" if video_url else ("still" if thumbnail_url else "none")

    return {
        "id": gid,
        "type": (
            "broll" if (is_ve and ve_preset == "b_roll")
            else "general_video" if (is_ve and ve_preset == "general_video")
            else "video_editor" if is_ve
            else "talking_head_v2" if is_th_v2
            else "talking_head" if is_th
            else "poster_human"
        ),
        "topic": row.get("topic"),
        "status": row.get("status"),
        "image_model": row.get("image_model"),
        "cost_usd": _serialise(row.get("cost_usd")),
        "created_at": _serialise(row.get("created_at")),
        "updated_at": _serialise(row.get("updated_at")),
        "thumbnail_url": thumbnail_url,
        "video_url": video_url,
        "has_video": video_url is not None,
        "download_url": download_url,
        "preview_url": preview_url,
        "preview_kind": preview_kind,
        "composition_status": composition_status,
        # Private-key hint for _apply_signed_thumbs: the object exists in Supabase
        # but its bucket isn't public, so the tile is currently on the slow proxy
        # and can be upgraded to a signed transform. Popped before serialisation.
        "_thumb_src": (
            (row.get("storage_bucket"), row.get("storage_path"))
            if _needs_signed_thumb(
                thumbnail_url, row.get("storage_bucket"), row.get("storage_path"),
            ) else None
        ),
    }


def _library_item_poster(rec: dict) -> dict:
    """Library tile for a poster-only (Create Poster) record. These live in the
    local JSON store, not yas_generations, so they're merged into /library here.
    """
    rid = rec.get("id")
    ready = rec.get("status") in (STATUS_READY, STATUS_SAVED)
    thumb = _public_media_url(
        rec, bucket=rec.get("storage_bucket"), path=rec.get("storage_path"),
        proxy_url=f"/api/yas-producer/poster/{rid}/file", thumb=True,
    ) if ready else None
    return {
        "id": rid,
        "type": "poster",
        "topic": rec.get("topic"),
        "status": rec.get("status"),
        "image_model": rec.get("image_model"),
        "cost_usd": rec.get("cost_usd"),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
        "thumbnail_url": thumb,
        "video_url": None,
        "has_video": False,
        "download_url": f"/api/yas-producer/poster/{rid}/file" if ready else None,
        "preview_url": None,
        "preview_kind": "still" if ready else "none",
        "composition_status": None,
        "_thumb_src": (
            (rec.get("storage_bucket"), rec.get("storage_path"))
            if _needs_signed_thumb(
                thumb, rec.get("storage_bucket"), rec.get("storage_path"),
            ) else None
        ),
    }


def _library_item_animation(rec: dict) -> dict:
    """Library tile for a standalone Create-Animation record (local JSON store).
    The animation store keys the brief under `prompt` — there is no `topic`.
    """
    aid = rec.get("id")
    ready = rec.get("status") == STATUS_READY
    fc = rec.get("frame_cost_usd") or 0
    vc = rec.get("veo_cost_usd") or 0
    try:
        cost = float(fc) + float(vc)
    except (TypeError, ValueError):
        cost = 0.0
    has_frame = bool(rec.get("frame_storage_path") or rec.get("frame_local_path"))
    frame_thumb = _public_media_url(
        rec, bucket=rec.get("frame_storage_bucket"), path=rec.get("frame_storage_path"),
        proxy_url=f"/api/yas-producer/animation/{aid}/frame", thumb=True,
    ) if has_frame else None
    return {
        "id": aid,
        "type": "animation",
        "topic": rec.get("prompt"),
        "status": rec.get("status"),
        "image_model": None,
        "cost_usd": cost or None,
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
        "thumbnail_url": frame_thumb,
        "video_url": _public_media_url(
            rec, bucket=rec.get("video_storage_bucket"), path=rec.get("video_storage_path"),
            proxy_url=f"/api/yas-producer/animation/{aid}/file",
        ) if ready else None,
        "has_video": ready,
        "download_url": (
            f"/api/yas-producer/animation/{aid}/file" if ready
            else f"/api/yas-producer/animation/{aid}/frame"
            if (rec.get("frame_storage_path") or rec.get("frame_local_path")) else None
        ),
        "preview_url": None,
        "preview_kind": "final" if ready else "none",
        "composition_status": None,
        "_thumb_src": (
            (rec.get("frame_storage_bucket"), rec.get("frame_storage_path"))
            if _needs_signed_thumb(
                frame_thumb,
                rec.get("frame_storage_bucket"), rec.get("frame_storage_path"),
            ) else None
        ),
    }


# Style-fingerprint cache keyed by sorted ref-path tuple. Lives until backend
# restart. Avoids re-extracting on retries within the same session.
_style_cache: dict[tuple[str, ...], dict] = {}


async def _extract_style_attributes(ref_paths: list[Path]) -> dict | None:
    """Call Gemini Flash multimodal to extract a STYLE FINGERPRINT from the
    references. Returns the parsed dict, or None if extraction fails or no
    refs are given. Results are cached per ref-set in process.
    """
    if not ref_paths:
        return None
    cache_key = tuple(sorted(str(p) for p in ref_paths))
    if cache_key in _style_cache:
        return _style_cache[cache_key]

    try:
        # 60s — the expanded design-DNA schema (10+ fields, several arrays)
        # makes Gemini Flash slower; 25s was timing out. Also bumped
        # max_tokens to give the model room for the deeper extraction.
        result = await asyncio.wait_for(
            gemini_multimodal(
                style_extract_user_prompt(len(ref_paths)),
                images=ref_paths,
                system=STYLE_EXTRACT_SYSTEM,
                model=config.vertex_gemini_fast_model,
                json_schema=STYLE_EXTRACT_SCHEMA,
                temperature=0.3,
                max_tokens=6000,
            ),
            timeout=60.0,
        )
    except Exception:
        log.exception("style extraction failed; proceeding without style attrs")
        return None

    if not isinstance(result, dict) or "_raw" in result:
        log.warning("style extraction returned non-dict / _raw — skipping")
        return None

    _style_cache[cache_key] = result
    log.info(
        "[yas-style] extracted: palette=%r illustration=%r",
        (result.get("palette") or {}).get("palette_description", "")[:60],
        (result.get("illustration_style") or "")[:60],
    )
    return result


async def _budget_guard() -> None:
    """No-op while the meter is unenforced, which it is. See `VertexCostMeter`."""
    meter = get_meter()
    if meter.over_budget(0.95):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Vertex spend is ${meter.spent_usd:.4f} of "
                f"${meter.budget_usd:.2f} budget (≥95%). Refusing generation."
            ),
        )


async def _retry_depth(parent_id: int) -> int:
    """Return the depth of `parent_id` in its chain (1 = root)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH RECURSIVE chain AS (
                  SELECT id, parent_generation_id, 1 AS depth
                    FROM yas_generations WHERE id = %s
                  UNION ALL
                  SELECT g.id, g.parent_generation_id, c.depth + 1
                    FROM yas_generations g JOIN chain c ON c.parent_generation_id = g.id
                )
                SELECT MAX(depth) AS depth FROM chain
                """,
                (parent_id,),
            )
            row = await cur.fetchone()
    return int(row.get("depth") or 1) if row else 1


async def _resolve_asset_paths(asset_ids: list[int]) -> list[Path]:
    """For each producer_assets id, return the local file path (downloading
    from bucket if needed). Used as source_images for gemini_image."""
    paths: list[Path] = []
    if not asset_ids:
        return paths
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, storage_bucket, storage_path, local_path, mime_type
                  FROM producer_assets
                 WHERE id = ANY(%s)
                """,
                (asset_ids,),
            )
            rows = await cur.fetchall()
    by_id = {int(r["id"]): r for r in rows}
    for aid in asset_ids:
        row = by_id.get(int(aid))
        if not row:
            raise HTTPException(404, f"asset #{aid} not found")
        # Local-disk asset
        if row.get("local_path"):
            p = asset_storage.local_to_abs(row["local_path"])
            if p.exists():
                paths.append(p)
                continue
        # Bucket asset → download to a temp file under YAS_ROOT/refs_cache.
        # internal=True: the backend GETs it itself and cannot reach the
        # public /media origin.
        if row.get("storage_bucket") and row.get("storage_path"):
            signed = await asset_storage.url_for(
                storage_bucket=row["storage_bucket"],
                storage_path=row["storage_path"],
                local_path=None,
                expires=600,
                internal=True,
            )
            if not signed:
                raise HTTPException(502, f"could not sign URL for asset #{aid}")
            cache_dir = YAS_ROOT / "refs_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            # Stable filename = asset id + sanitised extension
            ext = Path(row["storage_path"]).suffix.lstrip(".") or "jpg"
            cached = cache_dir / f"asset-{aid}.{ext}"
            if not cached.exists():
                import httpx
                async with httpx.AsyncClient(timeout=60.0) as client:
                    r = await client.get(signed)
                    if r.status_code >= 300:
                        raise HTTPException(
                            502, f"bucket fetch failed for asset #{aid} ({r.status_code})"
                        )
                    cached.write_bytes(r.content)
            paths.append(cached)
            continue
        raise HTTPException(410, f"asset #{aid} has no resolvable file")
    return paths


async def _resolve_upload_paths(upload_ids: list[int]) -> list[Path]:
    paths: list[Path] = []
    if not upload_ids:
        return paths
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, local_path FROM yas_uploads WHERE id = ANY(%s)",
                (upload_ids,),
            )
            rows = await cur.fetchall()
    by_id = {int(r["id"]): r for r in rows}
    for uid in upload_ids:
        row = by_id.get(int(uid))
        if not row:
            raise HTTPException(404, f"upload #{uid} not found")
        p = Path(row["local_path"])
        if not p.is_absolute():
            backend_root = config.asset_root.parent
            p = backend_root / p
        if not p.exists():
            raise HTTPException(410, f"upload #{uid} file missing on disk")
        paths.append(p)
    return paths


async def _cleanup_old_uploads() -> None:
    """Opportunistic cleanup: drop yas_uploads rows + files older than 24h
    that aren't referenced by any non-saved-to-assets yas_generations row.

    Cheap, idempotent — best-effort, never raises out of this function.
    """
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                # Find candidates
                await cur.execute(
                    """
                    SELECT id, local_path FROM yas_uploads
                     WHERE created_at < now() - interval '24 hours'
                       AND id NOT IN (
                         SELECT unnest(reference_upload_ids)
                           FROM yas_generations
                          WHERE created_at >= now() - interval '24 hours'
                       )
                     LIMIT 50
                    """
                )
                stale = await cur.fetchall()
                for r in stale:
                    p = Path(r["local_path"])
                    if not p.is_absolute():
                        p = config.asset_root.parent / p
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
                if stale:
                    await cur.execute(
                        "DELETE FROM yas_uploads WHERE id = ANY(%s)",
                        ([int(r["id"]) for r in stale],),
                    )
    except Exception:
        log.exception("yas_uploads cleanup failed (non-fatal)")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/poster-human/upload-ref")
async def upload_ref(file: UploadFile = File(...)) -> dict:
    """Store a user-uploaded reference image. Returns the upload row."""
    _ensure_dirs()
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    mime = (
        file.content_type
        or mimetypes.guess_type(file.filename or "")[0]
        or "application/octet-stream"
    ).lower()
    if mime not in SUPPORTED_REF_MIMES:
        # Last-resort guess from extension
        if file.filename and Path(file.filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            mime = "image/jpeg" if Path(file.filename).suffix.lower() in {".jpg", ".jpeg"} else f"image/{Path(file.filename).suffix.lower().lstrip('.')}"
        else:
            raise HTTPException(400, f"unsupported mime {mime!r}; use JPEG/PNG/WebP")

    # Sanity-check it's actually decodable
    width, height = _image_dims(raw)
    if not width or not height:
        raise HTTPException(400, "file is not a valid image")

    token = secrets.token_urlsafe(12)
    ext = _ext_for_image_bytes(raw)
    dest = UPLOADS_DIR / f"{token}.{ext}"
    dest.write_bytes(raw)

    # Store DB-relative path (mirrors services/storage.py convention)
    backend_root = config.asset_root.parent
    try:
        rel = str(dest.relative_to(backend_root))
    except ValueError:
        rel = str(dest)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_uploads (token, local_path, mime_type, bytes, width, height)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, token, mime_type, bytes, width, height, created_at
                """,
                (token, rel, mime, len(raw), width, height),
            )
            row = await cur.fetchone()
    out = _row(row) or {}
    out["url"] = f"/api/yas-producer/poster-human/upload/{token}/file"
    return out


@router.post("/poster-human/upload-clip")
async def upload_clip(file: UploadFile = File(...)) -> dict:
    """Store a user-uploaded VIDEO clip. Used as a DIRECT intro/outro in Talking
    Head — it bypasses card-gen + Veo and is stitched in as-is. Mirrors
    upload_ref but accepts MP4 / MOV / WebM and skips the image decode."""
    _ensure_dirs()
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    mime = (
        file.content_type
        or mimetypes.guess_type(file.filename or "")[0]
        or "application/octet-stream"
    ).lower()
    ext = SUPPORTED_CLIP_MIMES.get(mime)
    if ext is None:
        # Last-resort guess from extension
        suffix = Path(file.filename or "").suffix.lower()
        guess = {
            ".mp4": ("video/mp4", "mp4"), ".m4v": ("video/mp4", "mp4"),
            ".mov": ("video/quicktime", "mov"), ".webm": ("video/webm", "webm"),
        }.get(suffix)
        if not guess:
            raise HTTPException(400, f"unsupported video {mime!r}; use MP4 / MOV / WebM")
        mime, ext = guess

    token = secrets.token_urlsafe(12)
    dest = UPLOADS_DIR / f"{token}.{ext}"
    dest.write_bytes(raw)

    backend_root = config.asset_root.parent
    try:
        rel = str(dest.relative_to(backend_root))
    except ValueError:
        rel = str(dest)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_uploads (token, local_path, mime_type, bytes, width, height)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, token, mime_type, bytes, width, height, created_at
                """,
                (token, rel, mime, len(raw), None, None),
            )
            row = await cur.fetchone()
    out = _row(row) or {}
    out["url"] = f"/api/yas-producer/poster-human/upload/{token}/file"
    return out


@router.get("/poster-human/upload/{token}/file")
async def get_upload_file(token: str):
    """Serve a previously-uploaded reference image."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT local_path, mime_type FROM yas_uploads WHERE token = %s",
                (token,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "upload not found")
    p = Path(row["local_path"])
    if not p.is_absolute():
        p = config.asset_root.parent / p
    if not p.exists():
        raise HTTPException(410, "upload file missing on disk")
    return FileResponse(str(p), media_type=row["mime_type"] or "image/jpeg", filename=p.name)




class GenerateRequest(BaseModel):
    topic: str
    reference_asset_ids: list[int] = []
    reference_upload_ids: list[int] = []
    # Output shape, the same three Create Poster accepts. The literal rather
    # than DEFAULT_POSTER_ASPECT because poster_pipeline is imported further
    # down this file, after this class body has already been evaluated.
    aspect: str = "1:1"


# ---------------------------------------------------------------------------
# Poster + Human — 4-variant (2x2) two-stage batch. Mirrors Create Poster /
# Human Character, but stores in yas_generations (this tool's table) instead of
# the ystore-backed yas_poster_generations, so every existing /poster-human/*
# endpoint keeps working. Reuses POSTER_HUMAN_STYLE + the shared concept helpers
# (parse_concepts / dedupe_concepts) + gemini_image_multipart + the real logo
# part; the only genuinely new code is the store loop against yas_generations.
# ---------------------------------------------------------------------------

def _clean_poster_aspect(value: Optional[str]) -> str:
    """A shape Create Poster's pipeline knows, or a 400 naming the real ones.

    Poster + Human renders through the same models, so it offers the same three
    and validates them against the same tuple. A silently-corrected shape would
    be worse than a refusal: the caller would get a square poster and no reason
    for it."""
    aspect = (value or DEFAULT_POSTER_ASPECT).strip()
    if aspect not in POSTER_ASPECTS:
        raise HTTPException(400, f"aspect must be one of {', '.join(POSTER_ASPECTS)}")
    return aspect


_ph_variant_col_ready = False


async def _ensure_ph_variant_col() -> None:
    """Idempotently add variant_set_id to yas_generations (shared table). Safe
    ADD COLUMN IF NOT EXISTS — same pattern as yas_talking_head_v2's
    script_embedding column."""
    global _ph_variant_col_ready
    if _ph_variant_col_ready:
        return
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "ALTER TABLE yas_generations "
                    "ADD COLUMN IF NOT EXISTS variant_set_id BIGINT"
                )
        _ph_variant_col_ready = True
    except Exception:
        log.exception("[yas-gen] ensure variant_set_id column failed (non-fatal)")


async def _ph_fail_row(gen_id: int, error: str) -> dict:
    """Mark a variant row failed and return it (the batch tolerates per-variant
    failures instead of aborting the whole grid)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE yas_generations SET status=%s, error=%s, updated_at=NOW() "
                "WHERE id=%s RETURNING *",
                (STATUS_FAILED, (error or "")[:500], gen_id),
            )
            return await cur.fetchone()


def _ph_style_hint(style_attrs: dict | None) -> str:
    """Compact palette/texture/type refinement from the user's reference images,
    appended to the Stage-1 brief so concepts lean toward the refs WITHOUT
    changing the locked house-style anatomy."""
    if not style_attrs:
        return ""
    palette = style_attrs.get("palette") or {}
    bits = [
        b for b in (
            (palette.get("palette_description") or "").strip(),
            (style_attrs.get("texture_and_finish") or "").strip(),
            (style_attrs.get("typography_mood") or "").strip(),
        ) if b
    ]
    if not bits:
        return ""
    return (
        "\n\n[OPTIONAL STYLE REFINEMENT from the user's reference images — tune "
        "palette / texture / type flavour ONLY; keep the house-style anatomy, the "
        "real photographic human hero, and Latin-only text]\n  - "
        + "\n  - ".join(bits)
    )


async def _poster_human_concepts(
    topic: str, n: int, style_attrs: dict | None = None
) -> tuple[list[dict], float]:
    """Stage 1: ask gpt-4o for `n` DIFFERENT compositions of the SAME poster copy
    as strict JSON. Mirrors _poster_concepts / _human_character_concepts with the
    POSTER_HUMAN_STYLE concept system + its own user message. Returns
    (concepts[:n], openai_cost_usd); raises HTTPException on hard failure."""
    from ..services.openai_client import get_meter as _openai_meter
    spend_before = _openai_meter().spent_usd
    brief = topic + _ph_style_hint(style_attrs)

    concepts: list[dict] = []
    attempts = 0
    while len(concepts) < n and attempts < 3:
        attempts += 1
        avoid = [c["summary"] for c in concepts]
        try:
            raw = await asyncio.wait_for(
                openai_chat(
                    build_poster_human_user_message(brief, n, avoid),
                    system=POSTER_HUMAN_STYLE.concepts_system,
                    model="gpt-4o",
                    temperature=0.95,
                    max_tokens=POSTER_CONCEPTS_MAX_TOKENS,
                    response_format={"type": "json_object"},
                ),
                timeout=GEN_TIMEOUT_SEC,
            )
        except OpenAIBudgetExceededError as exc:
            raise HTTPException(503, str(exc))
        except asyncio.TimeoutError:
            if concepts:
                break
            raise HTTPException(504, "concept generation timed out — try again")
        except Exception as exc:
            if concepts:
                break
            log.exception("[yas-gen] poster-human stage 1 (gpt-4o) failed")
            raise HTTPException(502, f"Stage 1 (writing the poster prompt) failed: {exc}")
        concepts = dedupe_concepts(concepts + parse_concepts(raw, n))

    cost = max(0.0, _openai_meter().spent_usd - spend_before)
    if not concepts:
        raise HTTPException(502, "concept generation returned nothing usable")
    return concepts[:n], cost


async def _render_ph_variant(
    spec: dict,
    *,
    topic: str,
    reference_asset_ids: list[int],
    reference_upload_ids: list[int],
    render_directive: str,
    aspect_ratio: Optional[str] = None,
    parent_image_path: Optional[Path] = None,
) -> dict:
    """Render ONE Poster+Human variant into yas_generations. Creates its own
    'generating' row and NEVER raises — failures are recorded on the row and
    returned so a sibling still succeeds. On Explore, `parent_image_path` is sent
    as a reference image part (anchors the riff near the chosen poster)."""
    prompt = (spec.get("prompt") or "").strip()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_generations
                  (topic, mode, reference_asset_ids, reference_upload_ids,
                   parent_generation_id, prompt_used, status, image_model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    topic, "poster_human",
                    reference_asset_ids, reference_upload_ids,
                    spec.get("variation_of"),
                    prompt, STATUS_GENERATING, ENGINE_GEMINI_PRO,
                ),
            )
            gen_id = int((await cur.fetchone())["id"])

    parts: list[tuple[str, Any]] = []
    if POSTER_LOGO_PATH.exists():
        parts.append(("text", LOGO_INSTRUCTION))
        parts.append(("image", POSTER_LOGO_PATH))
    if parent_image_path is not None and Path(parent_image_path).exists():
        parts.append(("text", VARIATION_REF_INSTRUCTION))
        parts.append(("image", Path(parent_image_path)))
    parts.append(("text", f"{render_directive}\n\n{prompt}"))

    try:
        image_bytes = await asyncio.wait_for(
            gemini_image_multipart(
                parts,
                system_instruction=POSTER_HUMAN_STYLE.gemini_stage2_system,
                model=GEMINI_PRO_MODEL,
                location=GEMINI_PRO_LOCATION,
                # The directive in the prompt ASKS; this is the Gemini
                # ImageConfig parameter, which is the only thing that makes the
                # shape come back right. Create Poster learned that already.
                aspect_ratio=aspect_ratio,
            ),
            timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
        )
    except (OpenAIBudgetExceededError, VertexBudgetExceededError) as exc:
        return await _ph_fail_row(gen_id, gen_errors.budget_message(exc))
    except asyncio.TimeoutError:
        return await _ph_fail_row(
            gen_id,
            gen_errors.log_and_explain(log, "poster-human", gen_id,
                                       asyncio.TimeoutError(), what="this poster"))
    except Exception as exc:
        log.exception("[yas-gen] poster-human variant render failed (gen #%d)", gen_id)
        return await _ph_fail_row(
            gen_id,
            gen_errors.log_and_explain(log, "poster-human", gen_id, exc,
                                       what="this poster"))

    if not image_bytes:
        # The call SUCCEEDED and returned nothing, which is what a silent
        # safety refusal looks like from here. "empty image bytes" gave nobody
        # that hint.
        return await _ph_fail_row(
            gen_id,
            "The image model answered but sent no picture back. That usually "
            "means it quietly refused the prompt or the student photo — try "
            "rewording the topic, or use a different photo.")

    ext = _ext_for_image_bytes(image_bytes)
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/png")
    width, height = _image_dims(image_bytes)
    try:
        up = await asset_storage.upload(
            data=image_bytes, path=f"yas/generations/{gen_id}.{ext}", mime=mime,
        )
    except Exception:
        log.exception("[yas-gen] variant upload failed; local only")
        up = {"storage_bucket": None, "storage_path": None, "local_path": None}

    local_dest = GEN_DIR / f"{gen_id}.{ext}"
    local_dest.write_bytes(image_bytes)
    try:
        local_rel = str(local_dest.relative_to(config.asset_root.parent))
    except ValueError:
        local_rel = str(local_dest)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations SET
                  storage_bucket=%s, storage_path=%s, local_path=%s,
                  mime_type=%s, bytes=%s, width=%s, height=%s,
                  status=%s, updated_at=NOW()
                 WHERE id=%s
                RETURNING *
                """,
                (
                    up.get("storage_bucket"), up.get("storage_path"), local_rel,
                    mime, len(image_bytes), width, height, STATUS_READY, gen_id,
                ),
            )
            return await cur.fetchone()


async def _finalize_ph_variants(
    recs: list[dict],
    *,
    stage1_cost_usd: float,
    spend_before_vertex: float,
    spend_before_openai: float,
) -> list[dict]:
    """Shared finalizer for BOTH Poster+Human grids (style_inspired + template):
    split render + Stage-1 cost across the READY variants, stamp a shared
    variant_set_id (= smallest id in the batch), and return records via
    _gen_row_to_dict. Mirrors Create Poster's _run_stage2_batch tail."""
    from ..services.openai_client import get_meter as _openai_meter
    total_render = max(0.0, (get_meter().spent_usd - spend_before_vertex)
                       + (_openai_meter().spent_usd - spend_before_openai))
    ok = [r for r in recs if r and r.get("status") == STATUS_READY]
    per_image = round((stage1_cost_usd + total_render) / max(len(ok), 1), 6)
    variant_set_id = min((int(r["id"]) for r in recs if r), default=None)

    out: list[dict] = []
    for r in recs:
        if not r:
            continue
        gen_id = int(r["id"])
        cost = per_image if r.get("status") == STATUS_READY else r.get("cost_usd")
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE yas_generations SET variant_set_id=%s, cost_usd=%s, "
                    "updated_at=NOW() WHERE id=%s RETURNING *",
                    (variant_set_id, cost, gen_id),
                )
                fresh = await cur.fetchone()
        out.append(_gen_row_to_dict(fresh or r))
    log.info(
        "[yas-gen] poster-human batch · %d/%d ready · variant_set=%s · $%.4f",
        len(ok), len(recs), variant_set_id, stage1_cost_usd + total_render,
    )
    return out


async def _render_ph_template_variant(
    *,
    topic: str,
    prompt: str,
    template_path: Path,
    student_path: Path,
    reference_asset_ids: list[int],
    reference_upload_ids: list[int],
    student_upload_id: Optional[int],
    parent_generation_id: Optional[int],
    aspect: str = "1:1",
) -> dict:
    """Render ONE template-fill variant into yas_generations (template + student
    face via gemini_image multi-image fusion). Creates its own 'generating' row
    and NEVER raises — failures are recorded so a sibling still succeeds."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_generations
                  (topic, mode, reference_asset_ids, reference_upload_ids,
                   parent_generation_id, student_upload_id, prompt_used, status, image_model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    topic, "template_fill",
                    reference_asset_ids, reference_upload_ids,
                    parent_generation_id, student_upload_id,
                    prompt, STATUS_GENERATING, ENGINE_GEMINI_PRO,
                ),
            )
            gen_id = int((await cur.fetchone())["id"])
    try:
        image_bytes = await asyncio.wait_for(
            gemini_image(
                prompt,
                source_images=[template_path, student_path],
                model=GEMINI_PRO_MODEL,
                location=GEMINI_PRO_LOCATION,
                # Stated explicitly because `gemini_image` defaults to 16:9:
                # every template fill was asking for a WIDE image while the
                # grid drew it in a square cell.
                aspect_ratio=aspect,
            ),
            timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
        )
    except (OpenAIBudgetExceededError, VertexBudgetExceededError) as exc:
        return await _ph_fail_row(gen_id, gen_errors.budget_message(exc))
    except asyncio.TimeoutError:
        return await _ph_fail_row(
            gen_id,
            gen_errors.log_and_explain(log, "template-fill", gen_id,
                                       asyncio.TimeoutError(), what="this poster"))
    except Exception as exc:
        log.exception("[yas-tmpl] variant render failed (gen #%d)", gen_id)
        return await _ph_fail_row(
            gen_id,
            gen_errors.log_and_explain(log, "template-fill", gen_id, exc,
                                       what="this poster"))
    if not image_bytes:
        # The call SUCCEEDED and returned nothing, which is what a silent
        # safety refusal looks like from here. "empty image bytes" gave nobody
        # that hint.
        return await _ph_fail_row(
            gen_id,
            "The image model answered but sent no picture back. That usually "
            "means it quietly refused the prompt or the student photo — try "
            "rewording the topic, or use a different photo.")

    ext = _ext_for_image_bytes(image_bytes)
    mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/png")
    width, height = _image_dims(image_bytes)
    try:
        up = await asset_storage.upload(
            data=image_bytes, path=f"yas/generations/{gen_id}.{ext}", mime=mime,
        )
    except Exception:
        log.exception("[yas-tmpl] variant upload failed; local only")
        up = {"storage_bucket": None, "storage_path": None, "local_path": None}
    local_dest = GEN_DIR / f"{gen_id}.{ext}"
    local_dest.write_bytes(image_bytes)
    try:
        local_rel = str(local_dest.relative_to(config.asset_root.parent))
    except ValueError:
        local_rel = str(local_dest)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations SET
                  storage_bucket=%s, storage_path=%s, local_path=%s,
                  mime_type=%s, bytes=%s, width=%s, height=%s, status=%s, updated_at=NOW()
                 WHERE id=%s
                RETURNING *
                """,
                (
                    up.get("storage_bucket"), up.get("storage_path"), local_rel,
                    mime, len(image_bytes), width, height, STATUS_READY, gen_id,
                ),
            )
            return await cur.fetchone()


async def _poster_human_batch(
    specs: list[dict],
    *,
    topic: str,
    reference_asset_ids: list[int],
    reference_upload_ids: list[int],
    stage1_cost_usd: float,
    aspect: str = "1:1",
    parent_image_path: Optional[Path] = None,
) -> list[dict]:
    """Render each concept spec into yas_generations SEQUENTIALLY (avoids the
    Gemini parallel-submit 429), then finalize. Adapts Create Poster's
    _run_stage2_batch to this tool's yas_generations table."""
    await _ensure_ph_variant_col()
    from ..services.openai_client import get_meter as _openai_meter
    spend_before_vertex = get_meter().spent_usd
    spend_before_openai = _openai_meter().spent_usd

    recs: list[dict] = []
    for spec in specs:
        recs.append(await _render_ph_variant(
            spec,
            topic=topic,
            reference_asset_ids=reference_asset_ids,
            reference_upload_ids=reference_upload_ids,
            render_directive=_render_directive_for(aspect),
            aspect_ratio=aspect,
            parent_image_path=parent_image_path,
        ))
    return await _finalize_ph_variants(
        recs,
        stage1_cost_usd=stage1_cost_usd,
        spend_before_vertex=spend_before_vertex,
        spend_before_openai=spend_before_openai,
    )


@router.post("/poster-human/generate")
async def generate(body: GenerateRequest) -> dict:
    """Generate a 2x2 batch of 4 info-poster variants (style_inspired) via the
    two-stage pipeline: Stage-1 gpt-4o casts 4 DIFFERENT compositions of the SAME
    poster copy; Stage-2 Nano Banana renders each (real 42 logo sent as an image
    part). Returns {items: [...4 records]}."""
    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required")
    n_refs = len(body.reference_asset_ids) + len(body.reference_upload_ids)
    if n_refs > MAX_REFERENCES:
        raise HTTPException(400, f"at most {MAX_REFERENCES} references allowed")
    aspect = _clean_poster_aspect(body.aspect)

    await _budget_guard()
    _ensure_dirs()

    # Optional reference fingerprint (text-only style memory — the refs are never
    # shown to the renderer as images; the model can't copy a layout it can't see).
    asset_paths = await _resolve_asset_paths(body.reference_asset_ids)
    upload_paths = await _resolve_upload_paths(body.reference_upload_ids)
    style_attrs = await _extract_style_attributes(asset_paths + upload_paths)

    # Stage 1 — 4 different compositions of the SAME copy.
    concepts, stage1_cost = await _poster_human_concepts(
        topic, POSTER_VARIANT_COUNT, style_attrs
    )
    specs = [{"prompt": c["prompt"], "concept": c["summary"]} for c in concepts]
    log.info("[yas-gen] poster-human stage1 ok · %d composition(s)", len(specs))

    # Stage 2 — render each variant (sequential; shared variant_set_id).
    items = await _poster_human_batch(
        specs,
        topic=topic,
        reference_asset_ids=body.reference_asset_ids,
        reference_upload_ids=body.reference_upload_ids,
        stage1_cost_usd=stage1_cost,
        aspect=aspect,
    )
    asyncio.create_task(_cleanup_old_uploads())
    return {"items": items}


@router.post("/poster-human/{gen_id}/remove-logo")
async def poster_human_remove_logo(gen_id: int) -> dict:
    """Re-render a finished Poster+Human image with the 42 logo removed, saved as a
    NEW child generation next to the original (kept for compare / re-roll). Feeds the
    image back through the shared ``card_animation.render_delogo_image`` (NO logo
    part). Instruction-only edit — the rest stays very close but not pixel-identical."""
    await _budget_guard()
    _ensure_dirs()

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, topic, mode, status, storage_bucket, storage_path,
                       local_path, width, height
                  FROM yas_generations WHERE id = %s
                """,
                (int(gen_id),),
            )
            parent = await cur.fetchone()
    # Only a Poster+Human still (mode NULL / 'poster_human'); never a TH/VE row.
    if not parent or parent.get("mode") not in (None, "poster_human"):
        raise HTTPException(404, "poster not found")
    if parent.get("status") not in (STATUS_READY, STATUS_SAVED):
        raise HTTPException(422, f"cannot remove logo — status is '{parent.get('status')}'")

    src_bytes = await _media_bytes(
        storage_bucket=parent.get("storage_bucket"),
        storage_path=parent.get("storage_path"),
        local_path=parent.get("local_path"),
    )
    if not src_bytes:
        raise HTTPException(410, "poster image not retrievable")

    import tempfile as _tf
    _sext = _ext_for_image_bytes(src_bytes)
    src_img: Path | None = None
    try:
        with _tf.NamedTemporaryFile(suffix=f".{_sext}", delete=False) as _stf:
            _stf.write(src_bytes)
            src_img = Path(_stf.name)

        # New "generating" child row (parent linked for lineage).
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO yas_generations
                      (topic, parent_generation_id, prompt_used, status, image_model)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (
                        parent.get("topic"),
                        gen_id,
                        "[remove-logo edit]",
                        STATUS_GENERATING,
                        ENGINE_GEMINI_PRO,
                    ),
                )
                new_row = await cur.fetchone()
                new_id = int(new_row["id"])

        spend_before = get_meter().spent_usd
        try:
            image_bytes = await card_animation.render_delogo_image(
                src_img, timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
                # A child that silently comes back square is the feature half
                # built, so the shape is read off the parent's own pixels.
                aspect_ratio=_poster_aspect_of(parent),
            )
        except (OpenAIBudgetExceededError, VertexBudgetExceededError) as e:
            await _mark_failed(new_id, f"budget_exceeded: {e}")
            raise HTTPException(503, "Image budget exhausted mid-generation")
        except asyncio.TimeoutError:
            await _mark_failed(new_id, "timeout")
            raise HTTPException(504, detail={"error": "timeout", "retry_safe": True})
        except HTTPException:
            raise
        except GeminiNoImageError as e:
            await _mark_failed(new_id, f"no_image: {e}"[:500])
            raise _http_for_no_image(e, action="remove the logo")
        except Exception as e:
            if _is_resource_exhausted(e):
                await _mark_failed(new_id, f"rate_limited: {e}"[:500])
                raise HTTPException(429, "Image generation is rate-limited right now (quota) — wait a minute and try again.")
            log.exception("[yas-gen] remove-logo failed")
            await _mark_failed(new_id, str(e)[:500])
            raise HTTPException(502, f"remove-logo failed: {str(e)[:200]}")

        if not image_bytes:
            await _mark_failed(new_id, "empty image bytes")
            raise HTTPException(502, "remove-logo returned empty image")

        width, height = _image_dims(image_bytes)
        ext = _ext_for_image_bytes(image_bytes)
        mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "image/png")
        bucket_path = f"yas/generations/{new_id}.{ext}"
        try:
            upload_result = await asset_storage.upload(data=image_bytes, path=bucket_path, mime=mime)
        except Exception:
            log.exception("yas-gen remove-logo upload failed; saving locally only")
            upload_result = {"storage_bucket": None, "storage_path": None, "local_path": None}

        local_dest = GEN_DIR / f"{new_id}.{ext}"
        local_dest.write_bytes(image_bytes)
        backend_root = config.asset_root.parent
        try:
            local_rel = str(local_dest.relative_to(backend_root))
        except ValueError:
            local_rel = str(local_dest)

        # Real metered Vertex spend for this render (matches the poster/animation
        # remove-logo routes), not a hardcoded estimate.
        cost = round(max(0.0, get_meter().spent_usd - spend_before), 6)
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE yas_generations SET
                      storage_bucket=%s, storage_path=%s, local_path=%s,
                      mime_type=%s, bytes=%s, width=%s, height=%s,
                      status=%s, cost_usd=%s, updated_at=NOW()
                     WHERE id=%s
                    RETURNING *
                    """,
                    (
                        upload_result.get("storage_bucket"),
                        upload_result.get("storage_path"),
                        local_rel,
                        mime, len(image_bytes), width, height,
                        STATUS_READY, cost, new_id,
                    ),
                )
                row = await cur.fetchone()
        log.info("[yas-gen] remove-logo #%d → child #%d ready", gen_id, new_id)
        return _gen_row_to_dict(row)
    finally:
        if src_img is not None:
            src_img.unlink(missing_ok=True)


async def _mark_failed(gen_id: int, error: str) -> None:
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE yas_generations SET status=%s, error=%s, updated_at=NOW() WHERE id=%s",
                    (STATUS_FAILED, error[:500], gen_id),
                )
    except Exception:
        log.exception("could not mark yas_generation #%d failed", gen_id)


class TemplateFillRequest(BaseModel):
    topic: str
    template_asset_id: int       # producer_assets row used as the design template
    # The student face comes from EITHER an uploaded photo (yas_uploads row) OR a
    # saved Human Character asset (producer_assets row, content_category
    # 'img_human_character'). Exactly one must be provided.
    student_upload_id: Optional[int] = None
    student_asset_id: Optional[int] = None
    parent_generation_id: Optional[int] = None
    feedback_text: Optional[str] = None
    aspect: str = "1:1"


@router.post("/poster-human/template-fill")
async def template_fill(body: TemplateFillRequest) -> dict:
    """Compose a poster by using a saved poster as a literal TEMPLATE and
    dropping a SPECIFIC STUDENT's face into it. Uses Gemini 2.5 Flash Image
    (Nano Banana) — it natively accepts multiple source images and is the
    right model for image-to-image fusion. The student's face is preserved
    via prompt language + the model's native subject-locking behaviour.

    Distinct from the style_inspired /generate endpoint: that one strips
    references into text-only DNA. This one PASSES the images directly.
    """
    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required")
    aspect = _clean_poster_aspect(body.aspect)

    # Retry depth cap — same rule as style_inspired
    if body.parent_generation_id is not None:
        depth = await _retry_depth(int(body.parent_generation_id))
        if depth >= MAX_RETRY_DEPTH:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "retry_depth_exceeded",
                    "depth": depth,
                    "max": MAX_RETRY_DEPTH,
                    "hint": "Start over to begin a new chain.",
                },
            )
        if not (body.feedback_text or "").strip():
            raise HTTPException(400, "feedback_text required on retry")

    await _budget_guard()
    _ensure_dirs()

    # Resolve template (must be an asset row)
    template_paths = await _resolve_asset_paths([body.template_asset_id])
    if not template_paths:
        raise HTTPException(404, f"template asset #{body.template_asset_id} not found")
    template_path = template_paths[0]

    # Resolve student — either an uploaded photo (yas_uploads) or a saved Human
    # Character asset (producer_assets). Exactly one is required.
    if body.student_asset_id is not None:
        student_paths = await _resolve_asset_paths([body.student_asset_id])
        if not student_paths:
            raise HTTPException(404, f"student asset #{body.student_asset_id} not found")
    elif body.student_upload_id is not None:
        student_paths = await _resolve_upload_paths([body.student_upload_id])
        if not student_paths:
            raise HTTPException(404, f"student upload #{body.student_upload_id} not found")
    else:
        raise HTTPException(400, "a student photo (upload) or a Human Character asset is required")
    student_path = student_paths[0]

    prompt = build_template_fill_prompt(topic=topic, feedback=body.feedback_text)

    # A student from an asset rides in reference_asset_ids[1] (template is [0]);
    # an uploaded student stays in reference_upload_ids / student_upload_id.
    student_from_asset = body.student_asset_id is not None
    ref_asset_ids = [body.template_asset_id] + ([body.student_asset_id] if student_from_asset else [])
    ref_upload_ids = [] if student_from_asset else [body.student_upload_id]

    log.info(
        "[yas-tmpl] topic=%r template=%d student=%s(%s) -> %d variants",
        topic[:60], body.template_asset_id,
        body.student_asset_id if student_from_asset else body.student_upload_id,
        "asset" if student_from_asset else "upload", POSTER_VARIANT_COUNT,
    )

    # 4-variant batch: no concept stage (the template IS the design) — render the
    # SAME template+face prompt N times sequentially, sharing a variant_set_id.
    await _ensure_ph_variant_col()
    from ..services.openai_client import get_meter as _openai_meter
    spend_before_vertex = get_meter().spent_usd
    spend_before_openai = _openai_meter().spent_usd

    recs = [
        await _render_ph_template_variant(
            topic=topic,
            prompt=prompt,
            template_path=template_path,
            student_path=student_path,
            reference_asset_ids=ref_asset_ids,
            reference_upload_ids=ref_upload_ids,
            student_upload_id=body.student_upload_id,
            parent_generation_id=body.parent_generation_id,
            aspect=aspect,
        )
        for _ in range(POSTER_VARIANT_COUNT)
    ]
    items = await _finalize_ph_variants(
        recs, stage1_cost_usd=0.0,
        spend_before_vertex=spend_before_vertex,
        spend_before_openai=spend_before_openai,
    )
    asyncio.create_task(_cleanup_old_uploads())
    return {"items": items}


def _asset_slug(name: str | None, fallback: str) -> str:
    """Slug for a producer_assets row, also used as its object key stem. ASCII
    only: Arabic, spaces or '+' in a key break the signed /media URL or are
    refused by the store."""
    return f"{asset_storage.ascii_slug(name, fallback=fallback)}_{secrets.token_hex(3)}"


async def _thumb_attributes(image_bytes: bytes, upload_result: dict, mime: str) -> dict:
    """producer_assets.attributes for a freshly uploaded image: the key of its
    stored 480px thumbnail, when the image went to a bucket and one could be
    made. On S3 that derivative is the grid's only small tile (there is no image
    transformer), so a row without it shows the full original."""
    key = await thumbs.ensure_thumb_object(
        raw=image_bytes,
        storage_path=upload_result.get("storage_path"),
        storage_bucket=upload_result.get("storage_bucket"),
        mime=mime,
    )
    return {"_thumb_path": key} if key else {}


class SaveToAssetsRequest(BaseModel):
    display_name: Optional[str] = None


@router.post("/poster-human/{gen_id}/save-to-assets")
async def save_to_assets(gen_id: int, body: SaveToAssetsRequest) -> dict:
    """Copy a generated poster into the Assets library as img_poster_human."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, topic, status, saved_asset_id, prompt_used,
                       local_path, storage_bucket, storage_path, mime_type
                  FROM yas_generations WHERE id=%s
                """,
                (gen_id,),
            )
            gen = await cur.fetchone()
    if not gen:
        raise HTTPException(404, "generation not found")
    if gen["status"] == STATUS_SAVED:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_saved",
                "existing_asset_id": gen.get("saved_asset_id"),
            },
        )
    if gen["status"] != STATUS_READY:
        raise HTTPException(
            422, f"cannot save a generation with status={gen['status']}"
        )

    # Read image bytes from local disk (preferred) or bucket
    image_bytes: bytes | None = None
    if gen.get("local_path"):
        p = Path(gen["local_path"])
        if not p.is_absolute():
            p = config.asset_root.parent / p
        if p.exists():
            image_bytes = p.read_bytes()
    if image_bytes is None and gen.get("storage_bucket") and gen.get("storage_path"):
        # internal=True: the backend GETs it itself and cannot reach the public
        # /media origin.
        signed = await asset_storage.url_for(
            storage_bucket=gen["storage_bucket"],
            storage_path=gen["storage_path"],
            local_path=None, expires=600, internal=True,
        )
        if signed:
            import httpx
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(signed)
                if r.status_code < 300:
                    image_bytes = r.content
    if image_bytes is None:
        raise HTTPException(410, "generated image bytes are not retrievable")

    mime = gen.get("mime_type") or "image/png"
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "png")

    # Describe + embed for RAG. Without this, saved posters are search-dark
    # for the Producer agent.
    ai_description = ""
    try:
        sys_p, user_p = describe_poster_prompt()
        # Write the bytes to a temp file for gemini_multimodal (it accepts paths)
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
            tf.write(image_bytes)
            tmp_path = Path(tf.name)
        try:
            ai_description = await gemini_multimodal(
                user_p,
                images=[tmp_path],
                system=sys_p,
                model=config.vertex_gemini_fast_model,
                temperature=0.3,
                max_tokens=600,
            ) or ""
            ai_description = str(ai_description).strip()
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception:
        log.exception("describe-poster step failed; saving with fallback description")
        ai_description = (
            f"Yas-generated poster on topic: {gen['topic'][:200]}"
        )

    embedding = None
    if ai_description:
        try:
            vecs = await gemini_embed([ai_description])
            if vecs and vecs[0]:
                embedding = vecs[0]
        except Exception:
            log.exception("embedding failed; saving without embedding")

    # Compose a slug + bucket path for the asset library
    slug = _asset_slug(body.display_name or gen["topic"], f"yas_{gen_id}")
    bucket_path = f"instagram/img_poster_human/{slug}.{ext}"

    upload_result = await asset_storage.upload(
        data=image_bytes, path=bucket_path, mime=mime,
    )
    attributes = await _thumb_attributes(image_bytes, upload_result, mime)

    width, height = _image_dims(image_bytes)
    display_name = (body.display_name or gen["topic"][:80] or f"Yas poster #{gen_id}")
    metadata = {
        "content_category": "img_poster_human",
        "source": "yas_producer",
        "yas_generation_id": gen_id,
        "topic": gen["topic"],
        "prompt_used": gen["prompt_used"][:2000],
    }

    # Insert producer_assets row. The save-once gate is the
    # yas_generations.status check above — a concurrent second save sees
    # status='saved_to_assets' and 409s.
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name, ai_description,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes, width, height,
                   embedding, metadata, attributes, enabled)
                VALUES (%s, 'image', 'free', %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, TRUE)
                RETURNING id, slug, display_name, storage_bucket, storage_path,
                          local_path, mime_type, bytes, width, height,
                          ai_description, metadata, created_at,
                          (embedding IS NOT NULL) AS embedded
                """,
                (
                    slug, display_name, ai_description or None,
                    upload_result.get("storage_bucket"),
                    upload_result.get("storage_path"),
                    upload_result.get("local_path"),
                    mime, len(image_bytes), width, height,
                    embedding, Jsonb(metadata), Jsonb(attributes),
                ),
            )
            asset_row = await cur.fetchone()

            # Mark the generation saved
            await cur.execute(
                """
                UPDATE yas_generations
                   SET status = %s, saved_asset_id = %s, updated_at = NOW()
                 WHERE id = %s
                """,
                (STATUS_SAVED, int(asset_row["id"]), gen_id),
            )

    out = _row(asset_row) or {}
    if asset_row.get("storage_bucket") and asset_row.get("storage_path"):
        try:
            out["signed_url"] = await asset_storage.url_for(
                storage_bucket=asset_row["storage_bucket"],
                storage_path=asset_row["storage_path"],
                local_path=asset_row.get("local_path"),
            )
        except Exception:
            out["signed_url"] = None
    return out


@router.get("/poster-human/{gen_id}/file")
async def get_generation_file(gen_id: int, download: bool = False):
    """Stream a generated poster image. `?download=1` forces a save-to-disk
    (Content-Disposition: attachment) — used by the Library "Download image" button."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT local_path, storage_bucket, storage_path, mime_type FROM yas_generations WHERE id=%s",
                (gen_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "generation not found")
    mime = row.get("mime_type") or "image/png"
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    dl_name = f"poster-human-{gen_id}.{ext}"
    headers = _MEDIA_CACHE_HEADERS
    if download:
        headers = {**_MEDIA_CACHE_HEADERS, "Content-Disposition": f'attachment; filename="{dl_name}"'}
    if row.get("local_path"):
        p = Path(row["local_path"])
        if not p.is_absolute():
            p = config.asset_root.parent / p
        if p.exists():
            return FileResponse(
                str(p), media_type=mime, filename=(dl_name if download else p.name),
                headers=headers,
            )
    if row.get("storage_bucket") and row.get("storage_path"):
        # REDIRECT rather than download-and-buffer. This used to sign with
        # expires=600 (which collapses the signed-URL cache to its 30s floor, so
        # the sign round trip repeated constantly), open a fresh httpx client per
        # request, and push the whole image through backend RAM.
        # The download name is signed in by url_for; never append to the URL.
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=row["storage_path"],
            local_path=None, expires=_MEDIA_SIGN_TTL,
            download_name=dl_name if download else None,
        )
        if signed:
            return RedirectResponse(
                signed, status_code=307, headers=_REDIRECT_CACHE_HEADERS,
            )
    raise HTTPException(410, "generation file not retrievable")


@router.get("/poster-human/animations/{anim_id}/file")
async def get_poster_human_animation_file(anim_id: int, download: bool = False):
    """Stream a Poster+Human animation MP4.

    This route was REFERENCED in three places (the library tile's video_url and
    download_url, and the hydrated `animation.file_url`) but never defined, so
    every animated Poster+Human tile 404'd.

    It must read `yas_animations` — NOT the Create-Animation store. The library's
    `anim_id` comes from yas_animations, while /animation/{id}/file resolves via
    yas_animation_generations: two different tables with two INDEPENDENT SERIAL
    sequences, so aliasing the routes would serve an unrelated video under a
    Poster+Human tile for any colliding id.
    """
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, local_path, storage_bucket, storage_path "
                "  FROM yas_animations WHERE id = %s",
                (anim_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "animation not found")
    resp = await _media_response(
        local_path=row.get("local_path"),
        storage_bucket=row.get("storage_bucket"),
        storage_path=row.get("storage_path"),
        mime="video/mp4",
        download_name=f"poster-human-animation-{anim_id}.mp4" if download else None,
    )
    if resp is None:
        raise HTTPException(410, "animation file not retrievable")
    return resp


@router.get("/poster-human/recent")
async def list_recent() -> dict:
    """Recent Poster+Human generations for session rehydrate (last 3 days, capped).

    LEFT JOINs the latest animation per generation so a refresh mid-animation
    (or after the animation finished server-side) restores the right state.
    Scoped to Poster+Human rows only (mode NULL / 'poster_human' /
    'template_fill') so Talking Head and other yas_generations rows never leak in.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH latest_anim AS (
                    SELECT DISTINCT ON (yas_generation_id)
                           yas_generation_id, id, status, veo_model,
                           duration_sec, with_audio, aspect_ratio,
                           cost_usd, error, created_at, updated_at
                      FROM yas_animations
                     ORDER BY yas_generation_id, created_at DESC
                )
                SELECT g.id, g.topic, g.status, g.parent_generation_id,
                       g.feedback_text, g.retry_kind, g.mime_type, g.bytes,
                       g.width, g.height, g.cost_usd, g.error,
                       g.created_at, g.updated_at,
                       g.reference_asset_ids, g.reference_upload_ids,
                       g.saved_asset_id, g.mode, g.student_upload_id,
                       g.image_model, g.variant_set_id,
                       a.id            AS anim_id,
                       a.status        AS anim_status,
                       a.veo_model     AS anim_veo_model,
                       a.duration_sec  AS anim_duration_sec,
                       a.with_audio    AS anim_with_audio,
                       a.aspect_ratio  AS anim_aspect_ratio,
                       a.cost_usd      AS anim_cost_usd,
                       a.error         AS anim_error,
                       a.created_at    AS anim_created_at,
                       a.updated_at    AS anim_updated_at
                  FROM yas_generations g
                  LEFT JOIN latest_anim a
                    ON a.yas_generation_id = g.id
                 WHERE g.created_at >= now() - make_interval(hours => %s)
                   AND g.status IN ('ready', 'failed', 'saved_to_assets')
                   AND (g.mode IS NULL OR g.mode IN ('poster_human', 'template_fill'))
                 ORDER BY g.created_at DESC
                 LIMIT 50
                """,
                (HISTORY_WINDOW_HOURS,),
            )
            rows = await cur.fetchall()
    items = [_gen_row_to_dict(r) for r in rows]
    return {"items": items}


@router.get("/poster-human/{gen_id}")
async def get_generation(gen_id: int) -> dict:
    """Full metadata for ONE poster+human generation (Library "Continue editing").

    Single-row twin of /recent — same SELECT + latest-animation LEFT JOIN and the
    same `_gen_row_to_dict` shape, so the creator rehydrates straight into its
    ready/refine state. No age/status filter (resume must work for old rows).
    Declared AFTER /poster-human/recent so the literal route wins over {gen_id}.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH latest_anim AS (
                    SELECT DISTINCT ON (yas_generation_id)
                           yas_generation_id, id, status, veo_model,
                           duration_sec, with_audio, aspect_ratio,
                           cost_usd, error, created_at, updated_at
                      FROM yas_animations
                     ORDER BY yas_generation_id, created_at DESC
                )
                SELECT g.id, g.topic, g.status, g.parent_generation_id,
                       g.feedback_text, g.retry_kind, g.mime_type, g.bytes,
                       g.width, g.height, g.cost_usd, g.error,
                       g.created_at, g.updated_at,
                       g.reference_asset_ids, g.reference_upload_ids,
                       g.saved_asset_id, g.mode, g.student_upload_id,
                       g.image_model, g.variant_set_id,
                       a.id            AS anim_id,
                       a.status        AS anim_status,
                       a.veo_model     AS anim_veo_model,
                       a.duration_sec  AS anim_duration_sec,
                       a.with_audio    AS anim_with_audio,
                       a.aspect_ratio  AS anim_aspect_ratio,
                       a.cost_usd      AS anim_cost_usd,
                       a.error         AS anim_error,
                       a.created_at    AS anim_created_at,
                       a.updated_at    AS anim_updated_at
                  FROM yas_generations g
                  LEFT JOIN latest_anim a
                    ON a.yas_generation_id = g.id
                 WHERE g.id = %s
                """,
                (gen_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "generation not found")
    # Defensive: this endpoint shapes the row as a poster+human generation, so
    # reject ids that actually belong to a talking-head / video-editor run
    # (those share the yas_generations table but a different `mode`).
    if row.get("mode") in (
        th_service.MODE_PREVIEW, th_service.MODE_INTRO,
        th_service.MODE_OUTRO, th_service.MODE_SCENE,
        th_v2_service.MODE_PREVIEW, th_v2_service.MODE_INTRO,
        th_v2_service.MODE_OUTRO, th_v2_service.MODE_SCENE,
        ve_service.MODE_PREVIEW,
    ):
        raise HTTPException(404, "generation not found")
    return _gen_row_to_dict(row)


class PosterHumanVariantsRequest(BaseModel):
    parent_id: int


@router.post("/poster-human/variants")
async def poster_human_variants(body: PosterHumanVariantsRequest) -> dict:
    """Explore: 4 close riffs of a chosen Poster+Human poster, anchored on its
    image (mirrors /poster/variants). Reuses POSTER_HUMAN_STYLE.variation_nudges
    + build_variation_user_message + the batch render (parent poster sent as a
    reference part). Works for both style_inspired and template_fill parents."""
    await _budget_guard()
    _ensure_dirs()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, topic, status, mode, prompt_used,
                       reference_asset_ids, reference_upload_ids,
                       storage_bucket, storage_path, local_path,
                       width, height
                  FROM yas_generations WHERE id = %s
                """,
                (body.parent_id,),
            )
            parent = await cur.fetchone()
    if not parent:
        raise HTTPException(404, "parent generation not found")
    if parent.get("mode") not in (None, "poster_human", "template_fill"):
        raise HTTPException(422, "not a Poster+Human generation")
    if parent.get("status") not in (STATUS_READY, STATUS_SAVED):
        raise HTTPException(422, f"cannot explore — status is '{parent.get('status')}'")

    # Pull the parent poster to a temp file so it can be sent as a variation
    # reference part (anchors each riff NEAR the chosen poster).
    img_bytes = await _media_bytes(
        storage_bucket=parent.get("storage_bucket"),
        storage_path=parent.get("storage_path"),
        local_path=parent.get("local_path"),
    )
    if not img_bytes:
        raise HTTPException(410, "parent image is no longer available")
    cache_dir = YAS_ROOT / "parents_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    parent_img = cache_dir / f"explore-{body.parent_id}.png"
    parent_img.write_bytes(img_bytes)

    parent_topic = (parent.get("topic") or "").strip()
    base_prompt = (parent.get("prompt_used") or parent_topic or "").strip()
    nudges = POSTER_HUMAN_STYLE.variation_nudges or VARIATION_NUDGES
    specs = [
        {
            "prompt": build_variation_user_message(base_prompt, nudges[i % len(nudges)]),
            "concept": f"variation {i + 1}",
            "variation_of": body.parent_id,
        }
        for i in range(POSTER_VARIANT_COUNT)
    ]
    try:
        items = await _poster_human_batch(
            specs,
            topic=parent_topic,
            reference_asset_ids=parent.get("reference_asset_ids") or [],
            reference_upload_ids=parent.get("reference_upload_ids") or [],
            stage1_cost_usd=0.0,
            aspect=_poster_aspect_of(parent),
            parent_image_path=parent_img,
        )
    finally:
        try:
            parent_img.unlink(missing_ok=True)
        except Exception:
            pass
    return {"items": items}


# ---------------------------------------------------------------------------
# /talking-head — third Yas Producer category.
#
# Owns the multi-step flow: topic → GPT-4o script → 3-way image previews
# → Veo + Remotion composition → final ~12s MP4. The orchestration lives in
# `services/yas_talking_head.py`; these routes are thin HTTP wrappers.
# ---------------------------------------------------------------------------

from ..services import yas_talking_head as th_service  # noqa: E402
from ..services.yas_talking_head import TalkingHeadRefs  # noqa: E402


class TalkingHeadImageModels(BaseModel):
    main: Optional[str] = None    # 'openai' | 'gemini_pro'
    intro: Optional[str] = None
    outro: Optional[str] = None


# Talking Head v2 framing (how close the camera is): the run's, or a scene's.
# A scene's may also be "inherit" (drop its own; follow the run's). Anything
# else is refused (422).
THV2Framing = Optional[Literal["close", "above_hip", "full_body"]]
THV2SceneFraming = Optional[Literal["close", "above_hip", "full_body", "inherit"]]


class TalkingHeadSceneRefOverride(BaseModel):
    """Per-scene override for the global character / bg refs. Any field left
    null inherits from the run-level reference. Used by multi-scene runs."""
    character_asset_id: Optional[int] = None
    character_upload_id: Optional[int] = None
    bg_asset_id: Optional[int] = None
    bg_upload_id: Optional[int] = None
    # v2 one-shot intent: when this scene has NO bg ref, "new" asks the editor
    # to regenerate a fresh background while keeping the character; "keep"
    # (default) passes the existing still through untouched. Not persisted.
    bg_mode: Optional[str] = None


class TalkingHeadGenerateScriptRequest(BaseModel):
    topic: str
    # Optional user-authored dialogue. When set, GPT-4o uses it verbatim as
    # `script.dialogue` (only normalising trailing whitespace and curly
    # quotes) and derives `script.hook` from its opening words. Every other
    # field is still generated from the topic.
    user_dialogue: Optional[str] = None
    # v6 — multi-scene reels. scene_count is 1..5. When > 1, the script's
    # `scenes` array gets that many entries; user_dialogues (length ==
    # scene_count) provides per-scene verbatim text (None / empty → GPT
    # writes). scene_overrides lets each scene override the global
    # character + bg refs.
    scene_count: int = 1
    user_dialogues: Optional[list[Optional[str]]] = None
    scene_overrides: Optional[list[Optional[TalkingHeadSceneRefOverride]]] = None
    # Per-tile model selection. When the dict is None or any field is None
    # the service falls back to 'openai' for that tile.
    image_models: Optional[TalkingHeadImageModels] = None
    # Legacy single-model field — when set, applies to all three tiles.
    # Frontend should prefer `image_models` going forward.
    image_model: Optional[str] = None
    lock_character: bool = True
    # Each slot can be either a saved asset OR a freshly-uploaded file.
    # Only one should be set per slot; uploads win if both arrive.
    character_asset_id: Optional[int] = None
    character_upload_id: Optional[int] = None
    bg_asset_id: Optional[int] = None
    bg_upload_id: Optional[int] = None
    intro_asset_id: Optional[int] = None
    intro_upload_id: Optional[int] = None
    outro_asset_id: Optional[int] = None
    outro_upload_id: Optional[int] = None
    # Direct-video intro/outro: a yas_uploads id whose mime is a video, used
    # AS-IS as the intro/outro clip (skips card-gen + Veo).
    intro_video_upload_id: Optional[int] = None
    outro_video_upload_id: Optional[int] = None
    # How a picked intro/outro reference is used by the v2 cards: "as_is" (the
    # default: the card is the image itself), "new_vibe" (fresh card in the
    # reference's medium) or "text_only" (older runs). The v2 service refuses
    # anything else with a 400. (v1 ignores these.)
    intro_ref_mode: Optional[str] = None
    outro_ref_mode: Optional[str] = None
    # Bookend generate-toggle (v2). When false, that bookend is never generated
    # (no card image, no Veo clip) and is omitted from the final stitch — the
    # service seeds the existing `*_excluded` flag. Default True = today's
    # behavior. (v1 route ignores these.)
    generate_intro: bool = True
    generate_outro: bool = True
    # Output video aspect ratio (v2): "9:16" | "16:9" (1:1 unsupported by Veo).
    aspect_ratio: Optional[str] = None
    video_quality: Optional[str] = None  # "normal" | "quality"
    # How close the camera is for every still (v2): absent = "above_hip",
    # the default for a new reel. (v1 ignores it.)
    framing: THV2Framing = None


class TalkingHeadRetryRequest(BaseModel):
    # v6 — accepts "main" | "intro" | "outro" | "scene_<N>" where N is
    # a 0-indexed scene index. Validation happens in the service.
    which: str
    # feedback_text is OPTIONAL. When the user retries because of a
    # transient Veo failure (high load, etc), there's no creative
    # feedback to give — they just want the same prompt fired again.
    # The service treats null/empty as "re-run as-is". For a still (main /
    # scene_<N>) text means Refine (that one change, made on the current
    # still) and none means Regenerate.
    feedback_text: Optional[str] = None
    # Optional engine override — when set, the user's current toggle
    # selection wins over the engine stored on the run. The new value
    # is also persisted back to metadata.image_models so subsequent
    # retries stay consistent.
    image_models: Optional[TalkingHeadImageModels] = None


class TalkingHeadSaveRequest(BaseModel):
    display_name: Optional[str] = None


@router.post("/talking-head/generate-script")
async def th_generate_script(body: TalkingHeadGenerateScriptRequest) -> dict:
    """Run GPT-4o with full RAG context → return the structured script and
    a fresh run id. The row's image columns are NULL at this point — call
    /generate-previews next to populate them.
    """
    refs = TalkingHeadRefs(
        character_asset_id=body.character_asset_id,
        character_upload_id=body.character_upload_id,
        bg_asset_id=body.bg_asset_id,
        bg_upload_id=body.bg_upload_id,
        intro_asset_id=body.intro_asset_id,
        intro_upload_id=body.intro_upload_id,
        outro_asset_id=body.outro_asset_id,
        outro_upload_id=body.outro_upload_id,
    )
    # Resolve per-tile model dict. Frontend can pass `image_models` (new)
    # or `image_model` (legacy single value applied to all three).
    if body.image_models:
        models = {
            "main":  body.image_models.main,
            "intro": body.image_models.intro,
            "outro": body.image_models.outro,
        }
    else:
        single = (body.image_model or ENGINE_OPENAI).lower()
        if single not in (ENGINE_OPENAI, ENGINE_GEMINI_PRO):
            single = ENGINE_OPENAI
        models = {"main": single, "intro": single, "outro": single}
    log.info(
        "[yas-th] /generate-script body.image_models=%s body.image_model=%s -> resolved=%s",
        body.image_models.model_dump() if body.image_models else None,
        body.image_model, models,
    )
    # Convert pydantic scene_overrides into plain dicts so the service
    # can read them without importing the request model.
    overrides_dicts: list[dict] | None = None
    if body.scene_overrides is not None:
        overrides_dicts = [
            (o.model_dump() if o is not None else {}) for o in body.scene_overrides
        ]
    return await th_service.generate_script(
        topic=body.topic,
        refs=refs,
        lock_character=body.lock_character,
        image_models=models,
        user_dialogue=body.user_dialogue,
        user_dialogues=body.user_dialogues,
        scene_count=body.scene_count,
        scene_overrides=overrides_dicts,
    )


@router.post("/talking-head/{run_id}/generate-previews")
async def th_generate_previews(run_id: int) -> dict:
    """Generate main keyframe + intro card + outro card in parallel.
    Skips intro/outro card gen for slots where the user supplied a reference.
    """
    return await th_service.generate_previews(run_id)


@router.post("/talking-head/{run_id}/retry-preview")
async def th_retry_preview(run_id: int, body: TalkingHeadRetryRequest) -> dict:
    override: Optional[dict] = None
    if body.image_models:
        override = {
            "main":  body.image_models.main,
            "intro": body.image_models.intro,
            "outro": body.image_models.outro,
        }
    return await th_service.retry_preview(
        run_id=run_id, which=body.which, feedback_text=body.feedback_text,
        image_models_override=override,
    )


@router.post("/talking-head/{run_id}/finalize")
async def th_finalize(run_id: int) -> dict:
    """Kick Veo for every segment of the reel. Single-scene runs (the
    default) fire 3 jobs (intro/main/outro). Multi-scene runs fire N+2
    jobs in parallel (scene_0..N-1 + intro + outro). Returns immediately
    with composition_status=queued; UI should poll. When all jobs land,
    composition_status flips to `videos_ready` and the UI lets the user
    preview each clip then click `/stitch`.
    """
    return await th_service.start_videos_dispatch(run_id)


@router.post("/talking-head/{run_id}/retry-video")
async def th_retry_video(run_id: int, body: TalkingHeadRetryRequest) -> dict:
    """Retry ONE of the three Veo clips (intro/main/outro) with feedback.

    Inserts a NEW yas_animations row, points the run's metadata at it,
    and flips `composition_status` back to `queued` so the user has to
    re-press Stitch once the new clip lands.
    """
    return await th_service.retry_video(
        run_id=run_id, which=body.which, feedback_text=body.feedback_text,
    )


@router.post("/talking-head/{run_id}/stitch")
async def th_stitch(run_id: int) -> dict:
    """User-triggered composition kickoff. Only valid when
    composition_status == 'videos_ready'. The poller picks it up next tick
    and submits the Remotion render job.
    """
    return await th_service.stitch_composition(run_id)


@router.get("/talking-head/{run_id}")
async def th_fetch(run_id: int) -> dict:
    """Full run state — preview + intro/outro + Veo + composition status."""
    return await th_service.fetch_run(run_id)


@router.post("/talking-head/{run_id}/save-to-assets")
async def th_save(run_id: int, body: TalkingHeadSaveRequest) -> dict:
    return await th_service.save_to_assets(
        run_id=run_id, display_name=body.display_name,
    )


@router.get("/talking-head/recent")
async def th_recent(limit: int = 20) -> dict:
    items = await th_service.list_recent(limit=max(1, min(int(limit), 50)))
    return {"items": items}


@router.get("/library")
async def yas_library(request: Request, limit: int = 200, offset: int = 0):
    """Unified history of past Yas Producer work — talking-head runs and
    poster+human images/videos — for the Library grid. Lightweight: one query,
    no per-run hydration. Sibling card/scene rows are excluded; filtering by
    type/video is done client-side from the cached list.
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    # Bucket publicness must be known BEFORE any tile URL is built — _library_item
    # is sync and reads the cached map. Cheap after the first warm-up (5 min TTL),
    # never raises.
    await asset_storage.ensure_bucket_publicness()
    # (The image-transform capability is probed inside _apply_signed_thumbs, which
    # is where a real object path first exists to probe with.)
    # The global newest-first top (offset+limit) can only contain items that are
    # in the top (offset+limit) of their OWN source, so fetch exactly that many
    # from each source — no full-store scan, no arbitrary 10k truncation.
    cap = offset + limit
    # autocommit: read-only endpoint. The default transactional block costs a
    # BEGIN *and* a COMMIT round trip on top of the query — ~570ms of pure
    # latency against a pooler in another region — and parks the connection
    # `idle in transaction` while Python maps the rows.
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH latest_anim AS (
                    SELECT DISTINCT ON (yas_generation_id)
                           yas_generation_id, id AS anim_id, status AS anim_status,
                           storage_bucket AS anim_storage_bucket,
                           storage_path   AS anim_storage_path
                      FROM yas_animations
                     ORDER BY yas_generation_id, created_at DESC
                )
                SELECT g.id, g.topic, g.mode, g.status, g.image_model,
                       g.cost_usd, g.created_at, g.updated_at,
                       -- MINIMAL metadata projection, not g.metadata.
                       -- The full JSONB averages 10 kB/row for video-editor
                       -- projects — 856 kB across a 200-row page — and shipping
                       -- it from a pooler in another region measured ~1.2s of
                       -- this endpoint's response time (1.73s -> 0.55s without
                       -- it). _library_item reads exactly two things out of
                       -- metadata: `video_edit` (clips/render/project — for the
                       -- draft thumbnail, composition_status and the preset that
                       -- decides the tile type) and `composition_status` (Talking
                       -- Head). `main_animation_id` is consumed by the JOIN
                       -- above, not by Python. Projecting just those: ~141 kB.
                       -- jsonb_strip_nulls is recursive, so absent keys vanish
                       -- rather than arriving as explicit nulls, and a row with
                       -- NULL metadata yields {} — which _library_item already
                       -- treats as "no metadata".
                       jsonb_strip_nulls(jsonb_build_object(
                           'composition_status',
                               g.metadata->>'composition_status',
                           -- A TH v2 reel finalized again: its last good
                           -- file is shown while a new one renders.
                           'render_owner',
                               g.metadata->>'render_owner',
                           'composition_path',
                               g.metadata->>'composition_path',
                           'composition_good_seq',
                               g.metadata->'composition_good_seq',
                           'video_edit',
                               CASE WHEN g.metadata ? 'video_edit'
                                    THEN jsonb_build_object(
                                        'clips',   g.metadata->'video_edit'->'clips',
                                        'render',  g.metadata->'video_edit'->'render',
                                        'project', g.metadata->'video_edit'->'project'
                                    )
                               END
                       )) AS metadata,
                       g.storage_bucket, g.storage_path, g.local_path,
                       a.anim_id, a.anim_status,
                       a.anim_storage_bucket, a.anim_storage_path,
                       ma.status AS main_anim_status
                  FROM yas_generations g
                  LEFT JOIN latest_anim a ON a.yas_generation_id = g.id
                  LEFT JOIN yas_animations ma
                    ON ma.id = CASE
                         WHEN g.metadata->>'main_animation_id' ~ '^[0-9]+$'
                         THEN (g.metadata->>'main_animation_id')::int
                       END
                 WHERE (g.mode IS NULL
                        OR g.mode NOT IN (%s, %s, %s, %s, %s, %s))
                   -- Talking Head rows are INSERTed status='ready' when the
                   -- SCRIPT is ready, before any image exists (see
                   -- yas_talking_head_v2.py:1411). Those are abandoned script
                   -- drafts, not finished work: eligibility must test MEDIA
                   -- PRESENCE, not `status`, or the grid lists them and every
                   -- tile 410s.
                   AND NOT (
                         g.mode = %s
                     AND g.storage_path IS NULL
                     AND g.local_path IS NULL
                     AND g.metadata->>'composition_status' IS NULL
                     AND g.metadata->>'main_animation_id' IS NULL
                   )
                 ORDER BY g.created_at DESC
                 LIMIT %s
                """,
                (
                    th_service.MODE_INTRO,
                    th_service.MODE_OUTRO,
                    th_service.MODE_SCENE,
                    th_v2_service.MODE_INTRO,
                    th_v2_service.MODE_OUTRO,
                    th_v2_service.MODE_SCENE,
                    th_v2_service.MODE_PREVIEW,
                    cap,
                ),
            )
            rows = await cur.fetchall()
    db_items = [_library_item(r) for r in rows]
    # Poster-only (Create Poster) and standalone Create-Animation records live in
    # local JSON stores, not yas_generations, so the query above never sees them.
    # Merge them in here so the Library surfaces — and Phase C can reopen — every
    # tool's output. No 24h cap (the per-tool /recent endpoints have one; the
    # Library spans ALL past work). Paging is applied to the merged, re-sorted set.
    _FAR_HOURS = 24 * 365 * 20  # ~20y — effectively no age cutoff

    # These three reads are INDEPENDENT. Awaited one after another they cost three
    # separate connection acquisitions in series — and against a remote pooler
    # every acquisition is a full round trip, so the sequential version dominated
    # this endpoint's latency (measured ~4.4s TTFB for SQL that runs in 3.9ms).
    # Gathered, they overlap into roughly one. Three concurrent borrowers fit
    # inside DB_POOL_MAX with headroom.
    async def _active_roadmap() -> dict:
        """Roadmap step per (tool, ref_id). Non-fatal: a failure here must not
        500 the Library, so it degrades to no annotations."""
        try:
            from ..services import roadmap_state
            active = await roadmap_state.all_active()
            return {(r["tool"], int(r["ref_id"])): r["step"] for r in active}
        except Exception:
            log.exception("[library] roadmap-step annotation failed (non-fatal)")
            return {}

    poster_recs, anim_recs, by_key = await asyncio.gather(
        ystore.recent(
            mode=POSTER_MODE, within_hours=_FAR_HOURS, limit=cap,
            statuses=(STATUS_READY, STATUS_SAVED),
        ),
        anim_store.recent(
            within_hours=_FAR_HOURS, limit=cap, statuses=("ready",),
        ),
        _active_roadmap(),
    )
    merged = (
        db_items
        + [_library_item_poster(r) for r in poster_recs]
        + [_library_item_animation(r) for r in anim_recs]
    )
    # Newest first across all three sources. Sort on the parsed UTC instant: both
    # the SQL serialiser and the JSON stores emit offset-aware ISO-8601, so this
    # is timezone-correct regardless of the DB session timezone (a naive string
    # prefix compare would mis-order if the DB session weren't UTC).
    from datetime import datetime, timezone

    def _created_dt(it: dict) -> datetime:
        raw = it.get("created_at")
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    merged.sort(key=_created_dt, reverse=True)
    page = merged[offset:offset + limit]
    # Annotate each tile with its in-progress roadmap step (if any) so the
    # Library can offer "Continue → <Step>". item["type"] IS the roadmap tool
    # string for the 5 roadmap tools. (Fetched above, in the gather.)
    for it in page:
        try:
            it["roadmap_step"] = by_key.get((it.get("type"), int(it.get("id"))))
        except (TypeError, ValueError):
            it["roadmap_step"] = None

    # Upgrade any tile still pointing at the full-resolution backend proxy (because
    # its bucket is private) to a signed ~20kB WebP transform served by the CDN.
    # Only the paged slice — signing 500 rows to show 200 would be wasted work.
    await _apply_signed_thumbs(page)

    # ETag/304: the Library reloads this list on every open; a 304 skips the
    # (potentially large) payload when nothing changed since the client's copy.
    return _etag_response(request, {"items": page})


# Stable breakdown keys for the Dashboard (pure strings — safe at import time).
_REEL_KEYS = {"talking_head_v2"}
_POSTER_KEYS = {"style_inspired", "template_fill", "poster_human"}


@router.get("/stats")
async def yas_stats() -> dict:
    """Aggregate studio summary for the command-center Dashboard. One round-trip:
    a per-category × per-status breakdown of generations, an animations status
    rollup, lifetime spend, and a 14-day zero-filled daily activity series.

    Counts mirror the Library's child-mode exclusion (intro/outro/scene rows are
    pipeline internals, not standalone work). Best-effort: any failure degrades
    to zeros rather than 500-ing the dashboard.
    """
    # Category taxonomy — (mode value, bucket key, human label) in display order.
    # Built here, NOT at module scope, because the MODE_* constants come from
    # services imported lower in this file (deferred imports). Any mode not listed
    # (e.g. a future creator) gets its own bucket so nothing is silently dropped.
    # NULL mode is the original "Poster + Human" generative category.
    categories_def: list[tuple[str, str, str]] = [  # noqa: E501 (display map)
        (th_v2_service.MODE_PREVIEW, "talking_head_v2", "Human Element Video"),
        ("style_inspired", "style_inspired", "Posters · Style inspired"),
        ("template_fill", "template_fill", "Posters · Template + portrait"),
        (ve_service.MODE_PREVIEW, "video_edit", "Editor projects"),
        ("__null__", "poster_human", "Poster + Human"),
    ]
    child_modes = (
        th_service.MODE_INTRO, th_service.MODE_OUTRO, th_service.MODE_SCENE,
        th_v2_service.MODE_INTRO, th_v2_service.MODE_OUTRO, th_v2_service.MODE_SCENE,
    )
    # v1 Talking Head is hidden from the UI — exclude its preview rows too.
    excluded_modes = child_modes + (th_service.MODE_PREVIEW,)
    gen_rows: list[dict] = []
    anim_rows: list[dict] = []
    activity: list[dict] = []
    sched_agg: dict | None = None
    gen_spend = 0.0
    anim_spend = 0.0
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                # 1) Generations grouped by mode × status (+ spend per cell).
                await cur.execute(
                    """
                    SELECT mode, status,
                           COUNT(*)::int                     AS n,
                           COALESCE(SUM(cost_usd), 0)::float AS spend
                      FROM yas_generations
                     WHERE mode IS NULL OR mode NOT IN (%s, %s, %s, %s, %s, %s, %s)
                     GROUP BY mode, status
                    """,
                    excluded_modes,
                )
                gen_rows = await cur.fetchall()

                # 2) Animations grouped by status (+ spend).
                await cur.execute(
                    """
                    SELECT status,
                           COUNT(*)::int                     AS n,
                           COALESCE(SUM(cost_usd), 0)::float AS spend
                      FROM yas_animations
                     GROUP BY status
                    """
                )
                anim_rows = await cur.fetchall()

                # 3) 14-day zero-filled daily activity (UTC days), for the sparkline.
                await cur.execute(
                    """
                    SELECT to_char(d.day, 'YYYY-MM-DD') AS day,
                           COALESCE(c.n, 0)::int        AS count
                      FROM generate_series(
                             (now() AT TIME ZONE 'UTC')::date - interval '13 days',
                             (now() AT TIME ZONE 'UTC')::date,
                             interval '1 day'
                           ) AS d(day)
                      LEFT JOIN (
                        SELECT date_trunc('day', created_at AT TIME ZONE 'UTC')::date AS day,
                               COUNT(*) AS n
                          FROM yas_generations
                         WHERE created_at >= now() - interval '14 days'
                           AND (mode IS NULL OR mode NOT IN (%s, %s, %s, %s, %s, %s, %s))
                         GROUP BY 1
                      ) c ON c.day = d.day
                     ORDER BY d.day
                    """,
                    excluded_modes,
                )
                activity = await cur.fetchall()

                # 4) Content funnel — one query on this same connection (the
                #    scheduled table is tiny; FILTER + DISTINCT in a single pass).
                try:
                    await cur.execute(
                        """
                        SELECT
                          COUNT(*) FILTER (WHERE status IN ('pending','reminded'))::int AS in_process,
                          COUNT(*) FILTER (WHERE status = 'cancelled')::int             AS declined,
                          COUNT(*) FILTER (WHERE status = 'published')::int             AS approved,
                          COUNT(DISTINCT (tool, ref_id))::int                           AS distinct_scheduled
                          FROM scheduled_instagram_posts
                        """
                    )
                    sched_agg = await cur.fetchone()
                except Exception:
                    sched_agg = None  # table may not exist yet on a fresh DB
    except Exception:
        log.exception("[yas] stats aggregate failed")
        gen_rows, anim_rows, activity = [], [], []

    # --- Fold generation rows into ordered category buckets. -----------------
    mode_to_key = {m: key for (m, key, _label) in categories_def}
    label_for = {key: label for (_m, key, label) in categories_def}
    order = {key: i for i, (_m, key, _label) in enumerate(categories_def)}

    def status_bucket() -> dict:
        return {"total": 0, "ready": 0, "generating": 0, "failed": 0, "saved": 0}

    buckets: dict[str, dict] = {}
    gen_total = 0
    by_status = {"ready": 0, "generating": 0, "failed": 0, "saved": 0}
    for r in gen_rows:
        raw_mode = r.get("mode")
        n = int(r.get("n") or 0)
        gen_spend += float(r.get("spend") or 0.0)
        gen_total += n
        # Resolve the bucket key: known mode, NULL → poster_human, else its own.
        key = mode_to_key.get(raw_mode if raw_mode is not None else "__null__")
        if key is None:
            key = str(raw_mode)  # future/unknown creator — keep it visible
        b = buckets.setdefault(key, status_bucket())
        b["total"] += n
        # Normalise status into ready / generating / failed / saved.
        st = (r.get("status") or "").lower()
        if st == "generating":
            b["generating"] += n
            by_status["generating"] += n
        elif st == "failed":
            b["failed"] += n
            by_status["failed"] += n
        elif st == "saved_to_assets":
            b["saved"] += n
            by_status["saved"] += n
        else:  # ready (and any other terminal-good status)
            b["ready"] += n
            by_status["ready"] += n

    categories = []
    for key, b in buckets.items():
        if b["total"] <= 0:
            continue
        categories.append({
            "key": key,
            "label": label_for.get(key, key.replace("_", " ").title()),
            **b,
        })
    categories.sort(key=lambda c: (-c["total"], order.get(c["key"], 999)))

    reels = sum(b["total"] for k, b in buckets.items() if k in _REEL_KEYS)
    posters = sum(b["total"] for k, b in buckets.items() if k in _POSTER_KEYS)
    editor_projects = buckets.get("video_edit", {}).get("total", 0)

    # --- Animations status rollup. -------------------------------------------
    anim = {"total": 0, "ready": 0, "generating": 0, "failed": 0}
    for r in anim_rows:
        n = int(r.get("n") or 0)
        anim_spend += float(r.get("spend") or 0.0)
        anim["total"] += n
        st = (r.get("status") or "").lower()
        if st in ("ready", "generating", "failed"):
            anim[st] += n

    # --- Content funnel (Dashboard): made → scheduled → posted / declined. ----
    # in_process / declined / approved come straight from the scheduled-post
    # statuses (already aggregated above, on the shared connection); pending =
    # finished work that hasn't been booked yet, i.e. everything ready minus
    # everything that's ever been scheduled.
    funnel = {"pending": 0, "in_process": 0, "declined": 0, "approved": 0}
    if sched_agg:
        funnel["in_process"] = int(sched_agg.get("in_process") or 0)
        funnel["declined"] = int(sched_agg.get("declined") or 0)
        funnel["approved"] = int(sched_agg.get("approved") or 0)
        ready_total = by_status["ready"] + by_status["saved"] + anim["ready"]
        funnel["pending"] = max(0, ready_total - int(sched_agg.get("distinct_scheduled") or 0))

    return {
        "generations": {
            "total": gen_total,
            "by_status": by_status,
            "categories": categories,
            "reels": reels,
            "posters": posters,
            "editor_projects": editor_projects,
        },
        "animations": anim,
        "spend": {
            "generations_usd": round(gen_spend, 4),
            "animations_usd": round(anim_spend, 4),
            "total_usd": round(gen_spend + anim_spend, 4),
        },
        "activity_14d": [
            {"day": r.get("day"), "count": int(r.get("count") or 0)}
            for r in activity
        ],
        "funnel": funnel,
    }


# Dashboard Instagram card — OUR posted content (NOT the 42AD ingest). The like
# lookups hit the Graph API once per published post, so cache the rollup briefly;
# otherwise every dashboard load re-fetches the same numbers.
_IG_OWN_CACHE: dict = {"key": None, "at": 0.0, "data": None}
# 15 min, not 2. Engagement counts are a trailing indicator — nobody needs them
# fresher than this — and each miss fans out to the Graph API across every post.
# A 2-minute TTL meant an ordinary browsing session re-paid that fan-out
# repeatedly, on a token the PUBLISH path also depends on.
_IG_OWN_TTL_SEC = 900.0


@router.get("/instagram/own-summary")
async def yas_instagram_own_summary() -> dict:
    """What WE have actually published to Instagram through the Producer: post
    count, summed likes/comments (live from the Graph API), and the latest post
    time. Feeds the Dashboard's Instagram card. Best-effort — never 500s, and
    returns null likes (UI shows "—") when publishing isn't configured."""
    import time as _time

    from ..services import instagram_publish as ig_publish
    from ..services import scheduled_posts as sched_store

    try:
        rows = await sched_store.published_media()
    except Exception:
        log.exception("[yas] instagram own-summary: published_media failed")
        rows = []

    total_posts = len(rows)
    latest_post_at = None
    for r in rows:  # newest-first
        pub = r.get("published_at")
        if pub is not None:
            latest_post_at = pub.isoformat() if hasattr(pub, "isoformat") else str(pub)
            break

    media_ids = [str(r.get("ig_media_id")) for r in rows if r.get("ig_media_id")]
    cache_key = tuple(sorted(media_ids))
    now = _time.monotonic()
    cached = _IG_OWN_CACHE
    if (
        cached.get("data") is not None
        and cached.get("key") == cache_key
        and (now - float(cached.get("at") or 0.0)) < _IG_OWN_TTL_SEC
    ):
        metrics = cached["data"]
    else:
        metrics = await ig_publish.fetch_media_metrics_per_post(media_ids)
        _IG_OWN_CACHE.update(key=cache_key, at=now, data=metrics)

    available = bool(metrics.get("metrics_available"))
    per_media = metrics.get("per_media") or {}
    # Per-post series for the dashboard engagement chart — newest first, capped.
    # Always include published_at so the UI can fall back to a posts-over-time
    # view when engagement metrics aren't available.
    posts: list[dict] = []
    for r in rows[:12]:
        mid = str(r.get("ig_media_id")) if r.get("ig_media_id") else None
        pub = r.get("published_at")
        m = per_media.get(mid) if mid else None
        posts.append({
            "published_at": (pub.isoformat() if hasattr(pub, "isoformat") else str(pub)) if pub else None,
            "permalink": r.get("permalink"),
            "likes": (m.get("likes") if m else None) if available else None,
            "comments": (m.get("comments") if m else None) if available else None,
            "views": (m.get("views") if m else None) if available else None,
        })

    return {
        "total_posts": total_posts,
        "total_likes": metrics.get("total_likes") if available else None,
        "total_comments": metrics.get("total_comments") if available else None,
        "total_views": metrics.get("total_views") if available else None,
        "latest_post_at": latest_post_at,
        "metrics_available": available,
        "posts": posts,
    }


# File proxies — keyframe, card, Veo video, final composition


async def _serve_th_file(
    *,
    local_path: str | None,
    storage_bucket: str | None,
    storage_path: str | None,
    mime_type: str | None,
    default_mime: str,
    label: str,
):
    """Serve a talking-head asset from local disk, falling back to the Supabase
    bucket when the local file is absent (cross-machine moves, cleaned disk).

    Mirrors `get_animation_file` (the /poster-human proxy): local fast-path
    first, then a signed-URL fetch from the bucket, and only 410 when neither
    source has the bytes. Purely additive — healthy local rows behave exactly
    as before; only the previously-410 case now attempts the bucket.
    """
    # 1) Local disk (fast path — unchanged behavior for healthy rows).
    if local_path:
        p = th_service._abs_from_rel(local_path)
        if p.exists():
            # The main keyframe path is UPSERTED on regenerate, so this must not
            # be immutable-for-a-year or a regenerated keyframe stays pinned.
            return FileResponse(
                str(p),
                media_type=mime_type or default_mime,
                headers=_TH_FILE_CACHE_HEADERS,
            )
    # 2) Supabase bucket fallback — REDIRECT, don't proxy.
    #
    # This used to sign with expires=600, open a FRESH httpx.AsyncClient per
    # image, download the whole object and return `Response(content=r.content)`
    # — so every tile pushed ~1.4 MB through backend RAM and paid its own TLS
    # handshake. expires=600 also collapsed the signed-URL cache to its 30s floor
    # (max(30, 600-600)), so the sign round trip repeated constantly.
    #
    # Redirecting hands the bytes straight from the CDN to the browser: no
    # buffering, no extra handshake, and the long sign TTL lets the signed-URL
    # cache actually do its job.
    if storage_bucket and storage_path:
        signed = await asset_storage.url_for(
            storage_bucket=storage_bucket,
            storage_path=storage_path,
            local_path=None,
            expires=_MEDIA_SIGN_TTL,
        )
        if signed:
            return RedirectResponse(
                signed, status_code=307, headers=_REDIRECT_CACHE_HEADERS,
            )
    raise HTTPException(410, f"{label} missing on disk and in storage")


@router.get("/talking-head/{run_id}/file")
async def th_main_file(run_id: int):
    """Stream the main keyframe image."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT local_path, storage_bucket, storage_path, mime_type
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                """,
                (run_id, th_service.MODE_PREVIEW),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "main keyframe not found")
    return await _serve_th_file(
        local_path=row.get("local_path"),
        storage_bucket=row.get("storage_bucket"),
        storage_path=row.get("storage_path"),
        mime_type=row.get("mime_type"),
        default_mime="image/png",
        label="main keyframe",
    )


@router.get("/talking-head/cards/{card_id}/file")
async def th_card_file(card_id: int):
    """Stream an intro or outro card."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT local_path, mime_type, mode
                  FROM yas_generations
                 WHERE id = %s AND mode IN (%s, %s)
                """,
                (card_id, th_service.MODE_INTRO, th_service.MODE_OUTRO),
            )
            row = await cur.fetchone()
    if not row or not row.get("local_path"):
        raise HTTPException(404, "card image not found")
    p = th_service._abs_from_rel(row["local_path"])
    if not p.exists():
        raise HTTPException(410, "card image file missing on disk")
    # Card/scene keyframe paths are unique per row (never upserted), so an
    # immutable year is honest here. Starlette's FileResponse has no
    # if-none-match branch, so a 304 is impossible on this route — the header is
    # what saves the round trip, not a validator.
    return FileResponse(
        str(p), media_type=row.get("mime_type") or "image/png",
        headers=_MEDIA_CACHE_HEADERS,
    )


@router.get("/talking-head/scenes/{scene_id}/file")
async def th_scene_file(scene_id: int):
    """v6 — stream a multi-scene keyframe (MODE_SCENE row)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT local_path, mime_type
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                """,
                (scene_id, th_service.MODE_SCENE),
            )
            row = await cur.fetchone()
    if not row or not row.get("local_path"):
        raise HTTPException(404, "scene keyframe not found")
    p = th_service._abs_from_rel(row["local_path"])
    if not p.exists():
        raise HTTPException(410, "scene keyframe file missing on disk")
    # Card/scene keyframe paths are unique per row (never upserted), so an
    # immutable year is honest here. Starlette's FileResponse has no
    # if-none-match branch, so a 304 is impossible on this route — the header is
    # what saves the round trip, not a validator.
    return FileResponse(
        str(p), media_type=row.get("mime_type") or "image/png",
        headers=_MEDIA_CACHE_HEADERS,
    )


@router.get("/talking-head/{run_id}/video")
async def th_video_file(run_id: int):
    """Stream the MAIN Veo MP4 (legacy alias for /video/main)."""
    return await _th_video_role(run_id, "main")


@router.get("/talking-head/{run_id}/video/{role}")
async def th_video_file_role(run_id: int, role: str):
    """Stream the Veo MP4 for a specific role: intro | main | outro."""
    return await _th_video_role(run_id, role)


async def _th_video_role(run_id: int, role: str):
    # v6 — accept scene_<N> for multi-scene runs in addition to legacy roles.
    scene_idx: int | None = None
    if role.startswith("scene_"):
        try:
            scene_idx = int(role.split("_", 1)[1])
        except (ValueError, IndexError):
            raise HTTPException(400, f"invalid scene role: {role!r}")
    elif role not in ("intro", "main", "outro"):
        raise HTTPException(400, "role must be intro | main | outro | scene_<N>")

    run = await th_service._fetch_run_row(run_id)
    md = run.get("metadata") or {}

    if scene_idx is not None:
        scenes = (md.get("scenes") or [])
        if scene_idx < 0 or scene_idx >= len(scenes):
            raise HTTPException(404, f"scene_{scene_idx} not in this run")
        anim_id = scenes[scene_idx].get("animation_id")
        label = f"scene_{scene_idx}"
    else:
        key = {
            "intro": "intro_animation_id",
            "main":  "main_animation_id",
            "outro": "outro_animation_id",
        }[role]
        anim_id = md.get(key) or (md.get("veo_animation_id") if role == "main" else None)
        label = role

    if not anim_id:
        raise HTTPException(404, f"no {label} animation linked to this run")
    anim = await th_service._fetch_animation_row(int(anim_id))
    if not anim:
        raise HTTPException(404, f"no {label} animation row found")

    # Three-tier resolution. The local-disk MP4 is only a transient cache —
    # the durable copy lives in the Supabase bucket (uploaded by
    # yas_animations_poller._mark_ready). Serve from disk when present, else
    # recover from the bucket so a wiped cache doesn't 410 a ready video.
    # 1) Local copy present → stream it (fast path).
    local_rel = anim.get("local_path")
    if local_rel:
        p = th_service._abs_from_rel(local_rel)
        if p.exists():
            return FileResponse(
                str(p), media_type="video/mp4", headers=_MEDIA_CACHE_HEADERS,
            )

    # 2) Local copy gone but the bucket has it → 307-redirect to a short-lived
    #    signed URL. Supabase signed object URLs honour range requests, so
    #    <video> seeking keeps working straight from the CDN.
    if anim.get("storage_bucket") and anim.get("storage_path"):
        signed = await asset_storage.url_for(
            storage_bucket=anim["storage_bucket"],
            storage_path=anim["storage_path"],
            local_path=None,
            expires=3600,
        )
        if signed:
            return RedirectResponse(signed, status_code=307)

    # 3) Neither local nor bucket has it → genuine loss.
    raise HTTPException(
        410, f"{label} Veo MP4 missing (no local copy and no bucket object)"
    )


@router.get("/talking-head/{run_id}/composition/file")
async def th_composition_file(run_id: int):
    """Stream the final stitched composition MP4."""
    run = await th_service._fetch_run_row(run_id)
    md = run.get("metadata") or {}
    if md.get("composition_status") != th_service.COMP_READY:
        raise HTTPException(409, "Composition not ready")
    comp_rel = md.get("composition_path")
    if not comp_rel:
        raise HTTPException(410, "Composition path missing")
    p = th_service._abs_from_rel(comp_rel)
    if not p.exists():
        raise HTTPException(410, "Composition file missing on disk")
    return FileResponse(
        str(p), media_type="video/mp4", headers=_MEDIA_CACHE_HEADERS,
    )


# ===========================================================================
# POSTER MODE — two-stage static poster generation
#   Stage 1: OpenAI gpt-4o expands the topic into a retro-voxel scene desc.
#   Stage 2: Gemini gemini-3-pro-image renders it, with the 42 logo
#            + optional references sent as ordered image parts.
# ===========================================================================

from ..services.poster_prompts import (
    POSTER_CONCEPT_COUNT,
    LOGO_INSTRUCTION,
    VARIATION_REF_INSTRUCTION,
    VARIATION_NUDGES,
    build_variation_user_message,
    parse_concepts,
    dedupe_concepts,
)
from ..services.poster_logo import composite_logo_png
# Per-style Stage-1/Stage-2 prompts (voxel references the poster_prompts symbols
# above by identity, so the voxel look is byte-identical to the v1 pipeline).
from ..services.poster_styles import (
    PosterStyle,
    get_poster_style,
    DEFAULT_POSTER_STYLE,
)
from ..services.yas_poster_prompts import describe_poster_prompt as _describe_poster_prompt
from ..services.human_character_prompts import (
    HUMAN_CHARACTER_GEMINI_CONCEPTS_SCHEMA,
    HUMAN_CHARACTER_STYLE,
    build_human_character_user_message,
    HUMAN_CHARACTER_DESCRIBE_SYSTEM,
    HUMAN_CHARACTER_DESCRIBE_USER,
)

POSTER_MODE = "style_inspired"
# History strips across Ideation tools show the last 3 days of generations.
HISTORY_WINDOW_HOURS = 72
# Human Character generator (Ideation → Human Character). Reuses the poster
# two-stage pipeline with its OWN local-store mode, no logo, and a photoreal
# portrait style. Saved candidates become `character` assets (Talking Head refs).
HUMAN_CHARACTER_MODE = "human_character"
HUMAN_CHARACTER_VARIANT_COUNT = 4
HUMAN_CHARACTER_CONTENT_CATEGORY = "img_human_character"
# User-selectable output formats. Nano Banana takes the aspect via a text render
# directive (same mechanism as Create Poster's 1:1 and the Create-Animation
# frame's 9:16/16:9) — no model param needed. Keep in sync with the frontend.
HUMAN_CHARACTER_ASPECTS = ("1:1", "3:4", "9:16", "16:9")
_HC_RENDER_DIRECTIVES = {
    "1:1": "RENDER A 1:1 SQUARE IMAGE.",
    "3:4": "RENDER A 3:4 PORTRAIT (upright) IMAGE.",
    "9:16": "RENDER A 9:16 VERTICAL (tall, full-height portrait) IMAGE.",
    "16:9": "RENDER A 16:9 HORIZONTAL (wide, landscape) IMAGE.",
}
# Poster pipeline internals live in services/poster_pipeline.py — see the
# re-binding block near the top of this file for the shared constants.
from ..services.poster_pipeline import (  # noqa: E402
    POSTER_CONCEPTS_MAX_TOKENS,
    POSTER_LOGO_PATH,
    POSTER_MAX_REFS,
    POSTER_VARIANT_COUNT,
    poster_concepts as _poster_concepts,
    poster_recent_summaries as _poster_recent_summaries,
    poster_refund as _poster_refund,
    poster_reserve as _poster_reserve,
    poster_slots_remaining as _poster_slots_remaining,
    run_stage2_batch as _run_stage2_batch,
    single_stage2 as _single_stage2,
    start_stage2_batch as _start_stage2_batch,
    POSTER_ASPECTS,
    DEFAULT_POSTER_ASPECT,
    aspect_from_dims as _aspect_from_dims,
    render_directive_for as _render_directive_for,
)


def _poster_aspect_of(row: dict) -> str:
    """The shape an existing poster was rendered at.

    Explore, Refine and Remove-logo all produce a CHILD of a finished poster, and
    a child that silently comes back square is the feature half-built. There is no
    aspect column to read, so the parent's stored width/height answer it: see
    `poster_pipeline.aspect_from_dims` for why the pixels are the record."""
    return _aspect_from_dims(row.get("width"), row.get("height"))


def _is_resource_exhausted(exc: Exception) -> bool:
    """True for a Gemini/Vertex 429 RESOURCE_EXHAUSTED (image-gen quota/capacity).
    Surfaced as an HTTP 429 so the UI shows 'rate limited' instead of a scary 502."""
    s = str(exc)
    return " 429 " in f" {s} " or "RESOURCE_EXHAUSTED" in s or "resource has been exhausted" in s.lower()


def _http_for_no_image(exc: GeminiNoImageError, *, action: str) -> HTTPException:
    """Map a "model returned no image" failure to a clean, actionable HTTP error
    instead of a generic 502 with the misleading "check the model ID" text.

    A safety/policy block (``blocked``) is the user's content, not a server fault →
    422 with a "pick a different image / re-roll" hint. A persistent text-only reply
    (the escalated retry in ``card_animation`` also failed) is a transient model
    quirk → 502 'try again'. ``action`` is the user-facing verb ("remove the logo",
    "apply that change")."""
    if exc.blocked:
        return HTTPException(
            422,
            f"The image model declined to {action} for this image — it treated the "
            "request as not allowed (e.g. brand-mark removal) or filtered the content. "
            "Try re-rolling, or use a different source image.",
        )
    return HTTPException(
        502,
        f"The image model kept replying with text instead of an edited image, so it "
        f"couldn't {action}. This is usually transient — try again.",
    )


class PosterGenerateRequest(BaseModel):
    topic: str
    reference_asset_ids: list[int] = []
    reference_upload_ids: list[int] = []
    image_usage: str = "style"  # "style" | "background" — only matters with refs
    image_model: str = ENGINE_GEMINI_PRO  # "gemini_pro" (Nano Banana) | "openai" (gpt-image-1)
    style: str = DEFAULT_POSTER_STYLE  # "voxel" (default) | "voxel_classic" | "pattern" | "popup"
    aspect: str = DEFAULT_POSTER_ASPECT  # "1:1" (default) | "9:16" | "16:9"
    # True: answer as soon as the four rows exist (all `generating`) and render
    # them in the background; the page polls each row and shows it when ready.
    progressive: bool = False


# A refused (429) poster render gets this many attempts, a few seconds apart,
# then fails with "busy, try again" — instead of the image tools' patient
# policy, which can hold one card for minutes.
POSTER_QUOTA_ATTEMPTS = 2


def _norm_engine(image_model: str | None) -> str:
    """Create Poster is Nano Banana (Gemini) ONLY. The OpenAI gpt-image-1 engine
    was removed, so any incoming value (including a stale 'openai' from an old
    client) resolves to Nano Banana."""
    return ENGINE_GEMINI_PRO


@router.post("/poster/generate")
async def poster_generate(body: PosterGenerateRequest) -> dict:
    """Generate 4 static poster variants via the two-stage pipeline.

    Pipeline:
      1. Resolve reference images (asset library + uploads)
      2. Stage 1 — gpt-4o expands the topic into N *distinct* scene concepts in
         the chosen style (voxel / voxel_classic / pattern / popup), each with a
         [TEXT TO RENDER]
         block, anti-repeating against recent runs.
      3. Stage 2 — the selected engine (Nano Banana or gpt-image-1) renders each
         concept concurrently (Midjourney style), in the chosen style.
      4. Upload each image to Supabase Storage (producer-media bucket); the
         record lives in Postgres (yas_poster_generations).
      5. Return {items: [...4 records], remaining_this_hour}.
    """
    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required")

    image_usage = (body.image_usage or "style").strip().lower()
    if image_usage not in {"style", "background"}:
        raise HTTPException(400, "image_usage must be 'style' or 'background'")

    # Explicit user input is validated rather than snapped to the default: a
    # client asking for a shape this tool does not offer should be told, not
    # quietly handed a square.
    aspect = (body.aspect or DEFAULT_POSTER_ASPECT).strip()
    if aspect not in POSTER_ASPECTS:
        raise HTTPException(400, f"aspect must be one of {', '.join(POSTER_ASPECTS)}")

    if len(body.reference_asset_ids) + len(body.reference_upload_ids) > POSTER_MAX_REFS:
        raise HTTPException(400, f"at most {POSTER_MAX_REFS} reference images allowed")

    await _budget_guard()
    # Reserves nothing any more: the hourly image cap was removed and
    # poster_reserve is a no-op kept so its callers did not have to change.
    # The call stays because deleting it would also delete the refund below,
    # and the pair is what a reinstated cap would hook back into.
    remaining_after = _poster_reserve(POSTER_VARIANT_COUNT)

    # --- Resolve reference image paths (only touches Supabase when refs given) ---
    try:
        asset_paths = await _resolve_asset_paths(body.reference_asset_ids)
        upload_paths = await _resolve_upload_paths(body.reference_upload_ids)
    except Exception:
        _poster_refund(POSTER_VARIANT_COUNT)
        raise
    ref_paths = [*asset_paths, *upload_paths]

    style = get_poster_style(body.style)

    # --- Stage 1: gpt-4o invents N *distinct* concepts (JSON, deduped, topped-up) ---
    try:
        concepts, stage1_cost = await _poster_concepts(topic, POSTER_VARIANT_COUNT, style)
    except HTTPException:
        _poster_refund(POSTER_VARIANT_COUNT)
        raise

    # If the model gave fewer distinct concepts than slots reserved, refund the gap.
    if len(concepts) < POSTER_VARIANT_COUNT:
        _poster_refund(POSTER_VARIANT_COUNT - len(concepts))
    remaining_after = _poster_slots_remaining()

    engine = _norm_engine(body.image_model)
    specs = [
        {"prompt": c["prompt"], "concept": c["summary"],
         "variation_ref_path": None, "variation_of": None}
        for c in concepts
    ]
    log.info(
        "[poster] stage1 ok · %d distinct concept(s) · refs=%d (%s) · engine=%s · style=%s · aspect=%s",
        len(specs), len(ref_paths), image_usage, engine, style.key, aspect,
    )

    # --- Stage 2: render each concept ONE AT A TIME (sequential) to avoid the
    # parallel-submit image-gen 429 quota. ---
    if body.progressive:
        items = await _start_stage2_batch(
            specs, ref_paths, image_usage,
            topic=topic,
            reference_asset_ids=body.reference_asset_ids,
            reference_upload_ids=body.reference_upload_ids,
            parent_generation_id=None,
            stage1_cost_usd=stage1_cost,
            image_model=engine,
            style=style,
            render_directive=_render_directive_for(aspect),
            aspect_ratio=aspect,
            quota_attempts=POSTER_QUOTA_ATTEMPTS,
        )
        return {"items": items, "remaining_this_hour": remaining_after, "progressive": True}
    items = await _run_stage2_batch(
        specs, ref_paths, image_usage,
        topic=topic,
        reference_asset_ids=body.reference_asset_ids,
        reference_upload_ids=body.reference_upload_ids,
        parent_generation_id=None,
        stage1_cost_usd=stage1_cost,
        image_model=engine,
        style=style,
        render_directive=_render_directive_for(aspect),
        aspect_ratio=aspect,
        sequential=True,
        quota_attempts=POSTER_QUOTA_ATTEMPTS,
    )
    return {"items": items, "remaining_this_hour": remaining_after}


class PosterVariantsRequest(BaseModel):
    parent_id: int
    image_model: str = ENGINE_GEMINI_PRO  # engine for THIS explore action
    progressive: bool = False  # see PosterGenerateRequest.progressive


class RefineRequest(BaseModel):
    # The user's free-text description of the single change to apply ("make the title
    # bigger", "change the background to blue"). Shared by the poster + animation refine
    # routes, which strip/cap it before sending it to the model.
    instructions: str


@router.post("/poster/variants")
async def poster_variants(body: PosterVariantsRequest) -> dict:
    """Midjourney-style "explore": render 4 CLOSE variations of the chosen poster.

    Keeps the parent's concept + refs + image_usage and ANCHORS on the parent
    image (sent as a Gemini reference part) so the children stay near it. Each
    child gets a distinct micro-variation nudge so they differ from each other.
    Skips Stage 1 — the concept is inherited from the parent."""
    await _budget_guard()

    parent = await ystore.get(body.parent_id)
    if not parent or parent.get("mode") != POSTER_MODE:
        raise HTTPException(404, "parent poster not found")
    if parent.get("status") not in (STATUS_READY, STATUS_SAVED):
        raise HTTPException(422, f"cannot vary — parent status is '{parent.get('status')}'")
    base_prompt = (parent.get("prompt_used") or "").strip()
    if not base_prompt:
        raise HTTPException(422, "parent has no stored prompt to vary from")

    # The chosen poster image is the anchor for "more like this" — pull its bytes
    # (Supabase, or local fallback) into a temp file Gemini can read as a part.
    parent_bytes = await _media_bytes(
        storage_bucket=parent.get("storage_bucket"),
        storage_path=parent.get("storage_path"),
        local_path=parent.get("local_path"),
    )
    if not parent_bytes:
        raise HTTPException(410, "parent poster image not retrievable")
    import tempfile as _tf
    _pext = _ext_for_image_bytes(parent_bytes)
    with _tf.NamedTemporaryFile(suffix=f".{_pext}", delete=False) as _ptf:
        _ptf.write(parent_bytes)
        parent_img = Path(_ptf.name)

    # A background batch still reads the anchor image after this request ends,
    # so it removes the temp file itself; otherwise `finally` does.
    handed_off = False
    try:
        image_usage = parent.get("image_usage") or "style"
        asset_ids = parent.get("reference_asset_ids") or []
        upload_ids = parent.get("reference_upload_ids") or []
        asset_paths = await _resolve_asset_paths(asset_ids)
        upload_paths = await _resolve_upload_paths(upload_ids)
        ref_paths = [*asset_paths, *upload_paths]

        # Reserve only after refs resolve cleanly (refs can 404/410).
        remaining_after = _poster_reserve(POSTER_VARIANT_COUNT)

        # "More like this" stays in the chosen poster's style — inherit it from the
        # parent (legacy rows with no style fall back to voxel) and use that style's
        # variation nudges.
        style = get_poster_style(parent.get("style"))
        nudges = style.variation_nudges or VARIATION_NUDGES

        parent_concept = (parent.get("concept") or "").strip()
        specs = [
            {
                "prompt": build_variation_user_message(base_prompt, nudges[i % len(nudges)]),
                "concept": f"{parent_concept} · variation {i + 1}" if parent_concept else f"variation {i + 1} of #{body.parent_id}",
                "variation_ref_path": parent_img,
                "variation_of": body.parent_id,
            }
            for i in range(POSTER_VARIANT_COUNT)
        ]

        # "More like this" inherits the parent's SHAPE as well as its style —
        # riffing on a 9:16 poster and getting four squares back is not a riff.
        aspect = _poster_aspect_of(parent)

        engine = _norm_engine(body.image_model)
        log.info("[poster] exploring %d variations of #%d · refs=%d · engine=%s · style=%s · aspect=%s",
                 POSTER_VARIANT_COUNT, body.parent_id, len(ref_paths), engine, style.key, aspect)
        if body.progressive:
            items = await _start_stage2_batch(
                specs, ref_paths, image_usage,
                topic=parent.get("topic") or "",
                reference_asset_ids=asset_ids,
                reference_upload_ids=upload_ids,
                parent_generation_id=body.parent_id,
                stage1_cost_usd=0.0,
                image_model=engine,
                style=style,
                render_directive=_render_directive_for(aspect),
                aspect_ratio=aspect,
                quota_attempts=POSTER_QUOTA_ATTEMPTS,
                cleanup=lambda: parent_img.unlink(missing_ok=True),
            )
            handed_off = True
            return {"items": items, "remaining_this_hour": remaining_after, "progressive": True}
        items = await _run_stage2_batch(
            specs, ref_paths, image_usage,
            topic=parent.get("topic") or "",
            reference_asset_ids=asset_ids,
            reference_upload_ids=upload_ids,
            parent_generation_id=body.parent_id,
            stage1_cost_usd=0.0,
            image_model=engine,
            style=style,
            render_directive=_render_directive_for(aspect),
            aspect_ratio=aspect,
            sequential=True,
            quota_attempts=POSTER_QUOTA_ATTEMPTS,
        )
        return {"items": items, "remaining_this_hour": remaining_after}
    finally:
        if not handed_off:
            parent_img.unlink(missing_ok=True)


@router.post("/poster/{gen_id}/remove-logo")
async def poster_remove_logo(gen_id: int) -> dict:
    """Re-render a finished poster with the 42 logo removed, saved as a NEW child
    record next to the original (the original is never overwritten — the user can
    compare / re-roll). Mirrors /poster/variants: pull the chosen image bytes →
    feed them back to Gemini with a remove-logo instruction (NO logo part, via the
    shared ``card_animation.render_delogo_image``) → save a new
    yas_poster_generations row. Instruction-only edit (no mask/seed), so the rest
    of the image stays very close but not pixel-identical."""
    await _budget_guard()

    parent = await ystore.get(gen_id)
    if not parent or parent.get("mode") != POSTER_MODE:
        raise HTTPException(404, "poster not found")
    if parent.get("status") not in (STATUS_READY, STATUS_SAVED):
        raise HTTPException(422, f"cannot remove logo — status is '{parent.get('status')}'")

    src_bytes = await _media_bytes(
        storage_bucket=parent.get("storage_bucket"),
        storage_path=parent.get("storage_path"),
        local_path=parent.get("local_path"),
    )
    if not src_bytes:
        raise HTTPException(410, "poster image not retrievable")

    # One image render → reserve one hourly slot (only after the source resolved).
    remaining_after = _poster_reserve(1)

    import tempfile as _tf
    _sext = _ext_for_image_bytes(src_bytes)
    src_img: Path | None = None
    try:
        with _tf.NamedTemporaryFile(suffix=f".{_sext}", delete=False) as _stf:
            _stf.write(src_bytes)
            src_img = Path(_stf.name)

        # The de-logo render is an edit of this exact artwork, so it is pinned to
        # the shape the artwork already has.
        aspect = _poster_aspect_of(parent)

        # New child row — inherits topic/style/concept/prompt; image_usage flags the
        # de-logo lineage; parent_generation_id keeps the original linked.
        rec = await ystore.create(
            topic=parent.get("topic"),
            status=STATUS_GENERATING,
            mode=POSTER_MODE,
            image_model=parent.get("image_model") or ENGINE_GEMINI_PRO,
            style=parent.get("style"),
            prompt_used=(parent.get("prompt_used") or "")[:8000],
            concept=(parent.get("concept") or "")[:300],
            image_usage="logo_removed",
            variation_of=gen_id,
            parent_generation_id=gen_id,
            reference_asset_ids=parent.get("reference_asset_ids") or [],
            reference_upload_ids=parent.get("reference_upload_ids") or [],
        )
        new_id = int(rec["id"])

        spend_before = get_meter().spent_usd
        try:
            image_bytes = await card_animation.render_delogo_image(
                src_img, timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
                aspect_ratio=aspect,
            )
        except (VertexBudgetExceededError, OpenAIBudgetExceededError) as exc:
            await ystore.update(new_id, status=STATUS_FAILED, error=f"budget_exceeded: {exc}"[:500])
            raise HTTPException(503, str(exc))
        except asyncio.TimeoutError:
            await ystore.update(new_id, status=STATUS_FAILED, error="remove-logo timed out")
            raise HTTPException(504, "remove-logo timed out — try again")
        except HTTPException:
            raise
        except GeminiNoImageError as exc:
            await ystore.update(new_id, status=STATUS_FAILED, error=f"no_image: {exc}"[:500])
            raise _http_for_no_image(exc, action="remove the logo")
        except Exception as exc:
            if _is_permission_denied(exc):
                await ystore.update(new_id, status=STATUS_FAILED, error=f"permission_denied: {exc}"[:500])
                raise HTTPException(503, "Gemini access denied — check GCP project status")
            if _is_resource_exhausted(exc):
                await ystore.update(new_id, status=STATUS_FAILED, error=f"rate_limited: {exc}"[:500])
                raise HTTPException(429, "Image generation is rate-limited right now (quota) — wait a minute and try again.")
            log.exception("[poster] remove-logo failed (gen #%d)", new_id)
            await ystore.update(new_id, status=STATUS_FAILED, error=f"remove_logo_failed: {type(exc).__name__}: {exc}"[:500])
            raise HTTPException(502, f"remove-logo failed: {exc}")

        if not image_bytes:
            await ystore.update(new_id, status=STATUS_FAILED, error="empty image bytes")
            raise HTTPException(502, "remove-logo returned empty image")

        ext = _ext_for_image_bytes(image_bytes)
        mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
        width, height = _image_dims(image_bytes)
        up = await asset_storage.upload(
            data=image_bytes,
            path=f"poster/{new_id}.{ext}",
            mime=mime,
            bucket=config.producer_media_bucket,
        )
        cost = round(max(0.0, get_meter().spent_usd - spend_before), 6)
        updated = await ystore.update(
            new_id,
            status=STATUS_READY,
            mime_type=mime,
            bytes=len(image_bytes),
            width=width,
            height=height,
            cost_usd=cost,
            storage_bucket=up.get("storage_bucket"),
            storage_path=up.get("storage_path"),
            local_path=up.get("local_path"),
        )
        out = dict(updated or rec)
        out["file_url"] = f"/api/yas-producer/poster/{new_id}/file"
        out["remaining_this_hour"] = remaining_after
        log.info("[poster] remove-logo #%d → child #%d ready", gen_id, new_id)
        return out
    finally:
        if src_img is not None:
            src_img.unlink(missing_ok=True)


@router.post("/poster/{gen_id}/refine")
async def poster_refine(gen_id: int, body: RefineRequest) -> dict:
    """Re-render a finished poster applying ONLY a user-described change ("make the
    title bigger", "change the background to blue"), saved as a NEW child next to the
    original (the original is never overwritten — the user can compare / keep iterating).
    The general case of /poster/{id}/remove-logo: same plumbing (pull the chosen image
    bytes → feed them back to Gemini, NO logo part, via the shared
    ``card_animation.render_refine_image``) with the user's change carried in the final
    render directive instead of the fixed de-logo instruction. Instruction-only edit (no
    mask/seed), so the rest of the image stays very close but not pixel-identical."""
    await _budget_guard()

    change = (body.instructions or "").strip()[:500]
    if not change:
        raise HTTPException(422, "describe what to change")

    parent = await ystore.get(gen_id)
    if not parent or parent.get("mode") != POSTER_MODE:
        raise HTTPException(404, "poster not found")
    if parent.get("status") not in (STATUS_READY, STATUS_SAVED):
        raise HTTPException(422, f"cannot refine — status is '{parent.get('status')}'")

    src_bytes = await _media_bytes(
        storage_bucket=parent.get("storage_bucket"),
        storage_path=parent.get("storage_path"),
        local_path=parent.get("local_path"),
    )
    if not src_bytes:
        raise HTTPException(410, "poster image not retrievable")

    # One image render → reserve one hourly slot (only after the source resolved).
    remaining_after = _poster_reserve(1)

    import tempfile as _tf
    _sext = _ext_for_image_bytes(src_bytes)
    src_img: Path | None = None
    try:
        with _tf.NamedTemporaryFile(suffix=f".{_sext}", delete=False) as _stf:
            _stf.write(src_bytes)
            src_img = Path(_stf.name)

        # A refine edits this exact artwork, so it is pinned to the shape the
        # artwork already has.
        aspect = _poster_aspect_of(parent)

        # New child row — inherits topic/style/concept/prompt; feedback_text + retry_kind
        # record the refine lineage (columns already exist on the table);
        # parent_generation_id keeps the original linked.
        rec = await ystore.create(
            topic=parent.get("topic"),
            status=STATUS_GENERATING,
            mode=POSTER_MODE,
            image_model=parent.get("image_model") or ENGINE_GEMINI_PRO,
            style=parent.get("style"),
            prompt_used=(parent.get("prompt_used") or "")[:8000],
            concept=(parent.get("concept") or "")[:300],
            image_usage="refined",
            feedback_text=change,
            retry_kind="refine",
            variation_of=gen_id,
            parent_generation_id=gen_id,
            reference_asset_ids=parent.get("reference_asset_ids") or [],
            reference_upload_ids=parent.get("reference_upload_ids") or [],
        )
        new_id = int(rec["id"])

        spend_before = get_meter().spent_usd
        try:
            image_bytes = await card_animation.render_refine_image(
                src_img, change=change, timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
                aspect_ratio=aspect,
            )
        except (VertexBudgetExceededError, OpenAIBudgetExceededError) as exc:
            await ystore.update(new_id, status=STATUS_FAILED, error=f"budget_exceeded: {exc}"[:500])
            raise HTTPException(503, str(exc))
        except asyncio.TimeoutError:
            await ystore.update(new_id, status=STATUS_FAILED, error="refine timed out")
            raise HTTPException(504, "refine timed out — try again")
        except HTTPException:
            raise
        except GeminiNoImageError as exc:
            await ystore.update(new_id, status=STATUS_FAILED, error=f"no_image: {exc}"[:500])
            raise _http_for_no_image(exc, action="apply that change")
        except Exception as exc:
            if _is_permission_denied(exc):
                await ystore.update(new_id, status=STATUS_FAILED, error=f"permission_denied: {exc}"[:500])
                raise HTTPException(503, "Gemini access denied — check GCP project status")
            if _is_resource_exhausted(exc):
                await ystore.update(new_id, status=STATUS_FAILED, error=f"rate_limited: {exc}"[:500])
                raise HTTPException(429, "Image generation is rate-limited right now (quota) — wait a minute and try again.")
            log.exception("[poster] refine failed (gen #%d)", new_id)
            await ystore.update(new_id, status=STATUS_FAILED, error=f"refine_failed: {type(exc).__name__}: {exc}"[:500])
            raise HTTPException(502, f"refine failed: {exc}")

        if not image_bytes:
            await ystore.update(new_id, status=STATUS_FAILED, error="empty image bytes")
            raise HTTPException(502, "refine returned empty image")

        ext = _ext_for_image_bytes(image_bytes)
        mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
        width, height = _image_dims(image_bytes)
        up = await asset_storage.upload(
            data=image_bytes,
            path=f"poster/{new_id}.{ext}",
            mime=mime,
            bucket=config.producer_media_bucket,
        )
        cost = round(max(0.0, get_meter().spent_usd - spend_before), 6)
        updated = await ystore.update(
            new_id,
            status=STATUS_READY,
            mime_type=mime,
            bytes=len(image_bytes),
            width=width,
            height=height,
            cost_usd=cost,
            storage_bucket=up.get("storage_bucket"),
            storage_path=up.get("storage_path"),
            local_path=up.get("local_path"),
        )
        out = dict(updated or rec)
        out["file_url"] = f"/api/yas-producer/poster/{new_id}/file"
        out["remaining_this_hour"] = remaining_after
        log.info("[poster] refine #%d → child #%d ready", gen_id, new_id)
        return out
    finally:
        if src_img is not None:
            src_img.unlink(missing_ok=True)


@router.get("/poster/{gen_id}/file")
async def poster_get_file(gen_id: int, download: bool = False):
    """Serve the generated poster — redirect to a signed Supabase URL (or local fallback).

    `?download=1` forces a save-to-disk (Content-Disposition: attachment) — used by
    the Library "Download image" button."""
    gen = await ystore.get(gen_id)
    if not gen or gen.get("mode") != POSTER_MODE:
        raise HTTPException(404, "poster not found")
    mime = gen.get("mime_type") or "image/png"
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    resp = await _media_response(
        storage_bucket=gen.get("storage_bucket"),
        storage_path=gen.get("storage_path"),
        local_path=gen.get("local_path"),
        mime=mime,
        download_name=f"poster-{gen_id}.{ext}" if download else None,
    )
    if resp is None:
        raise HTTPException(410, "poster file not retrievable")
    return resp


class PosterSaveRequest(BaseModel):
    display_name: Optional[str] = None


@router.post("/poster/{gen_id}/save-to-assets")
async def poster_save(gen_id: int, body: PosterSaveRequest) -> dict:
    """Copy a generated poster into the Assets library.

    NOTE: this is the one poster path that still touches Supabase — it writes
    the image into the shared `producer_assets` library (Yasin's carve-out).
    The generation record itself is read from / updated in the local store.
    Until Supabase is reconnected, the producer_assets INSERT below will error.
    """
    gen = await ystore.get(gen_id)
    if not gen or gen.get("mode") != POSTER_MODE:
        raise HTTPException(404, "poster not found")
    if gen["status"] == STATUS_SAVED:
        raise HTTPException(409, detail={"error": "already_saved", "existing_asset_id": gen.get("saved_asset_id")})
    if gen["status"] != STATUS_READY:
        raise HTTPException(422, f"cannot save — status is '{gen['status']}'")

    # Read image bytes from Supabase Storage (or local fallback).
    image_bytes = await _media_bytes(
        storage_bucket=gen.get("storage_bucket"),
        storage_path=gen.get("storage_path"),
        local_path=gen.get("local_path"),
    )
    if image_bytes is None:
        raise HTTPException(410, "poster bytes not retrievable")

    mime = gen.get("mime_type") or "image/png"
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "png")

    # Describe + embed (best-effort)
    ai_description = ""
    try:
        sys_p, user_p = _describe_poster_prompt()
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
            tf.write(image_bytes)
            tmp_path = Path(tf.name)
        try:
            ai_description = await gemini_multimodal(
                user_p, images=[tmp_path], system=sys_p,
                model=config.vertex_gemini_fast_model,
                temperature=0.3, max_tokens=600,
            ) or ""
            ai_description = str(ai_description).strip()
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        if _is_permission_denied(exc):
            log.error("[poster] Gemini permission denied during describe")
        else:
            log.exception("[poster] describe step failed")
        ai_description = f"Yas-generated poster: {gen['topic'][:200]}"

    embedding = None
    if ai_description:
        try:
            vecs = await gemini_embed([ai_description])
            if vecs and vecs[0]:
                embedding = vecs[0]
        except Exception:
            log.exception("[poster] embedding failed")

    slug = _asset_slug(body.display_name or gen["topic"], f"yas_{gen_id}")

    upload_result = await asset_storage.upload(
        data=image_bytes, path=f"instagram/img_poster/{slug}.{ext}", mime=mime,
    )
    attributes = await _thumb_attributes(image_bytes, upload_result, mime)
    width, height = _image_dims(image_bytes)
    display_name = (body.display_name or gen["topic"][:80] or f"Yas poster #{gen_id}")
    metadata = {
        "content_category": "img_poster",
        "source": "yas_producer_poster",
        "yas_generation_id": gen_id,
        "topic": gen["topic"],
        "prompt_used": (gen.get("prompt_used") or "")[:2000],
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name, ai_description,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes, width, height, embedding, metadata, attributes, enabled)
                VALUES (%s, 'image', 'free', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id, slug, display_name, storage_bucket, storage_path,
                          local_path, mime_type, bytes, width, height,
                          ai_description, metadata, created_at,
                          (embedding IS NOT NULL) AS embedded
                """,
                (
                    slug, display_name, ai_description or None,
                    upload_result.get("storage_bucket"), upload_result.get("storage_path"),
                    upload_result.get("local_path"),
                    mime, len(image_bytes), width, height,
                    embedding, Jsonb(metadata), Jsonb(attributes),
                ),
            )
            asset_row = await cur.fetchone()

    # Flip the local-store record to "saved" (generation state lives locally).
    await ystore.update(gen_id, status=STATUS_SAVED, saved_asset_id=int(asset_row["id"]))

    out = _row(asset_row) or {}
    if asset_row.get("storage_bucket") and asset_row.get("storage_path"):
        try:
            out["signed_url"] = await asset_storage.url_for(
                storage_bucket=asset_row["storage_bucket"],
                storage_path=asset_row["storage_path"],
                local_path=asset_row.get("local_path"),
            )
        except Exception:
            out["signed_url"] = None
    return out


@router.get("/poster/recent")
async def poster_list_recent() -> dict:
    """Last 24h of poster generations (local store) for session rehydrate.

    Flat list — feeds the bottom history strip. The variant grid/tree is
    session state on the client and is not rehydrated on reload.
    """
    rows = await ystore.recent(
        mode=POSTER_MODE,
        within_hours=HISTORY_WINDOW_HOURS,
        limit=50,
        statuses=(STATUS_READY, STATUS_FAILED, STATUS_SAVED),
    )
    items = []
    for r in rows:
        out = dict(r)
        if r.get("status") in (STATUS_READY, STATUS_SAVED):
            out["file_url"] = f"/api/yas-producer/poster/{r['id']}/file"
        items.append(out)
    return {"items": items}


@router.get("/poster/{gen_id}")
async def poster_get_one(gen_id: int) -> dict:
    """Full metadata for ONE poster (local store) so the Library can reopen it
    in PosterCreator's ready/refine state. Same item shape as /poster/recent
    (the stored record + a file_url when the image exists). No age filter —
    resume must work for posters older than the 24h /recent window."""
    gen = await ystore.get(gen_id)
    if not gen or gen.get("mode") != POSTER_MODE:
        raise HTTPException(404, "poster not found")
    out = dict(gen)
    if gen.get("status") in (STATUS_READY, STATUS_SAVED):
        out["file_url"] = f"/api/yas-producer/poster/{gen['id']}/file"
    return out


@router.delete("/poster/{gen_id}")
async def poster_delete(gen_id: int) -> dict:
    """Hard-delete a poster (Create Poster) generation — used by the Library
    "Delete" button. Removes the poster from EVERYWHERE that references it:

      * the Supabase Storage object (best-effort),
      * the local-store row — which clears the Library grid, the Create-Poster
        history strip (/poster/recent), and "Continue editing" (/poster/{id}),
      * any NON-published scheduled Instagram post booked off this poster (a
        dangling row would 404 its thumbnail in the Scheduled tab and dead-end
        at Approve & Post), and
      * its mid-roadmap resume state (producer_roadmap), so a stale "Continue →
        <Step>" / saved draft can't be resurrected.

    Two things are deliberately PRESERVED. (1) If the poster was previously "Save
    to assets"-ed, that producer_assets copy is an independent library entry
    (used for Instagram publishing, search, etc.) and is left untouched — a
    delete must not silently gut the shared Assets library. (2) Already-PUBLISHED
    scheduled posts are kept: they are the audit trail of a real Instagram post
    with independently-hosted media.
    """
    gen = await ystore.get(gen_id)
    if not gen or gen.get("mode") != POSTER_MODE:
        raise HTTPException(404, "poster not found")
    # Best-effort: remove the stored image so we don't orphan it in the bucket.
    # Never block the row delete on a storage hiccup (the row is the thing the
    # Library reads from; an orphaned object is harmless and recoverable).
    try:
        await asset_storage.delete(
            storage_bucket=gen.get("storage_bucket"),
            storage_path=gen.get("storage_path"),
            local_path=gen.get("local_path"),
        )
    except Exception:
        log.exception("[poster] storage delete failed for #%s (continuing)", gen_id)
    deleted = await ystore.delete(gen_id)

    # Tidy up the two sibling tables that key off this poster id, so it's gone
    # from the Scheduled queue and the roadmap-resume state too. Both are
    # best-effort: a hiccup here must not fail the (already-completed) row delete.
    scheduled_removed = 0
    try:
        from ..services import scheduled_posts as sched_store

        scheduled_removed = await sched_store.delete_by_ref("poster", gen_id)
    except Exception:
        log.exception("[poster] scheduled-post cleanup failed for #%s (continuing)", gen_id)
    try:
        from ..services import roadmap_state

        await roadmap_state.delete("poster", gen_id)
    except Exception:
        log.exception("[poster] roadmap-state cleanup failed for #%s (continuing)", gen_id)

    return {"deleted": 1 if deleted else 0, "id": gen_id, "scheduled_removed": scheduled_removed}


# ---------------------------------------------------------------------------
# Library "Delete" for the OTHER types (poster is above). Each removes the
# generation row(s) + every storage object it OWNS, then the two sibling tables
# (non-published scheduled IG posts + roadmap resume state). Preserved across all:
# already-published IG posts (audit trail) and any "Save to assets" copy.
# ---------------------------------------------------------------------------

async def _delete_library_siblings(tool: str, ref_id: int) -> int:
    """Best-effort cleanup of the two tables that key off a finished asset when it's
    deleted: drop its NON-published scheduled Instagram posts and its mid-roadmap
    resume state. Never raises — a hiccup must not fail the (already completed) row
    delete. Returns the number of scheduled posts removed."""
    scheduled_removed = 0
    try:
        from ..services import scheduled_posts as sched_store

        scheduled_removed = await sched_store.delete_by_ref(tool, ref_id)
    except Exception:
        log.exception("[library-delete] scheduled cleanup failed for %s #%s", tool, ref_id)
    try:
        from ..services import roadmap_state

        await roadmap_state.delete(tool, ref_id)
    except Exception:
        log.exception("[library-delete] roadmap cleanup failed for %s #%s", tool, ref_id)
    return scheduled_removed


@router.delete("/poster-human/{gen_id}")
async def poster_human_delete(gen_id: int) -> dict:
    """Hard-delete a Poster+Human generation — Library "Delete". Removes the
    yas_generations row, its image storage, and EVERY yas_animations row tied to it
    (each animation's MP4 too), plus the sibling scheduled/roadmap state. Retry /
    de-logo CHILD rows are independent Library tiles and are NOT cascaded — this
    deletes only this one generation (and its own animations)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, mode, storage_bucket, storage_path, local_path "
                "FROM yas_generations WHERE id = %s",
                (int(gen_id),),
            )
            row = await cur.fetchone()
    # Only a Poster+Human still (mode NULL / 'poster_human' / 'template_fill') —
    # never a TH/VE row (which carry their own distinct modes).
    if not row or row.get("mode") not in (None, "poster_human", "template_fill"):
        raise HTTPException(404, "poster not found")

    try:
        await asset_storage.delete(
            storage_bucket=row.get("storage_bucket"),
            storage_path=row.get("storage_path"),
            local_path=row.get("local_path"),
        )
    except Exception:
        log.exception("[poster-human] image storage delete failed for #%s (continuing)", gen_id)

    # Each animation's MP4 (a gen can have several), then the rows. Explicit —
    # never rely on an FK cascade, so we can delete the bucket objects first.
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT storage_bucket, storage_path, local_path "
                "FROM yas_animations WHERE yas_generation_id = %s",
                (int(gen_id),),
            )
            anims = await cur.fetchall() or []
    for a in anims:
        try:
            await asset_storage.delete(
                storage_bucket=a.get("storage_bucket"),
                storage_path=a.get("storage_path"),
                local_path=a.get("local_path"),
            )
        except Exception:
            log.exception("[poster-human] animation storage delete failed (gen #%s)", gen_id)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM yas_animations WHERE yas_generation_id = %s", (int(gen_id),))
            await cur.execute("DELETE FROM yas_generations WHERE id = %s RETURNING id", (int(gen_id),))
            deleted = await cur.fetchone()

    scheduled_removed = await _delete_library_siblings("poster_human", gen_id)
    return {"deleted": 1 if deleted else 0, "id": gen_id, "scheduled_removed": scheduled_removed}


@router.delete("/animation/{anim_id}")
async def animation_delete(anim_id: int) -> dict:
    """Hard-delete a Create-Animation record — Library "Delete". Removes BOTH owned
    storage objects (the static frame AND the MP4), the animation-store row, and the
    sibling scheduled/roadmap state. A Video-Editor project seeded via "open in
    editor" holds an INDEPENDENT uploaded copy (different id space) and is preserved."""
    rec = await anim_store.get(anim_id)
    if not rec:
        raise HTTPException(404, "animation not found")
    for kind, b, p, lp in (
        ("frame", rec.get("frame_storage_bucket"), rec.get("frame_storage_path"), rec.get("frame_local_path")),
        ("video", rec.get("video_storage_bucket"), rec.get("video_storage_path"), rec.get("video_local_path")),
    ):
        try:
            await asset_storage.delete(storage_bucket=b, storage_path=p, local_path=lp)
        except Exception:
            log.exception("[animation] %s storage delete failed for #%s (continuing)", kind, anim_id)
    deleted = await anim_store.delete(anim_id)
    scheduled_removed = await _delete_library_siblings("animation", anim_id)
    return {"deleted": 1 if deleted else 0, "id": anim_id, "scheduled_removed": scheduled_removed}


@router.delete("/talking-head-v2/{run_id}")
async def th_v2_delete(run_id: int) -> dict:
    """Hard-delete a Talking-Head v2 run — Library "Delete". The service removes the
    run row + every child intro/outro/scene row, all their media (cards, keyframes,
    Veo MP4s, the stitched composition), and the per-run local dir; here we add the
    sibling scheduled/roadmap cleanup. Saved assets + published IG posts + any
    Video-Editor project opened from the run are preserved."""
    from ..services import yas_talking_head_v2 as th_v2_service

    result = await th_v2_service.delete_run(run_id)  # raises 404 if not a run
    scheduled_removed = await _delete_library_siblings("talking_head_v2", run_id)
    return {**result, "scheduled_removed": scheduled_removed}


@router.delete("/video-editor/{project_id}")
async def video_editor_delete(project_id: int) -> dict:
    """Hard-delete a Video-Editor project — Library "Delete". The service removes the
    yas_generations project row + its local render output dir; here we add the
    sibling scheduled/roadmap cleanup. The shared bin source clips (animations,
    uploads, assets, stills, compositions) are NEVER touched."""
    result = await ve_service.delete_project(project_id)  # raises 404 if not a project
    scheduled_removed = await _delete_library_siblings("video_editor", project_id)
    # A B-roll project's posts and roadmap state are keyed under "broll".
    scheduled_removed += await _delete_library_siblings("broll", project_id)
    return {**result, "scheduled_removed": scheduled_removed}


# ===========================================================================
# HUMAN CHARACTER — photorealistic person generator (Ideation → Human Character).
#
# Reuses the poster two-stage pipeline (gpt-4o casting candidates → Nano Banana
# render) with its OWN local-store mode, NO 42 logo, and a portrait style. A
# chosen candidate is saved as a `character` asset (content_category
# img_human_character) so Talking Head can pick it up as the on-screen presenter.
# ===========================================================================

async def _human_character_recent_summaries(limit: int = 8) -> list[str]:
    """Recent candidate signatures (newest first) for cross-batch anti-repetition
    — so successive Generate clicks don't keep casting the same faces."""
    try:
        rows = await ystore.recent(
            mode=HUMAN_CHARACTER_MODE,
            within_hours=24,
            limit=limit,
            statuses=(STATUS_READY, STATUS_SAVED),
        )
    except Exception:
        return []
    return [s for r in rows if (s := (r.get("concept") or "").strip())]


async def _human_character_concepts_via_gemini(topic: str, n: int, avoid: list[str]) -> str:
    """Stage 1 without OpenAI: the SAME casting system and user message on
    Gemini (fast model, thinking off), with a schema so the JSON parses.
    Returns a JSON string so the reply goes through parse_concepts exactly as
    OpenAI's does (the poster pipeline's _concepts_via_gemini, for casting)."""
    raw = await gemini_text(
        build_human_character_user_message(topic, n, avoid),
        system=HUMAN_CHARACTER_STYLE.concepts_system,
        json_schema=HUMAN_CHARACTER_GEMINI_CONCEPTS_SCHEMA,
        model=config.vertex_gemini_fast_model,
        temperature=0.95,
        max_tokens=POSTER_CONCEPTS_MAX_TOKENS,
        thinking_budget=0,
    )
    # gemini_text never raises on unparseable output: it returns {"_raw": ...}.
    if not isinstance(raw, dict) or raw.get("_raw") is not None:
        raise RuntimeError("the fallback model returned an unreadable answer")
    return json.dumps(raw)


async def _human_character_concepts(topic: str, n: int) -> tuple[list[dict], float]:
    """Stage 1: ask gpt-4o for `n` DISTINCT casting candidates (different people
    who all fit the brief) as strict JSON. Mirrors `_poster_concepts` but uses the
    human-character casting system + user message, and its own anti-repeat memory.

    Every OpenAI failure (its spend cap included: OpenAI has no credits on prod)
    falls through to Gemini with the same prompt (_human_character_concepts_via_gemini),
    the same shape as the poster pipeline's Stage 1.

    Returns (concepts[:n], openai_cost_usd). Raises HTTPException on hard failure.
    """
    from ..services.openai_client import get_meter as _openai_meter
    spend_before = _openai_meter().spent_usd
    recent = await _human_character_recent_summaries()

    concepts: list[dict] = []
    attempts = 0
    # The last thing that went wrong across both providers, for the error the
    # user sees when nothing at all came back.
    last_error: BaseException | None = None
    while len(concepts) < n and attempts < 3:
        attempts += 1
        avoid = recent + [c["summary"] for c in concepts]
        raw: str | None = None
        try:
            raw = await asyncio.wait_for(
                openai_chat(
                    build_human_character_user_message(topic, n, avoid),
                    system=HUMAN_CHARACTER_STYLE.concepts_system,
                    model="gpt-4o",
                    temperature=0.95,
                    max_tokens=POSTER_CONCEPTS_MAX_TOKENS,
                    response_format={"type": "json_object"},
                ),
                timeout=GEN_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            if concepts:
                break
            last_error = asyncio.TimeoutError("character casting timed out")
        except Exception as exc:
            last_error = exc
            log.warning("[human-character] stage 1 casting (gpt-4o) failed (%s), falling back to Gemini",
                        gen_errors._text(exc)[:200])

        if raw is None:
            try:
                raw = await asyncio.wait_for(
                    _human_character_concepts_via_gemini(topic, n, avoid), timeout=GEN_TIMEOUT_SEC,
                )
                log.info("[human-character] stage 1 casting via Gemini fallback")
            except Exception as exc:  # noqa: BLE001
                if concepts:
                    break  # a partial batch beats no batch
                log.exception("[human-character] stage 1 Gemini fallback also failed")
                last_error = exc
                continue  # both providers failed this round; `attempts` bounds it
        concepts = dedupe_concepts(concepts + parse_concepts(raw, n))

    # Only OpenAI is metered here; a Gemini round bills on the Vertex meter.
    cost = max(0.0, _openai_meter().spent_usd - spend_before)
    if not concepts:
        if isinstance(last_error, asyncio.TimeoutError):
            raise HTTPException(504, "character casting timed out — try again")
        if last_error is not None:
            friendly, technical = gen_errors.explain(last_error, what="the character casting")
            log.error("[human-character] stage 1 exhausted: %s", technical)
            raise HTTPException(502, friendly)
        raise HTTPException(502, "character casting returned nothing usable")
    return concepts[:n], cost


class HumanCharacterGenerateRequest(BaseModel):
    topic: str
    aspect: str = "1:1"  # "1:1" | "3:4" | "9:16" | "16:9"


@router.post("/human-character/generate")
async def human_character_generate(body: HumanCharacterGenerateRequest) -> dict:
    """Generate N distinct photorealistic human candidates from a brief.

    Pipeline mirrors Create Poster: Stage 1 (gpt-4o) casts N distinct people who
    fit the brief; Stage 2 (Nano Banana) renders each front-facing portrait with
    NO logo and NO text. Returns {items: [...N records], remaining_this_hour}.
    """
    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required")
    aspect = (body.aspect or "1:1").strip()
    if aspect not in HUMAN_CHARACTER_ASPECTS:
        raise HTTPException(400, f"aspect must be one of {list(HUMAN_CHARACTER_ASPECTS)}")

    await _budget_guard()
    # Shares the poster image-gen hourly cap (same expensive Nano Banana model).
    remaining_after = _poster_reserve(HUMAN_CHARACTER_VARIANT_COUNT)

    try:
        concepts, stage1_cost = await _human_character_concepts(
            topic, HUMAN_CHARACTER_VARIANT_COUNT
        )
    except HTTPException:
        _poster_refund(HUMAN_CHARACTER_VARIANT_COUNT)
        raise

    if len(concepts) < HUMAN_CHARACTER_VARIANT_COUNT:
        _poster_refund(HUMAN_CHARACTER_VARIANT_COUNT - len(concepts))
    remaining_after = _poster_slots_remaining()

    specs = [
        {"prompt": c["prompt"], "concept": c["summary"],
         "variation_ref_path": None, "variation_of": None}
        for c in concepts
    ]
    log.info("[human-character] stage1 ok · %d distinct candidate(s)", len(specs))

    items = await _run_stage2_batch(
        specs, [], "style",
        topic=topic,
        reference_asset_ids=[],
        reference_upload_ids=[],
        parent_generation_id=None,
        stage1_cost_usd=stage1_cost,
        image_model=ENGINE_GEMINI_PRO,
        style=HUMAN_CHARACTER_STYLE,
        mode=HUMAN_CHARACTER_MODE,
        include_logo=False,            # photoreal person — NO 42 logo, NO text
        storage_prefix="human-character",
        file_route="human-character",
        render_directive=_HC_RENDER_DIRECTIVES[aspect],
        # The directive asks; this forces. Without it Human Character's three
        # formats were a request in the prompt and nothing more, which is the
        # same half-built state Create Poster was in before it passed the real
        # ImageConfig parameter. `run_stage2_batch` hands it straight to the
        # model, so 3:4 (not a poster shape) passes through fine.
        aspect_ratio=aspect,
    )
    return {"items": items, "remaining_this_hour": remaining_after}


@router.get("/human-character/{gen_id}/file")
async def human_character_get_file(gen_id: int, download: bool = False):
    """Serve a generated human-character image (signed Supabase URL or local
    fallback). `?download=1` forces a save-to-disk."""
    gen = await ystore.get(gen_id)
    if not gen or gen.get("mode") != HUMAN_CHARACTER_MODE:
        raise HTTPException(404, "character not found")
    mime = gen.get("mime_type") or "image/png"
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    resp = await _media_response(
        storage_bucket=gen.get("storage_bucket"),
        storage_path=gen.get("storage_path"),
        local_path=gen.get("local_path"),
        mime=mime,
        download_name=f"human-character-{gen_id}.{ext}" if download else None,
    )
    if resp is None:
        raise HTTPException(410, "character file not retrievable")
    return resp


@router.post("/human-character/{gen_id}/save-to-assets")
async def human_character_save(gen_id: int, body: PosterSaveRequest) -> dict:
    """Copy a chosen candidate into the Assets library.

    Tagged with metadata.content_category='img_human_character' — that is what
    BOTH the Assets "Human Characters" card AND the Talking Head v2 character
    picker filter on, and what makes it selectable as a presenter reference.

    The top-level `category` is left NULL (not 'character') on purpose: the
    codebase invariant is "category='character' REQUIRES an archetype" (see the
    frame-import path in producer_assets.py, which sets category=None when no
    archetype is supplied). We don't collect an archetype here, so NULL is the
    correct, constraint-safe value — and the content_category tag carries all the
    integration we need.
    """
    gen = await ystore.get(gen_id)
    if not gen or gen.get("mode") != HUMAN_CHARACTER_MODE:
        raise HTTPException(404, "character not found")
    if gen["status"] == STATUS_SAVED:
        raise HTTPException(409, detail={"error": "already_saved", "existing_asset_id": gen.get("saved_asset_id")})
    if gen["status"] != STATUS_READY:
        raise HTTPException(422, f"cannot save — status is '{gen['status']}'")

    image_bytes = await _media_bytes(
        storage_bucket=gen.get("storage_bucket"),
        storage_path=gen.get("storage_path"),
        local_path=gen.get("local_path"),
    )
    if image_bytes is None:
        raise HTTPException(410, "character bytes not retrievable")

    mime = gen.get("mime_type") or "image/png"
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "png")

    # Describe + embed (best-effort) so the character is searchable in Assets.
    ai_description = ""
    try:
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
            tf.write(image_bytes)
            tmp_path = Path(tf.name)
        try:
            ai_description = await gemini_multimodal(
                HUMAN_CHARACTER_DESCRIBE_USER, images=[tmp_path],
                system=HUMAN_CHARACTER_DESCRIBE_SYSTEM,
                model=config.vertex_gemini_fast_model,
                temperature=0.3, max_tokens=400,
            ) or ""
            ai_description = str(ai_description).strip()
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        if _is_permission_denied(exc):
            log.error("[human-character] Gemini permission denied during describe")
        else:
            log.exception("[human-character] describe step failed")
        ai_description = f"AI-generated human character: {gen['topic'][:200]}"

    embedding = None
    if ai_description:
        try:
            vecs = await gemini_embed([ai_description])
            if vecs and vecs[0]:
                embedding = vecs[0]
        except Exception:
            log.exception("[human-character] embedding failed")

    slug = _asset_slug(body.display_name or gen["topic"], f"character_{gen_id}")

    upload_result = await asset_storage.upload(
        data=image_bytes, path=f"instagram/{HUMAN_CHARACTER_CONTENT_CATEGORY}/{slug}.{ext}", mime=mime,
    )
    attributes = await _thumb_attributes(image_bytes, upload_result, mime)
    width, height = _image_dims(image_bytes)
    display_name = (body.display_name or gen["topic"][:80] or f"Character #{gen_id}")
    metadata = {
        "content_category": HUMAN_CHARACTER_CONTENT_CATEGORY,
        "source": "yas_producer_human_character",
        "yas_generation_id": gen_id,
        "topic": gen["topic"],
        "prompt_used": (gen.get("prompt_used") or "")[:2000],
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name, ai_description,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes, width, height, embedding, metadata, attributes, enabled)
                VALUES (%s, 'image', NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id, slug, display_name, storage_bucket, storage_path,
                          local_path, mime_type, bytes, width, height,
                          ai_description, metadata, created_at,
                          (embedding IS NOT NULL) AS embedded
                """,
                (
                    slug, display_name, ai_description or None,
                    upload_result.get("storage_bucket"), upload_result.get("storage_path"),
                    upload_result.get("local_path"),
                    mime, len(image_bytes), width, height,
                    embedding, Jsonb(metadata), Jsonb(attributes),
                ),
            )
            asset_row = await cur.fetchone()

    await ystore.update(gen_id, status=STATUS_SAVED, saved_asset_id=int(asset_row["id"]))

    out = _row(asset_row) or {}
    if asset_row.get("storage_bucket") and asset_row.get("storage_path"):
        try:
            out["signed_url"] = await asset_storage.url_for(
                storage_bucket=asset_row["storage_bucket"],
                storage_path=asset_row["storage_path"],
                local_path=asset_row.get("local_path"),
            )
        except Exception:
            out["signed_url"] = None
    return out


@router.get("/human-character/recent")
async def human_character_list_recent() -> dict:
    """Last 24h of human-character generations (local store) for session rehydrate
    — feeds the creator's history strip."""
    rows = await ystore.recent(
        mode=HUMAN_CHARACTER_MODE,
        within_hours=HISTORY_WINDOW_HOURS,
        limit=50,
        statuses=(STATUS_READY, STATUS_FAILED, STATUS_SAVED),
    )
    items = []
    for r in rows:
        out = dict(r)
        if r.get("status") in (STATUS_READY, STATUS_SAVED):
            out["file_url"] = f"/api/yas-producer/human-character/{r['id']}/file"
        items.append(out)
    return {"items": items}


@router.get("/human-character/{gen_id}")
async def human_character_get_one(gen_id: int) -> dict:
    """Full metadata for ONE human-character generation (no age filter)."""
    gen = await ystore.get(gen_id)
    if not gen or gen.get("mode") != HUMAN_CHARACTER_MODE:
        raise HTTPException(404, "character not found")
    out = dict(gen)
    if gen.get("status") in (STATUS_READY, STATUS_SAVED):
        out["file_url"] = f"/api/yas-producer/human-character/{gen['id']}/file"
    return out


# ===========================================================================
# TALKING HEAD v2 — isolated copy of the talking-head flow.
#
# Byte-for-byte behavioural twin of the /talking-head/* routes above, but
# wired to `yas_talking_head_v2` (its own service + prompts + poller) and its
# own mode rows (talking_head_v2_*). v1 is the untouched backup; everything
# here can diverge freely. Request models + the generic `_serve_th_file`
# helper are reused from the v1 block (pure data shapes / mode-agnostic).
# ===========================================================================

from ..services import yas_talking_head_v2 as th_v2_service  # noqa: E402
from ..services.yas_talking_head_v2 import TalkingHeadRefs as TalkingHeadRefsV2  # noqa: E402


@router.post("/talking-head-v2/generate-script")
async def th_v2_generate_script(body: TalkingHeadGenerateScriptRequest) -> dict:
    """Run GPT-4o with full RAG context → return the structured script and
    a fresh run id. The row's image columns are NULL at this point — call
    /generate-previews next to populate them.
    """
    refs = TalkingHeadRefsV2(
        character_asset_id=body.character_asset_id,
        character_upload_id=body.character_upload_id,
        bg_asset_id=body.bg_asset_id,
        bg_upload_id=body.bg_upload_id,
        intro_asset_id=body.intro_asset_id,
        intro_upload_id=body.intro_upload_id,
        outro_asset_id=body.outro_asset_id,
        outro_upload_id=body.outro_upload_id,
        intro_video_upload_id=body.intro_video_upload_id,
        outro_video_upload_id=body.outro_video_upload_id,
        intro_ref_mode=body.intro_ref_mode,
        outro_ref_mode=body.outro_ref_mode,
    )
    # Resolve per-tile model dict. Frontend can pass `image_models` (new)
    # or `image_model` (legacy single value applied to all three).
    if body.image_models:
        models = {
            "main":  body.image_models.main,
            "intro": body.image_models.intro,
            "outro": body.image_models.outro,
        }
    else:
        single = (body.image_model or ENGINE_GEMINI_PRO).lower()
        if single not in (ENGINE_OPENAI, ENGINE_GEMINI_PRO):
            single = ENGINE_GEMINI_PRO
        models = {"main": single, "intro": single, "outro": single}
    log.info(
        "[yas-th] /generate-script body.image_models=%s body.image_model=%s -> resolved=%s",
        body.image_models.model_dump() if body.image_models else None,
        body.image_model, models,
    )
    # Convert pydantic scene_overrides into plain dicts so the service
    # can read them without importing the request model.
    overrides_dicts: list[dict] | None = None
    if body.scene_overrides is not None:
        overrides_dicts = [
            (o.model_dump() if o is not None else {}) for o in body.scene_overrides
        ]
    return await th_v2_service.generate_script(
        topic=body.topic,
        refs=refs,
        lock_character=body.lock_character,
        image_models=models,
        user_dialogue=body.user_dialogue,
        user_dialogues=body.user_dialogues,
        scene_count=body.scene_count,
        scene_overrides=overrides_dicts,
        generate_intro=body.generate_intro,
        generate_outro=body.generate_outro,
        aspect_ratio=body.aspect_ratio or "9:16",
        video_quality=body.video_quality or "normal",
        framing=body.framing,
    )


# How a picked intro/outro image is used (yas_talking_head_v2.REF_MODES):
# kept as it is (the default), a new design in its style, or (older runs)
# redrawn with the script's words. Anything else is refused (422).
THV2RefMode = Optional[Literal["as_is", "new_vibe", "text_only"]]


class THV2GeneratePreviewsRequest(BaseModel):
    """Optional 'look' payload for the first-creation flow: the refs/overrides/
    lock/engine picked on the Look page (after the script was generated at
    Topic→Look). All fields optional; an empty/absent body = today's behavior."""
    character_asset_id: Optional[int] = None
    character_upload_id: Optional[int] = None
    bg_asset_id: Optional[int] = None
    bg_upload_id: Optional[int] = None
    intro_asset_id: Optional[int] = None
    intro_upload_id: Optional[int] = None
    outro_asset_id: Optional[int] = None
    outro_upload_id: Optional[int] = None
    # Direct-video intro/outro: a yas_uploads id whose mime is a video, used
    # AS-IS as the intro/outro clip (skips card-gen + Veo).
    intro_video_upload_id: Optional[int] = None
    outro_video_upload_id: Optional[int] = None
    intro_ref_mode: THV2RefMode = None
    outro_ref_mode: THV2RefMode = None
    # A previous reel's card picked as the intro/outro ("Previous videos": a
    # TH v2 card id), and whether a kept card keeps its own words ("Keep card
    # text"; off: a headline that differs from the script is swapped).
    intro_card_id: Optional[int] = None
    outro_card_id: Optional[int] = None
    intro_keep_text: Optional[bool] = None
    outro_keep_text: Optional[bool] = None
    scene_overrides: Optional[list[Optional[TalkingHeadSceneRefOverride]]] = None
    lock_character: Optional[bool] = None
    image_models: Optional[TalkingHeadImageModels] = None
    bg_mode: Optional[str] = None  # global one-shot "New bg" intent (read, not persisted)
    aspect_ratio: Optional[str] = None  # "9:16" | "16:9"
    video_quality: Optional[str] = None  # "normal" | "quality"
    # How close the camera is: the run's, and per scene (null = unchanged,
    # "inherit" = follow the run's). Saved, then every still is made at it.
    framing: THV2Framing = None
    scene_framings: Optional[list[THV2SceneFraming]] = None


@router.post("/talking-head-v2/{run_id}/generate-previews")
async def th_v2_generate_previews(
    run_id: int, body: Optional[THV2GeneratePreviewsRequest] = None,
) -> dict:
    """Generate main keyframe + intro card + outro card in parallel.
    Skips intro/outro card gen for slots that are excluded.

    When a `look` body is sent (first-creation flow), the picked refs/overrides/
    lock/engine are patched onto the run first (no regen), and the one-shot
    `bg_mode` drives the "use my uploaded photo as-is" path.
    """
    look: dict | None = None
    if body is not None:
        provided = body.model_fields_set
        refs = {k: getattr(body, k) for k in _TH_V2_REF_FIELDS if k in provided}
        overrides = (
            [(o.model_dump() if o is not None else None) for o in body.scene_overrides]
            if body.scene_overrides is not None else None
        )
        look = {}
        if refs:
            look["refs"] = refs
        if overrides is not None:
            look["scene_overrides"] = overrides
        if "lock_character" in provided:
            look["lock_character"] = body.lock_character
        if body.image_models:
            look["image_models"] = body.image_models.model_dump()
        if body.bg_mode is not None:
            look["bg_mode"] = body.bg_mode
        if body.aspect_ratio is not None:
            look["aspect_ratio"] = body.aspect_ratio
        if body.video_quality is not None:
            look["video_quality"] = body.video_quality
        if body.framing is not None:
            look["framing"] = body.framing
        if body.scene_framings is not None:
            look["scene_framings"] = list(body.scene_framings)
        look = look or None
    return await th_v2_service.generate_previews(run_id, look=look)


@router.post("/talking-head-v2/{run_id}/retry-preview")
async def th_v2_retry_preview(run_id: int, body: TalkingHeadRetryRequest) -> dict:
    override: Optional[dict] = None
    if body.image_models:
        override = {
            "main":  body.image_models.main,
            "intro": body.image_models.intro,
            "outro": body.image_models.outro,
        }
    return await th_v2_service.retry_preview(
        run_id=run_id, which=body.which, feedback_text=body.feedback_text,
        image_models_override=override,
    )


class THV2RefineStillRequest(BaseModel):
    # The ONE change to make on the still ("warmer light", "zoom out to above
    # the hip"). The service keeps the first 500 characters.
    change: str = Field(..., max_length=2000)


# Stills ("main" or "scene_<N>"): Refine makes one change on the current
# still (a true image edit), Regenerate makes it again the way first
# generation did (no text), Undo puts back the version before the last
# change. Each returns the run. Errors: 409 no still / being made / nothing
# to undo, 422 refused or no image, 429 quota, 502, 504 timeout.
@router.post("/talking-head-v2/{run_id}/still/{which}/refine")
async def th_v2_refine_still(run_id: int, which: str, body: THV2RefineStillRequest) -> dict:
    await th_v2_service.refine_still(run_id, which, body.change)
    return await th_v2_service.fetch_run(run_id)


@router.post("/talking-head-v2/{run_id}/still/{which}/regenerate")
async def th_v2_regenerate_still(run_id: int, which: str) -> dict:
    await th_v2_service.regenerate_still(run_id, which)
    return await th_v2_service.fetch_run(run_id)


@router.post("/talking-head-v2/{run_id}/still/{which}/undo")
async def th_v2_undo_still(run_id: int, which: str) -> dict:
    await th_v2_service.undo_still(run_id, which)
    return await th_v2_service.fetch_run(run_id)


class THV2FinalizeRequest(BaseModel):
    # Set only by the page's "Make videos" / "Update N clips" button (it
    # shows the cost). Anything else (an older page's auto-fire on entering
    # the Videos step, a stray script) is refused: making clips is paid.
    confirm: bool = False


THV2_FINALIZE_UNCONFIRMED = (
    "Making clips costs money, so it starts only from the Make videos (or "
    "Update clips) button. Nothing was started. Reload the page if it keeps "
    "saying this."
)


@router.post("/talking-head-v2/{run_id}/finalize")
async def th_v2_finalize(run_id: int, body: Optional[THV2FinalizeRequest] = None) -> dict:
    """Kick Veo for the clips that need it (scoped: missing, failed or out
    of date; fresh clips are kept). Single-scene runs fire up to 3 jobs
    (intro/main/outro), multi-scene runs N+2. Returns with
    composition_status=queued; UI should poll. When all jobs land,
    composition_status flips to `videos_ready` and the UI lets the user
    preview each clip then click `/stitch`.

    Paid, so only an explicit press may start it: the body must carry
    {"confirm": true} (409 otherwise, and nothing runs). Run 830 got two
    unasked rounds of Veo from a page that sent this on entering Videos.
    """
    if not (body and body.confirm):
        raise HTTPException(409, THV2_FINALIZE_UNCONFIRMED)
    return await th_v2_service.start_videos_dispatch(run_id)


@router.post("/talking-head-v2/{run_id}/retry-video")
async def th_v2_retry_video(run_id: int, body: TalkingHeadRetryRequest) -> dict:
    """Retry ONE of the three Veo clips (intro/main/outro) with feedback.

    Inserts a NEW yas_animations row, points the run's metadata at it,
    and flips `composition_status` back to `queued` so the user has to
    re-press Stitch once the new clip lands.
    """
    return await th_v2_service.retry_video(
        run_id=run_id, which=body.which, feedback_text=body.feedback_text,
    )


class THV2StitchRequest(BaseModel):
    # The reel's inputs_sha as the page last read it: a reel changed since
    # (another tab) is a 409 instead of a render of something unseen.
    expect_sha: Optional[str] = Field(default=None, max_length=64)


@router.post("/talking-head-v2/{run_id}/stitch")
async def th_v2_stitch(run_id: int, body: Optional[THV2StitchRequest] = None) -> dict:
    """Finalize: freeze the reel (which clip, which version, where each
    ends) and hand it to this instance's poller. Valid from videos_ready,
    ready or failed once every included clip is made — so a reel can be
    finalized again; the last good one stays served meanwhile. The body is
    optional (an older page sends none)."""
    return await th_v2_service.finalize_reel(run_id, expect_sha=body.expect_sha if body else None)


class THV2ReelChoiceRequest(BaseModel):
    # Partial: a key left out is kept. pick: a version id, or null for the
    # original; end_frame: the scene's end in the reel's 30 fps frames, or
    # null for automatic; start_frame: a main scene's start in the same
    # frames (from its clip's first), or null for its first frame.
    pick: Optional[int] = None
    end_frame: Optional[int] = None
    start_frame: Optional[int] = Field(default=None, ge=0, le=900)


_REEL_TARGET = r"^(scene_\d{1,2}|intro|outro)$"


@router.get("/talking-head-v2/{run_id}/reel")
async def th_v2_reel(run_id: int) -> dict:
    """The reel as it stands, scene by scene (what ships, where each ends,
    a version still being made) and its inputs_sha (Finalize's
    expect_sha). Light: no studio work, one read of the versions named."""
    from ..services import th_reel

    return await th_reel.reel_of(run_id)


@router.put("/talking-head-v2/{run_id}/reel/{target}")
async def th_v2_reel_choice(
    run_id: int, body: THV2ReelChoiceRequest,
    target: str = FastAPIPath(..., pattern=_REEL_TARGET),
) -> dict:
    """A scene's pick (the version that goes in the reel; null: the
    original), end (null: automatic) and/or start (a main scene's; null:
    its first frame). 409 "Made on an older clip." for a version of the
    scene's previous clip; 422 for an end or a start that would leave the
    scene under a second. The whole reel block back, with its new
    inputs_sha."""
    from ..services import th_reel

    return await th_reel.put_choice(run_id, target, {k: getattr(body, k) for k in body.model_fields_set})


class THV2MusicRequest(BaseModel):
    # The vibe, in the user's words ("slow jazz piano"); the service trims it
    # and wants 1..200 characters.
    brief: str = Field(default="", max_length=1000)


class THV2MusicApplyRequest(BaseModel):
    on: bool
    level: Optional[Literal["soft", "medium", "loud"]] = None
    # "Remove music": off, and the panel starts over (the files are kept).
    remove: bool = False


@router.post("/talking-head-v2/{run_id}/music")
async def th_v2_music(run_id: int, body: THV2MusicRequest) -> dict:
    """Background music (Final): ask Suno for an instrumental track in this
    vibe. Paid, so only this explicit press starts it; the poller collects
    the track and lays it under the finished reel. 409 without a finished
    reel or while a track is being made. The whole run back."""
    from ..services import th_reel_music

    await th_reel_music.start_track(run_id, body.brief)
    return await th_v2_service.fetch_run(run_id)


@router.post("/talking-head-v2/{run_id}/music/apply")
async def th_v2_music_apply(run_id: int, body: THV2MusicApplyRequest) -> dict:
    """The music on (laid under the reel being served, at `level`; seconds,
    free) or off. The reel without music is never changed. The whole run
    back."""
    from ..services import th_reel_music

    await th_reel_music.apply_mix(run_id, body.on, body.level, remove=body.remove)
    return await th_v2_service.fetch_run(run_id)


@router.get("/talking-head-v2/{run_id}")
async def th_v2_fetch(run_id: int) -> dict:
    """Full run state — preview + intro/outro + Veo + composition status."""
    return await th_v2_service.fetch_run(run_id)


@router.post("/talking-head-v2/{run_id}/save-to-assets")
async def th_v2_save(run_id: int, body: TalkingHeadSaveRequest) -> dict:
    return await th_v2_service.save_to_assets(
        run_id=run_id, display_name=body.display_name,
    )


class THV2OpenInEditorRequest(BaseModel):
    mode: str  # "clips" | "final"


@router.post("/talking-head-v2/{run_id}/open-in-editor")
async def th_v2_open_in_editor(run_id: int, body: THV2OpenInEditorRequest) -> dict:
    """Create a NEW video-editor project seeded from this run (clips or final)
    and return {project_id}. The frontend then deep-links to it."""
    if body.mode not in ("clips", "final"):
        raise HTTPException(400, "mode must be 'clips' or 'final'")
    return await th_v2_service.open_in_editor(run_id=run_id, mode=body.mode)


@router.get("/talking-head-v2/recent")
async def th_v2_recent(limit: int = 20) -> dict:
    items = await th_v2_service.list_recent(limit=max(1, min(int(limit), 50)))
    return {"items": items}


@router.get("/talking-head-v2/{run_id}/file")
async def th_v2_main_file(run_id: int):
    """Stream the main keyframe image."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT local_path, storage_bucket, storage_path, mime_type
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                """,
                (run_id, th_v2_service.MODE_PREVIEW),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "main keyframe not found")
    return await _serve_th_file(
        local_path=row.get("local_path"),
        storage_bucket=row.get("storage_bucket"),
        storage_path=row.get("storage_path"),
        mime_type=row.get("mime_type"),
        default_mime="image/png",
        label="main keyframe",
    )


@router.get("/talking-head-v2/cards/previous")
async def th_v2_previous_cards(
    role: Literal["intro", "outro"], exclude_run_id: Optional[int] = None, limit: int = 30,
) -> dict:
    """The intro/outro picker's "Previous videos": each earlier reel's
    current `role` card, newest first, with its thumbnail URL, text, shape
    and the finished clip reused when it is picked as it is."""
    items = await th_v2_service.list_previous_cards(
        role, exclude_run_id=exclude_run_id, limit=max(1, min(int(limit), 60)),
    )
    return {"items": items}


@router.get("/talking-head-v2/cards/{card_id}/file")
async def th_v2_card_file(card_id: int):
    """Stream an intro or outro card."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT local_path, mime_type, mode
                  FROM yas_generations
                 WHERE id = %s AND mode IN (%s, %s)
                """,
                (card_id, th_v2_service.MODE_INTRO, th_v2_service.MODE_OUTRO),
            )
            row = await cur.fetchone()
    if not row or not row.get("local_path"):
        raise HTTPException(404, "card image not found")
    p = th_v2_service._abs_from_rel(row["local_path"])
    if not p.exists():
        raise HTTPException(410, "card image file missing on disk")
    # Card/scene keyframe paths are unique per row (never upserted), so an
    # immutable year is honest here. Starlette's FileResponse has no
    # if-none-match branch, so a 304 is impossible on this route — the header is
    # what saves the round trip, not a validator.
    return FileResponse(
        str(p), media_type=row.get("mime_type") or "image/png",
        headers=_MEDIA_CACHE_HEADERS,
    )


@router.get("/talking-head-v2/scenes/{scene_id}/file")
async def th_v2_scene_file(scene_id: int):
    """v6 — stream a multi-scene keyframe (MODE_SCENE row)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT local_path, mime_type
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                """,
                (scene_id, th_v2_service.MODE_SCENE),
            )
            row = await cur.fetchone()
    if not row or not row.get("local_path"):
        raise HTTPException(404, "scene keyframe not found")
    p = th_v2_service._abs_from_rel(row["local_path"])
    if not p.exists():
        raise HTTPException(410, "scene keyframe file missing on disk")
    # Card/scene keyframe paths are unique per row (never upserted), so an
    # immutable year is honest here. Starlette's FileResponse has no
    # if-none-match branch, so a 304 is impossible on this route — the header is
    # what saves the round trip, not a validator.
    return FileResponse(
        str(p), media_type=row.get("mime_type") or "image/png",
        headers=_MEDIA_CACHE_HEADERS,
    )


@router.get("/talking-head-v2/{run_id}/video")
async def th_v2_video_file(run_id: int):
    """Stream the MAIN Veo MP4 (legacy alias for /video/main)."""
    return await _th_v2_video_role(run_id, "main")


@router.get("/talking-head-v2/{run_id}/video/{role}")
async def th_v2_video_file_role(run_id: int, role: str):
    """Stream the Veo MP4 for a specific role: intro | main | outro."""
    return await _th_v2_video_role(run_id, role)


async def _th_v2_video_role(run_id: int, role: str):
    # v6 — accept scene_<N> for multi-scene runs in addition to legacy roles.
    scene_idx: int | None = None
    if role.startswith("scene_"):
        try:
            scene_idx = int(role.split("_", 1)[1])
        except (ValueError, IndexError):
            raise HTTPException(400, f"invalid scene role: {role!r}")
    elif role not in ("intro", "main", "outro"):
        raise HTTPException(400, "role must be intro | main | outro | scene_<N>")

    run = await th_v2_service._fetch_run_row(run_id)
    md = run.get("metadata") or {}

    if scene_idx is not None:
        scenes = (md.get("scenes") or [])
        if scene_idx < 0 or scene_idx >= len(scenes):
            raise HTTPException(404, f"scene_{scene_idx} not in this run")
        anim_id = scenes[scene_idx].get("animation_id")
        label = f"scene_{scene_idx}"
    else:
        key = {
            "intro": "intro_animation_id",
            "main":  "main_animation_id",
            "outro": "outro_animation_id",
        }[role]
        anim_id = md.get(key) or (md.get("veo_animation_id") if role == "main" else None)
        label = role

    if not anim_id:
        raise HTTPException(404, f"no {label} animation linked to this run")
    anim = await th_v2_service._fetch_animation_row(int(anim_id))
    if not anim:
        raise HTTPException(404, f"no {label} animation row found")

    # Three-tier resolution. The local-disk MP4 is only a transient cache —
    # the durable copy lives in the Supabase bucket (uploaded by
    # yas_animations_poller._mark_ready). Serve from disk when present, else
    # recover from the bucket so a wiped cache doesn't 410 a ready video.
    # 1) Local copy present → stream it (fast path).
    local_rel = anim.get("local_path")
    if local_rel:
        p = th_v2_service._abs_from_rel(local_rel)
        if p.exists():
            return FileResponse(
                str(p), media_type="video/mp4", headers=_MEDIA_CACHE_HEADERS,
            )

    # 2) Local copy gone but the bucket has it → 307-redirect to a short-lived
    #    signed URL. Supabase signed object URLs honour range requests, so
    #    <video> seeking keeps working straight from the CDN.
    if anim.get("storage_bucket") and anim.get("storage_path"):
        signed = await asset_storage.url_for(
            storage_bucket=anim["storage_bucket"],
            storage_path=anim["storage_path"],
            local_path=None,
            expires=3600,
        )
        if signed:
            return RedirectResponse(signed, status_code=307)

    # 3) Neither local nor bucket has it → genuine loss.
    raise HTTPException(
        410, f"{label} Veo MP4 missing (no local copy and no bucket object)"
    )


@router.get("/talking-head-v2/{run_id}/composition/file")
async def th_v2_composition_file(run_id: int, download: bool = False, music: bool = False):
    """Stream the final stitched composition MP4. `?download=1` forces a
    save-to-disk (Content-Disposition: attachment) instead of inline playback —
    used by the Finalization "Download video" button. `?music=1`: the music
    version instead (services/th_reel_music), its own file beside the reel."""
    from ..services import th_reel

    run = await th_v2_service._fetch_run_row(run_id)
    md = run.get("metadata") or {}
    if music:
        return await _th_v2_music_file(run_id, md, download)
    # A reel finalized again (render_owner) is served from its last good file
    # whatever its status — a new render under way or failed keeps it on
    # screen. A legacy render rewrites final.mp4 in place, so it is served
    # only when ready.
    owned = th_reel.owned(md)
    if not owned and md.get("composition_status") != th_v2_service.COMP_READY:
        raise HTTPException(409, "Composition not ready")
    # Three-tier resolution (mirrors the Veo route): local-disk cache → signed
    # bucket URL (so a redeploy that wiped the disk doesn't 410 a finished
    # stitch) → genuine 410. Never cached as immutable: the file behind this
    # URL changes when the reel is finalized again (pages ask for ?v=<seq>),
    # so the browser revalidates (ETag / Last-Modified).
    comp_rel = md.get("composition_path")
    if owned and not comp_rel:
        raise HTTPException(409, "Composition not ready")
    if comp_rel:
        p = th_v2_service._abs_from_rel(comp_rel)
        if p.exists():
            headers = dict(_REEL_CACHE_HEADERS)
            if download:
                headers["Content-Disposition"] = f'attachment; filename="talking-head-{run_id}.mp4"'
            return FileResponse(str(p), media_type="video/mp4", headers=headers)
    # The one bucket key per run is overwritten by each backup: it is the
    # reel being served only when it was made from the good seq.
    if (md.get("composition_storage_bucket") and md.get("composition_storage_path")
            and (not owned or th_reel.bucket_current(md))):
        # The download name is signed in by url_for; never append to the URL.
        signed = await asset_storage.url_for(
            storage_bucket=md["composition_storage_bucket"],
            storage_path=md["composition_storage_path"],
            local_path=None,
            expires=3600,
            download_name=f"talking-head-{run_id}.mp4" if download else None,
        )
        if signed:
            return RedirectResponse(signed, status_code=307)
    raise HTTPException(410, "Composition file missing (no local copy and no bucket object)")


async def _th_v2_music_file(run_id: int, md: dict, download: bool):
    """The reel's music version: its last mix, whatever the switch says (a
    page may still be playing it) — this disk first, then its bucket copy.
    Revalidated like the reel (?v=<seq>&m=<mix> name the version)."""
    from ..services import th_reel_music

    mix = th_reel_music.served_mix(md)
    if mix is None:
        raise HTTPException(409, "No music on this reel yet")
    name = f"talking-head-{run_id}-music.mp4"
    if mix["path"] is not None:
        headers = dict(_REEL_CACHE_HEADERS)
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return FileResponse(str(mix["path"]), media_type="video/mp4", headers=headers)
    if mix["bucket"] and mix["key"]:
        signed = await asset_storage.url_for(
            storage_bucket=mix["bucket"], storage_path=mix["key"], local_path=None,
            expires=3600, download_name=name if download else None,
        )
        if signed:
            return RedirectResponse(signed, status_code=307)
    raise HTTPException(410, "Music file missing (no local copy and no bucket object)")


# ---------------------------------------------------------------------------
# TALKING HEAD v2 — editor endpoints (edit-in-place, exclusion, batch, restore)
# ---------------------------------------------------------------------------


class THV2EditRequest(BaseModel):
    topic: Optional[str] = None
    scene_count: Optional[int] = None
    restore_archived: bool = True
    lock_character: Optional[bool] = None
    image_models: Optional[TalkingHeadImageModels] = None
    # Per-scene dialogue + ref overrides (multi-scene). Lengths align to scenes.
    scene_dialogues: Optional[list[Optional[str]]] = None
    # Per-scene hook overrides (Look-phase inline edit). null = leave unchanged.
    scene_hooks: Optional[list[Optional[str]]] = None
    scene_overrides: Optional[list[Optional[TalkingHeadSceneRefOverride]]] = None
    # Intro/outro card wording (Look-phase inline edit). Baked into the card
    # image → editing marks that card stale.
    intro_headline: Optional[str] = None
    intro_subtitle: Optional[str] = None
    outro_headline: Optional[str] = None
    outro_subtitle: Optional[str] = None
    # Global one-shot "New bg" intent for the run-level bg slot (single-scene or
    # scenes inheriting the global bg): "new" regenerates a fresh background and
    # keeps the character; "keep"/None passes the still through. Not persisted.
    bg_mode: Optional[str] = None
    # Global reference slots. Only fields explicitly present in the request are
    # applied (so sending null clears a slot, omitting it leaves it unchanged).
    character_asset_id: Optional[int] = None
    character_upload_id: Optional[int] = None
    bg_asset_id: Optional[int] = None
    bg_upload_id: Optional[int] = None
    intro_asset_id: Optional[int] = None
    intro_upload_id: Optional[int] = None
    outro_asset_id: Optional[int] = None
    outro_upload_id: Optional[int] = None
    # Direct-video intro/outro: a yas_uploads id whose mime is a video, used
    # AS-IS as the intro/outro clip (skips card-gen + Veo).
    intro_video_upload_id: Optional[int] = None
    outro_video_upload_id: Optional[int] = None
    intro_ref_mode: THV2RefMode = None
    outro_ref_mode: THV2RefMode = None
    # A previous reel's card as the intro/outro, and "Keep card text" (see
    # THV2GeneratePreviewsRequest).
    intro_card_id: Optional[int] = None
    outro_card_id: Optional[int] = None
    intro_keep_text: Optional[bool] = None
    outro_keep_text: Optional[bool] = None
    aspect_ratio: Optional[str] = None  # "9:16" | "16:9"
    video_quality: Optional[str] = None  # "normal" | "quality"
    # The Look step's Speaker field: who is on camera when no character is
    # picked. Free text, or a chip ("Emirati man" / "Emirati woman") that the
    # service turns into a default cast. Re-shoots the scenes showing the
    # speaker once the run has stills.
    speaker_subject: Optional[str] = Field(default=None, max_length=1000)
    # How close the camera is: the run's (the Look step's Framing), and per
    # scene (a Stills tile's framing buttons: null = unchanged, "inherit" =
    # follow the run's). A still whose framing changes is re-shot at it.
    framing: THV2Framing = None
    scene_framings: Optional[list[THV2SceneFraming]] = None


class THV2ExcludeRequest(BaseModel):
    which: str            # intro | outro | main | scene_<N>
    excluded: bool


class TalkingHeadSegmentQualityRequest(BaseModel):
    which: str            # intro | outro | main | scene_<N>
    quality: str          # "normal" | "quality"


class THV2BatchItem(BaseModel):
    which: str            # main | intro | outro | scene_<N>
    feedback_text: Optional[str] = None
    image_models: Optional[TalkingHeadImageModels] = None


class THV2BatchRequest(BaseModel):
    items: list[THV2BatchItem]
    target: str = "both"  # images | videos | both


class THV2RestoreSceneRequest(BaseModel):
    archived_index: int


_TH_V2_REF_FIELDS = (
    "character_asset_id", "character_upload_id", "bg_asset_id", "bg_upload_id",
    "intro_asset_id", "intro_upload_id", "outro_asset_id", "outro_upload_id",
    "intro_video_upload_id", "outro_video_upload_id",
    "intro_ref_mode", "outro_ref_mode",
    "intro_card_id", "outro_card_id", "intro_keep_text", "outro_keep_text",
)


@router.post("/talking-head-v2/{run_id}/edit")
async def th_v2_edit(run_id: int, body: THV2EditRequest) -> dict:
    """Edit an existing run in place: topic, per-scene dialogue, scene count,
    references, per-scene overrides, image models, lock_character. Marks only
    affected clips stale; never deletes or auto-regenerates existing videos.
    """
    provided = body.model_fields_set
    refs_patch: dict | None = None
    ref_keys = [k for k in _TH_V2_REF_FIELDS if k in provided]
    if ref_keys:
        refs_patch = {k: getattr(body, k) for k in ref_keys}
    image_models = body.image_models.model_dump() if body.image_models else None
    overrides = (
        [(o.model_dump() if o is not None else None) for o in body.scene_overrides]
        if body.scene_overrides is not None else None
    )
    return await th_v2_service.edit_run(
        run_id=run_id,
        topic=body.topic if "topic" in provided else None,
        scene_count=body.scene_count if "scene_count" in provided else None,
        restore_archived=body.restore_archived,
        lock_character=body.lock_character if "lock_character" in provided else None,
        image_models=image_models,
        refs_patch=refs_patch,
        scene_dialogues=body.scene_dialogues,
        scene_hooks=body.scene_hooks,
        scene_overrides=overrides,
        bg_mode=body.bg_mode if "bg_mode" in provided else None,
        aspect_ratio=body.aspect_ratio if "aspect_ratio" in provided else None,
        video_quality=body.video_quality if "video_quality" in provided else None,
        intro_headline=body.intro_headline if "intro_headline" in provided else None,
        intro_subtitle=body.intro_subtitle if "intro_subtitle" in provided else None,
        outro_headline=body.outro_headline if "outro_headline" in provided else None,
        outro_subtitle=body.outro_subtitle if "outro_subtitle" in provided else None,
        speaker_subject=body.speaker_subject if "speaker_subject" in provided else None,
        framing=body.framing if "framing" in provided else None,
        scene_framings=(
            list(body.scene_framings)
            if "scene_framings" in provided and body.scene_framings is not None else None
        ),
    )


@router.post("/talking-head-v2/{run_id}/exclude")
async def th_v2_exclude(run_id: int, body: THV2ExcludeRequest) -> dict:
    return await th_v2_service.set_clip_excluded(
        run_id=run_id, which=body.which, excluded=body.excluded,
    )


@router.post("/talking-head-v2/{run_id}/segment-quality")
async def th_v2_segment_quality(run_id: int, body: TalkingHeadSegmentQualityRequest) -> dict:
    return await th_v2_service.set_segment_quality(
        run_id=run_id, which=body.which, quality=body.quality,
    )


@router.post("/talking-head-v2/{run_id}/batch-regen")
async def th_v2_batch_regen(run_id: int, body: THV2BatchRequest) -> dict:
    return await th_v2_service.batch_regen(
        run_id=run_id,
        items=[i.model_dump() for i in body.items],
        target=body.target,
    )


@router.post("/talking-head-v2/{run_id}/restore-scene")
async def th_v2_restore_scene(run_id: int, body: THV2RestoreSceneRequest) -> dict:
    return await th_v2_service.restore_scene(
        run_id, archived_index=body.archived_index,
    )


# ===========================================================================
# VIDEO EDITOR — multi-track NLE category.
#
# Pure assembly/render: no Veo, no Gemini, no RAI-gated calls. The whole
# editable timeline lives in yas_generations.metadata.video_edit (JSONB). The
# frontend autosaves the doc; the export poller drives the Remotion render.
# Backed by services/yas_video_editor.py + workers/yas_video_edit_composition_poller.py.
# ===========================================================================


class VECreateProjectRequest(BaseModel):
    title: Optional[str] = None
    aspect: Optional[str] = None  # "9:16" | "16:9" | "1:1"
    preset: Optional[str] = None  # None | "b_roll" (opt-in B-roll auto-edit)


class VESaveTimelineRequest(BaseModel):
    project: dict
    tracks: list
    clips: list
    base_rev: Optional[int] = None


@router.post("/video-editor/projects")
async def ve_create_project(body: VECreateProjectRequest) -> dict:
    return await ve_service.create_project(
        title=body.title or "Untitled project",
        aspect=body.aspect or ve_service.DEFAULT_ASPECT,
        preset=body.preset,
    )


# NOTE: literal-path routes (/sources, /upload) MUST be declared before the
# parametrized `/{project_id}` route — FastAPI matches in registration order and
# `{project_id}` would otherwise capture "sources"/"upload" and 422 on int parse.

@router.get("/video-editor/sources")
async def ve_list_sources(
    q: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Pickable sources for the media bin: ready Veo animations, stitched
    finals, keyframe/poster stills and video uploads — each tagged with a
    `category` for the picker's filter chips."""
    return await ve_service.list_sources(q=q, kind=kind, limit=limit, offset=offset)


@router.post("/video-editor/upload")
async def ve_upload(file: UploadFile = File(...)) -> dict:
    """Upload a video to use as a source clip. Probes duration + thumbnail."""
    raw = await file.read()
    return await ve_service.import_upload(
        raw=raw, filename=file.filename, mime=file.content_type,
    )


@router.get("/video-editor/sources/still/{source_id}/file")
async def ve_source_still_file(source_id: int):
    """Stream a still-image source (keyframe / card / poster) — used as both the
    picker tile thumbnail and the Player's image-clip sourceUrl."""
    res = await ve_service.resolve_still_serving(source_id)
    if not res:
        raise HTTPException(410, f"still #{source_id} source unavailable")
    return FileResponse(
        str(res["path"]), media_type=res.get("mime") or "image/png",
        headers=_MEDIA_CACHE_HEADERS,
    )


@router.get("/video-editor/sources/{kind}/{source_id}/video")
async def ve_source_video(kind: str, source_id: int):
    """Range-served source clip for the <Player>. Local file or 307→signed URL."""
    if kind not in ("animation", "upload", "composition"):
        raise HTTPException(400, "kind must be animation | upload | composition")
    res = await ve_service.resolve_source_serving(kind, source_id)
    if not res:
        raise HTTPException(410, f"{kind} #{source_id} source unavailable")
    if res.get("redirect"):
        return RedirectResponse(res["redirect"], status_code=307)
    return FileResponse(
        str(res["path"]), media_type="video/mp4", headers=_MEDIA_CACHE_HEADERS,
    )


@router.get("/video-editor/sources/{kind}/{source_id}/thumb")
async def ve_source_thumb(kind: str, source_id: int):
    """Cached JPG thumbnail for the picker tile (stills serve their own file)."""
    if kind not in ("animation", "upload", "composition"):
        raise HTTPException(400, "kind must be animation | upload | composition")
    p = await ve_service.resolve_source_thumb(kind, source_id)
    if not p or not p.exists():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(str(p), media_type="image/jpeg", headers=_MEDIA_CACHE_HEADERS)


@router.get("/video-editor/assets/fonts")
async def ve_list_fonts() -> dict:
    """Brand fonts (Assets Library, asset_kind='font') for the Text tab's font
    picker. Curated Google fonts are baked into the composition on the client."""
    return await ve_service.list_fonts()


# NOTE: /assets/audio/upload MUST precede /assets/audio/{upload_id}/file so the
# literal "upload" isn't captured as an int upload_id.
@router.post("/video-editor/assets/audio/upload")
async def ve_audio_upload(file: UploadFile = File(...)) -> dict:
    """Upload a music/audio file for an audio track. Probes duration."""
    raw = await file.read()
    return await ve_service.import_audio(raw=raw, filename=file.filename, mime=file.content_type)


@router.get("/video-editor/assets/audio")
async def ve_list_audio(limit: int = 200) -> dict:
    """Uploaded audio/music files for the Audio tab."""
    return await ve_service.list_audio(limit=limit)


@router.get("/video-editor/assets/audio/{upload_id}/file")
async def ve_audio_file(upload_id: int):
    """Range-served audio file for the <Audio> Player layer (mime-correct)."""
    res = await ve_service.resolve_audio_serving(upload_id)
    if not res:
        raise HTTPException(410, f"audio #{upload_id} unavailable")
    return FileResponse(str(res["path"]), media_type=res.get("mime") or "audio/mpeg", headers=_MEDIA_CACHE_HEADERS)


# ---------------------------------------------------------------------------
# VIDEO EDITOR — "Clone my voice": ONE app-wide ElevenLabs instant clone
# (services/yas_voice_clone.py). Synchronous — no job machinery.
#
# NOTE: these literal /video-editor/ai/* paths MUST precede the parametrized
# GET /video-editor/{project_id} below, or "ai" would be parsed as project_id.
# ---------------------------------------------------------------------------

_VOICE_SAMPLE_MAX_BYTES = 12 * 1024 * 1024   # per file
# ElevenLabs IVC works from a few seconds (more = better quality; the UI still
# suggests 30s+). Keep only a tiny sanity floor so an accidental 1-2s tap is
# caught — anything reasonable should clone. (Was 25s, which rejected normal
# short recordings before they ever reached ElevenLabs.)
_VOICE_SAMPLE_MIN_TOTAL_SEC = 4.0


def _transcode_voice_sample_to_mp3(raw: bytes, suffix: str) -> Optional[tuple[bytes, float]]:
    """Transcode an uploaded voice sample to a clean mono mp3 and measure it.

    Browser MediaRecorder produces a header-less webm whose duration ffprobe
    CANNOT read (it reports ~0s), which made the duration floor reject perfectly
    good recordings. ffmpeg decodes the actual audio, so the re-encoded mp3 has a
    reliable duration AND is a format ElevenLabs definitely accepts. Returns
    ``(mp3_bytes, duration_sec)`` or ``None`` if the audio can't be decoded
    (corrupt / empty / not-audio take)."""
    import imageio_ffmpeg
    import subprocess
    import tempfile

    src: Optional[Path] = None
    dst: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix or ".webm", delete=False) as f:
            f.write(raw)
            src = Path(f.name)
        dst = src.with_name(src.stem + "_voice.mp3")
        cmd = [
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(src),
            "-vn", "-ac", "1", "-ar", "44100", "-b:a", "128k", "-f", "mp3", str(dst),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            log.warning("[voice-clone] ffmpeg transcode failed: %s", (proc.stderr or "")[:300])
            return None
        data = dst.read_bytes()
        dur = ve_service._probe_audio_duration(dst) or 0.0
        return data, float(dur)
    except Exception:  # noqa: BLE001 — any failure ⇒ treat the take as unreadable
        log.exception("[voice-clone] sample transcode error")
        return None
    finally:
        for p in (src, dst):
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass


@router.post("/video-editor/ai/voice-clone")
async def ve_voice_clone_create(
    files: list[UploadFile] = File(...),
    name: Optional[str] = Form(None),
) -> dict:
    """Create/replace THE app-wide voice clone from one or more audio samples.
    Replace-on-set: yas_voice_clone deletes the previous ElevenLabs voice so
    only one slot is ever used. Returns {voice_id, name, created_at}."""
    provider = media_get_voice_provider()
    if not provider.available():
        raise HTTPException(
            422, "Voice cloning needs ELEVENLABS_API_KEY — add it to backend/.env and retry.",
        )

    samples: list[tuple[str, bytes]] = []
    total_sec = 0.0
    for i, f in enumerate(files):
        raw = await f.read()
        if not raw:
            raise HTTPException(400, f"empty file {f.filename!r}")
        if len(raw) > _VOICE_SAMPLE_MAX_BYTES:
            raise HTTPException(
                422, f"{f.filename}: voice samples are capped at 12 MB per file",
            )
        # Always transcode to mp3 first. This decodes the real audio (so the
        # duration is correct even for a header-less MediaRecorder webm that
        # ffprobe reads as ~0s) and hands ElevenLabs a format it always accepts.
        suffix = Path(f.filename or "").suffix.lower()
        out = await asyncio.to_thread(_transcode_voice_sample_to_mp3, raw, suffix)
        if out is None:
            raise HTTPException(
                422,
                "Couldn't read that recording — please record again (speak for a "
                "few seconds) or upload an audio file.",
            )
        mp3_bytes, dur = out
        total_sec += dur
        base = Path(f.filename or f"sample{i + 1}").stem or f"sample{i + 1}"
        samples.append((f"{base}.mp3", mp3_bytes))

    if total_sec < _VOICE_SAMPLE_MIN_TOTAL_SEC:
        raise HTTPException(
            422,
            f"Your recording is only {total_sec:.0f}s — record at least "
            f"~{int(_VOICE_SAMPLE_MIN_TOTAL_SEC)} seconds of speech for a clone.",
        )

    clone_name = (name or "").strip() or "My voice"
    try:
        voice_id = await provider.clone_voice(name=clone_name, samples=samples)
    except MediaProviderError as e:
        raise HTTPException(422, str(e)) from e
    return await voice_clone_service.set_cloned_voice(voice_id, clone_name)


@router.get("/video-editor/ai/voice-clone")
async def ve_voice_clone_get() -> Optional[dict]:
    """The stored clone {voice_id, name, created_at}, or null when none."""
    return await voice_clone_service.get_cloned_voice()


@router.delete("/video-editor/ai/voice-clone")
async def ve_voice_clone_delete() -> dict:
    """Delete the clone (frees the ElevenLabs slot, best-effort remotely)."""
    await voice_clone_service.clear_cloned_voice()
    return {"ok": True}


@router.get("/video-editor/ai/providers")
async def ve_ai_providers(refresh: bool = False) -> dict:
    """Which reasoning brain the editor's agent is actually using, and why.

    The gateway probes Claude-on-Vertex → the direct Anthropic API → Gemini and
    uses the first that answers, so this is the one place to look when the AI
    feels dumber (or smarter) than expected. `?refresh=true` re-probes instead of
    reading the cached verdict."""
    from ..services.model_gateway import reasoner_status, resolve_reasoner

    if refresh:
        await resolve_reasoner(force=True)
    return await reasoner_status()


@router.get("/video-editor/{project_id}")
async def ve_fetch_project(project_id: int) -> dict:
    return await ve_service.fetch_project(project_id)


@router.get("/video-editor/{project_id}/history")
async def ve_history(project_id: int, limit: int = 100) -> dict:
    """Saved versions of the timeline, oldest first. The editor loads them under
    Undo when a project opens, so the history survives a reload."""
    return await ve_service.list_revisions(project_id, limit=limit)


@router.patch("/video-editor/{project_id}/timeline")
async def ve_save_timeline(project_id: int, body: VESaveTimelineRequest) -> dict:
    return await ve_service.save_timeline(
        project_id,
        project=body.project,
        tracks=body.tracks,
        clips=body.clips,
        base_rev=body.base_rev,
    )


@router.post("/video-editor/{project_id}/export")
async def ve_export(project_id: int) -> dict:
    """Flag the project for export; the poller renders it via Remotion."""
    return await ve_service.export_project(project_id)


@router.get("/video-editor/{project_id}/composition/file")
async def ve_composition_file(project_id: int, download: bool = False):
    """Stream the exported MP4 once the render is ready. `?download=1` forces a
    save-to-disk (Content-Disposition: attachment) instead of inline playback —
    used by the Finalization "Download video" button.

    Three-tier resolution (mirrors the Veo route): local-disk cache → signed
    bucket URL (so a redeploy that wiped the disk doesn't 410 a finished video)
    → genuine 410."""
    loc = await ve_service.composition_locator(project_id)
    # 1) Local copy present → stream it (fast path).
    if loc["local"] is not None:
        headers = _MEDIA_CACHE_HEADERS
        if download:
            headers = {**_MEDIA_CACHE_HEADERS, "Content-Disposition": f'attachment; filename="video-{project_id}.mp4"'}
        return FileResponse(str(loc["local"]), media_type="video/mp4", headers=headers)
    # 2) Local gone but the bucket has it → 307-redirect to a short-lived signed
    #    URL (the object store honours range requests, so <video> seeking works).
    if loc["storage_bucket"] and loc["storage_path"]:
        # The download name is signed in by url_for; never append to the URL.
        signed = await asset_storage.url_for(
            storage_bucket=loc["storage_bucket"],
            storage_path=loc["storage_path"],
            local_path=None,
            expires=3600,
            download_name=f"video-{project_id}.mp4" if download else None,
        )
        if signed:
            return RedirectResponse(signed, status_code=307)
    # 3) Neither local nor bucket → genuine loss.
    raise HTTPException(410, "composition file missing (no local copy and no bucket object)")


class VEBrollReorderRequest(BaseModel):
    # Full permutation of the main video track's clip ids, in the new order.
    clip_ids: list[str]


@router.post("/video-editor/{project_id}/broll/reorder")
async def ve_broll_reorder(project_id: int, body: VEBrollReorderRequest) -> dict:
    """Re-order the B-roll clips on their track without opening the timeline UI.

    The dedicated B-roll tool sends the new order of the main video track's clip
    ids; we resolve the track they live on and replay the SAME `reorder_clips`
    op the AI engine uses (back-to-back relayout, transitions preserved)."""
    if not body.clip_ids:
        raise HTTPException(400, "clip_ids is required")
    proj = await ve_service.fetch_project(project_id)
    by_id = {c["id"]: c for c in (proj.get("clips") or [])}
    track_ids = {by_id[c]["trackId"] for c in body.clip_ids if c in by_id}
    if len(track_ids) != 1:
        raise HTTPException(400, "clips must all be on one track")
    track_id = track_ids.pop()
    from ..services import yas_video_edit_ai_ops as ve_ops
    await ve_ops.apply_plan(
        project_id,
        plan_id="broll_manual_reorder",
        ops=[{"op": "reorder_clips", "track_id": track_id, "clip_ids": body.clip_ids}],
    )
    return await ve_service.fetch_project(project_id)


# ---------------------------------------------------------------------------
# VIDEO EDITOR — AI edit engine (services/yas_video_edit_ai.py orchestrator).
#
# NOTE: these are all parametrized `/{project_id}/ai/*` routes, so route order
# vs the literal /video-editor/* routes above does not matter — but keep them
# AFTER the main video-editor block so the category reads top-to-bottom.
# Each POST enqueues ONE background job per project (409 while one is active);
# poll GET /ai/status for progress. Suno music jobs park at waiting_provider
# and are finished by workers/yas_video_edit_ai_poller.py.
# ---------------------------------------------------------------------------


class VEAIAnalyzeRequest(BaseModel):
    force: bool = False


class VEAIAgentRequest(BaseModel):
    instruction: str
    clip_id: Optional[str] = None   # the clip the user has selected, if any


class VEAIPopupsRequest(BaseModel):
    text: str
    hint: Optional[str] = None


class VEAIComposeRequest(BaseModel):
    keys: Optional[list[str]] = None
    style: Optional[str] = None
    generate_bookends: bool = False  # also frame the result intro → main → outro


class VEAIEditRequest(BaseModel):
    instruction: Optional[str] = None


class VEAIVoiceRequest(BaseModel):
    clip_id: str
    voice_id: Optional[str] = None
    use_cloned: bool = False                 # speak through the app-wide voice clone
    use_as_is: bool = False                  # pre-uploaded audio replaces the voice verbatim
    source_upload_id: Optional[int] = None   # required when use_as_is (audio-upload id)


class VEAIMusicRequest(BaseModel):
    mode: Optional[str] = None   # auto | suno | library
    query: Optional[str] = None


class VEAISfxRequest(BaseModel):
    clip_id: Optional[str] = None
    query: Optional[str] = None
    mode: Optional[str] = None


class VEAIBookendRequest(BaseModel):
    role: str = "both"   # intro | outro | both
    reuse: bool = True   # reuse an already-rendered bookend when one is ready


class VEAIBrollRequest(BaseModel):
    intro_outro: bool = True        # frame the montage with intro/outro cards
    bookend_style: str = "veo"      # veo (rendered bookends) | light (legacy text cards)
    bookend_mode: str = "generate"  # generate (Veo from topics) | upload (use own clips)
    music: bool = True              # lay a ducked music bed
    music_mode: Optional[str] = None  # auto | suno | library
    title: Optional[str] = None     # legacy: single override for both bookends
    intro_topic: Optional[str] = None  # per-role topic for the generated intro
    outro_topic: Optional[str] = None  # per-role topic for the generated outro
    intro_upload_id: Optional[int] = None  # 'upload' mode: clip pinned FIRST
    outro_upload_id: Optional[int] = None  # 'upload' mode: clip pinned LAST


class VEAIChatMsg(BaseModel):
    role: str           # "user" | "assistant"
    text: str


class VEAIChatRequest(BaseModel):
    message: str
    history: list[VEAIChatMsg] = []        # recent turns — conversation memory
    selected_clip_ids: list[str] = []      # multi-selection the command scopes to
    selected_clip_id: Optional[str] = None  # back-compat (single)
    playhead_frame: int = 0                # where new popups/overlays default to
    attachments: list[dict] = []           # sidebar items dragged onto the chat


@router.post("/video-editor/{project_id}/ai/chat")
async def ve_ai_chat(project_id: int, body: VEAIChatRequest) -> dict:
    """Conversational op-planner: sees the conversation + every clip + the
    selection + any dragged-in attachments, then either applies precise ops
    directly (one undo step) or routes to a generation feature. Returns
    {job, reply, feature, applied?, rev?} — job is null for direct edits."""
    sel = list(body.selected_clip_ids or [])
    if body.selected_clip_id and body.selected_clip_id not in sel:
        sel.append(body.selected_clip_id)
    history = [{"role": m.role, "text": m.text} for m in (body.history or [])]
    return await ve_ai_service.route_chat(
        project_id, body.message, history, sel,
        playhead_frame=int(body.playhead_frame or 0),
        attachments=list(body.attachments or []),
    )


@router.post("/video-editor/{project_id}/ai/analyze")
async def ve_ai_analyze(project_id: int, body: Optional[VEAIAnalyzeRequest] = None) -> dict:
    """(Re)build the per-source media analysis cache as a background job."""
    job = await ve_ai_service.start_job(
        project_id, "analyze", {"force": bool(body.force) if body else False},
    )
    return {"job": job}


@router.get("/video-editor/{project_id}/ai/status")
async def ve_ai_status(project_id: int) -> dict:
    """Current AI job blob + whether the analysis cache is ready."""
    return await ve_ai_service.get_status(project_id)


@router.post("/video-editor/{project_id}/ai/popups")
async def ve_ai_popups(project_id: int, body: VEAIPopupsRequest) -> dict:
    """Plan + apply text popups (Gemini picks from analysis-derived windows)."""
    job = await ve_ai_service.start_job(
        project_id, "popups", {"text": body.text, "hint": body.hint},
    )
    return {"job": job}


@router.post("/video-editor/{project_id}/ai/compose")
async def ve_ai_compose(project_id: int, body: Optional[VEAIComposeRequest] = None) -> dict:
    """Re-sequence clips + add transitions for narrative flow. With
    generate_bookends=true the result is also framed intro → main → outro
    (ready bookends reused; missing ones rendered via card → Veo, async)."""
    params = (
        {"keys": body.keys, "style": body.style,
         "generate_bookends": body.generate_bookends}
        if body else {}
    )
    job = await ve_ai_service.start_job(project_id, "compose", params)
    return {"job": job}


@router.post("/video-editor/{project_id}/ai/edit")
async def ve_ai_edit(project_id: int, body: Optional[VEAIEditRequest] = None) -> dict:
    """Free-form AI edit pass (full op vocabulary, Library-grounded)."""
    params = {"instruction": body.instruction} if body else {}
    job = await ve_ai_service.start_job(project_id, "edit", params)
    return {"job": job}


@router.post("/video-editor/{project_id}/ai/agent")
async def ve_ai_agent(project_id: int, body: VEAIAgentRequest) -> dict:
    """The agentic editor: it watches the footage, reads the transcript, edits,
    previews its own work and iterates until the instruction is satisfied.

    Use this for anything needing judgement or depending on what is actually on
    screen. Progress streams into the job blob as `agent_steps`, so the UI can
    show what it is doing. Which model reasons is decided by the gateway — see
    GET /video-editor/ai/providers."""
    job = await ve_ai_service.start_job(
        project_id, "agent",
        {"instruction": body.instruction, "clip_id": body.clip_id},
    )
    return {"job": job}


@router.post("/video-editor/{project_id}/ai/voice")
async def ve_ai_voice(project_id: int, body: VEAIVoiceRequest) -> dict:
    """Replace a clip's voice (no LLM). Default: ElevenLabs speech-to-speech.
    use_cloned=true → STS through the app-wide voice clone; use_as_is=true →
    lay the pre-uploaded source_upload_id audio directly (no ElevenLabs)."""
    job = await ve_ai_service.start_job(
        project_id, "voice", {
            "clip_id": body.clip_id,
            "voice_id": body.voice_id,
            "use_cloned": body.use_cloned,
            "use_as_is": body.use_as_is,
            "source_upload_id": body.source_upload_id,
        },
    )
    return {"job": job}


@router.post("/video-editor/{project_id}/ai/music")
async def ve_ai_music(project_id: int, body: Optional[VEAIMusicRequest] = None) -> dict:
    """Add a ducked music bed (Suno when configured, else Library pick)."""
    params = {"mode": body.mode, "query": body.query} if body else {}
    job = await ve_ai_service.start_job(project_id, "music", params)
    return {"job": job}


@router.post("/video-editor/{project_id}/ai/sfx")
async def ve_ai_sfx(project_id: int, body: Optional[VEAISfxRequest] = None) -> dict:
    """Per-clip sound effect (generate via ElevenLabs, else bundled/synth)."""
    params = (
        {"clip_id": body.clip_id, "query": body.query, "mode": body.mode}
        if body else {}
    )
    job = await ve_ai_service.start_job(project_id, "sfx", params)
    return {"job": job}


@router.post("/video-editor/{project_id}/ai/bookend")
async def ve_ai_bookend(project_id: int, body: Optional[VEAIBookendRequest] = None) -> dict:
    """Generate + place an AI intro/outro (TH v2 card → Veo i2v, async). The
    job parks at waiting_provider; the AI poller places the clips when ready."""
    params = {"role": body.role, "reuse": body.reuse} if body else {}
    job = await ve_ai_service.start_job(project_id, "bookend", params)
    return {"job": job}


@router.post("/video-editor/{project_id}/ai/broll")
async def ve_ai_broll(project_id: int, body: Optional[VEAIBrollRequest] = None) -> dict:
    """One-click B-roll auto-edit (opt-in preset, deterministic — no LLM ops):
    order the clips for visual flow (CLIP edge-frame chaining) + fade joins,
    frame with intro/outro cards (light text cards or Veo bookends) and lay a
    ducked music bed. Suno/Veo halves park at waiting_provider; the AI poller
    finishes them."""
    params = body.model_dump() if body else {}
    job = await ve_ai_service.start_job(project_id, "broll", params)
    return {"job": job}


# ===========================================================================
# CREATE ANIMATION — freeform intro/outro → static frame (logo baked) → Veo
#
# Record in Postgres (yas_animation_generations); media in Supabase Storage
# (producer-media bucket). Two steps:
#   1. POST /animation/generate-frame  {prompt, aspect_ratio}
#        gpt-4o expands → gemini renders a hero frame (+ 42 logo) → upload to Supabase.
#   2. POST /animation/{id}/animate     {duration_sec, with_audio}
#        the frame → Veo i2v → status='animating'; yas_local_animation_poller
#        downloads the MP4 and flips status='ready'.
# ===========================================================================

from ..services.yas_animation_styles import (  # noqa: E402
    ANIMATE_NEGATIVE_PROMPT as ANIM_NEGATIVE_PROMPT,
    DEFAULT_STYLE,
    IMPORTED_STYLE,
    get_animation_style,
    build_motion_prompt_for_style,
)
# Shared card-render + Veo-i2v glue, reused VERBATIM by the Talking-Head v2
# intro/outro bookends (yas_talking_head_v2.py) so the two pipelines share one
# implementation instead of duplicating it.
from ..services import card_animation  # noqa: E402

ANIMATION_ASPECTS = {"9:16", "16:9"}
ANIMATION_DURATIONS = {4, 6, 8}
# Midjourney-style batch: "Generate frame" and "Explore" each render this many
# frame variants concurrently (mirrors POSTER_VARIANT_COUNT). The expensive Veo
# step stays one clip per click; only the cheap stills batch.
ANIMATION_VARIANT_COUNT = 4

# There used to be a self-imposed hourly cap on Veo animations here (10 per
# rolling hour, kept in an in-process ledger). It was removed by request: there
# is now NO local limit on how many animations can be made in an hour. What
# still protects spend is the Vertex USD budget guard (_budget_guard) and
# Google's own quota, which answers 429 on its own.


def _anim_out(rec: dict) -> dict:
    """Attach frame_url (+ file_url when ready) to an animation record."""
    out = dict(rec)
    # Legacy records (pre-style-presets) have no `style` — default to voxel so
    # the frontend always receives a valid preset key.
    out["style"] = rec.get("style") or DEFAULT_STYLE
    aid = rec.get("id")
    if aid is not None and (rec.get("frame_storage_path") or rec.get("frame_local_path")):
        out["frame_url"] = f"/api/yas-producer/animation/{aid}/frame"
    if aid is not None and rec.get("status") == "ready":
        out["file_url"] = f"/api/yas-producer/animation/{aid}/file"
    return out


class AnimationFrameRequest(BaseModel):
    prompt: str
    aspect_ratio: str = "9:16"  # "9:16" | "16:9"
    style: str = DEFAULT_STYLE  # "voxel" | "wireframe" | "popup" | "pattern"


class AnimationAnimateRequest(BaseModel):
    duration_sec: Optional[int] = None  # 4 | 6 | 8 — default 6
    with_audio: Optional[bool] = None   # default True


class AnimationVariantsRequest(BaseModel):
    parent_id: int


async def _render_animation_frame(
    expanded_prompt: str,
    *,
    aspect: str,
    poster_style: PosterStyle,
    prompt: str,
    variation_ref_path: Optional[Path] = None,
) -> dict:
    """Render ONE animation hero frame (42 logo baked in) from an already-expanded
    prompt, mirroring the poster ``_single_stage2`` recipe. Creates its own
    ``anim_store`` record and NEVER raises — failures are recorded on the record and
    returned, so a sibling in an ``asyncio.gather`` batch can still succeed.

    When ``variation_ref_path`` is given (Explore), the parent frame is sent as a
    Gemini reference part right after the logo so the child stays close to it.
    The record starts at ``status='generating'`` (invisible to Library + recent())
    and ends ``frame_ready`` or ``failed``.
    """
    rec = await anim_store.create(
        prompt=prompt,
        image_prompt_used=expanded_prompt[:8000],
        status="generating",
        aspect_ratio=aspect,
        style=poster_style.key,
    )
    anim_id = int(rec["id"])
    try:
        # Reinforce the aspect at render time too. Shared with the Talking-Head
        # bookend cards via card_animation — one render recipe, several callers.
        aspect_note = card_animation.aspect_render_note(aspect)
        logo_parts, logo_attached = card_animation.build_logo_parts()
        if not logo_attached:
            log.warning("[animation] logo not found at %s — rendering without logo", card_animation.LOGO_PATH)
        parts: list[tuple[str, Any]] = list(logo_parts)
        # Explore anchor: send the chosen frame so the child stays near it.
        if variation_ref_path is not None and Path(variation_ref_path).exists():
            parts.append(("text", VARIATION_REF_INSTRUCTION))
            parts.append(("image", Path(variation_ref_path)))
        parts.append(("text", f"{aspect_note}\n\n{expanded_prompt}"))

        image_bytes = await card_animation.render_card_image(
            parts,
            system_instruction=poster_style.gemini_stage2_system,
            timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
        )
    except VertexBudgetExceededError as exc:
        return await anim_store.update(anim_id, status="failed", error=f"budget_exceeded: {exc}"[:500]) or rec
    except asyncio.TimeoutError:
        return await anim_store.update(anim_id, status="failed", error="frame generation timed out") or rec
    except Exception as exc:
        if _is_permission_denied(exc):
            return await anim_store.update(anim_id, status="failed", error=f"permission_denied: {exc}"[:500]) or rec
        log.exception("[animation] frame render failed (anim #%d)", anim_id)
        return await anim_store.update(anim_id, status="failed", error=f"frame_failed: {type(exc).__name__}: {exc}"[:500]) or rec

    if not image_bytes:
        return await anim_store.update(anim_id, status="failed", error="empty image bytes") or rec

    ext = _ext_for_image_bytes(image_bytes)
    fmime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    up = await asset_storage.upload(
        data=image_bytes,
        path=f"animation/{anim_id}-frame.{ext}",
        mime=fmime,
        bucket=config.producer_media_bucket,
    )
    updated = await anim_store.update(
        anim_id,
        status="frame_ready",
        frame_storage_bucket=up.get("storage_bucket"),
        frame_storage_path=up.get("storage_path"),
        frame_local_path=up.get("local_path"),
    )
    log.info("[animation] frame #%d ready · %s · %s", anim_id, poster_style.key, aspect)
    return updated or rec


async def _finalize_animation_batch(
    recs: list[dict], spend_before_openai: float, spend_before_vertex: float
) -> list[dict]:
    """Split the batch's total render spend (Stage 1 + all Stage 2 frames) evenly
    across the ``frame_ready`` siblings, stamp each ``frame_cost_usd``, and return
    the records serialised with ``_anim_out`` (same cost-split as poster
    ``_run_stage2_batch``). The meters are GLOBAL, so per-frame isolation isn't
    possible mid-gather — we divide the measured total instead."""
    from ..services.openai_client import get_meter as _openai_meter
    total = max(
        0.0,
        (get_meter().spent_usd - spend_before_vertex)
        + (_openai_meter().spent_usd - spend_before_openai),
    )
    ready = [r for r in recs if r.get("status") == "frame_ready"]
    per_frame = round(total / max(len(ready), 1), 6)
    out: list[dict] = []
    for r in recs:
        if r.get("status") == "frame_ready":
            fresh = await anim_store.update(int(r["id"]), frame_cost_usd=per_frame)
            out.append(_anim_out(fresh or r))
        else:
            out.append(_anim_out(r))
    return out


@router.post("/animation/generate-frame")
async def animation_generate_frame(body: AnimationFrameRequest) -> dict:
    """Stage 1+2: expand the brief into ANIMATION_VARIANT_COUNT *distinct* concepts,
    then render that many hero frames (42 logo baked in) concurrently — a
    Midjourney-style batch, mirroring Create-Poster. Returns ``{items:[...]}``; each
    frame stays independently animatable (Veo i2v) and Explorable."""
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    aspect = (body.aspect_ratio or "9:16").strip()
    if aspect not in ANIMATION_ASPECTS:
        raise HTTPException(400, "aspect_ratio must be '9:16' or '16:9'")

    # The static frames are generated EXACTLY like Create-Poster images — same
    # Stage-1 concept pipeline and same Stage-2 Gemini render system — so each
    # frame is a real poster. Only the motion step (Veo) stays animation-specific.
    # Unknown/legacy style keys fall back to voxel.
    poster_style = get_poster_style(body.style)

    await _budget_guard()

    from ..services.openai_client import get_meter as _openai_meter
    spend_before_openai = _openai_meter().spent_usd
    spend_before_vertex = get_meter().spent_usd

    # --- Stage 1: Create-Poster's concept pipeline, asking for N DISTINCT concepts. ---
    concepts, _stage1_cost = await _poster_concepts(prompt, ANIMATION_VARIANT_COUNT, poster_style)
    expanded = [(c.get("prompt") or "").strip() for c in concepts]
    expanded = [p for p in expanded if p]
    if not expanded:
        raise HTTPException(502, "concept expansion returned empty text")

    # --- Stage 2: render each concept concurrently (per-variant isolation). ---
    recs = await asyncio.gather(*[
        _render_animation_frame(p, aspect=aspect, poster_style=poster_style, prompt=prompt)
        for p in expanded
    ])
    items = await _finalize_animation_batch(recs, spend_before_openai, spend_before_vertex)

    ready = sum(1 for r in items if r.get("status") == "frame_ready")
    log.info("[animation] batch · %d/%d frames ready · %s · %s", ready, len(items), poster_style.key, aspect)
    return {"items": items}


@router.post("/animation/variants")
async def animation_variants(body: AnimationVariantsRequest) -> dict:
    """Midjourney-style "explore" for animation frames: render ANIMATION_VARIANT_COUNT
    CLOSE variations of the chosen frame, ANCHORED on it (sent as a Gemini reference
    part) with a distinct micro-nudge each. Skips Stage 1 — the concept is inherited
    from the parent's stored image prompt. Each child is a new ``frame_ready`` record,
    independently animatable. Mirrors ``/poster/variants``."""
    await _budget_guard()

    parent = await anim_store.get(body.parent_id)
    if not parent:
        raise HTTPException(404, "parent animation not found")
    if not (parent.get("frame_storage_path") or parent.get("frame_local_path")):
        raise HTTPException(422, "parent has no frame to vary from")
    base_prompt = (parent.get("image_prompt_used") or "").strip()
    if not base_prompt:
        raise HTTPException(422, "parent has no stored prompt to vary from")

    # The chosen frame is the anchor for "more like this" — pull its bytes
    # (Supabase, or local fallback) into a temp file Gemini can read as a part.
    parent_bytes = await _media_bytes(
        storage_bucket=parent.get("frame_storage_bucket"),
        storage_path=parent.get("frame_storage_path"),
        local_path=parent.get("frame_local_path"),
    )
    if not parent_bytes:
        raise HTTPException(410, "parent frame image not retrievable")
    import tempfile as _tf
    _pext = _ext_for_image_bytes(parent_bytes)
    with _tf.NamedTemporaryFile(suffix=f".{_pext}", delete=False) as _ptf:
        _ptf.write(parent_bytes)
        parent_img = Path(_ptf.name)

    try:
        aspect = parent.get("aspect_ratio") or "9:16"
        # "More like this" stays in the chosen frame's style (legacy rows fall back
        # to voxel) and uses that style's variation nudges.
        poster_style = get_poster_style(parent.get("style"))
        nudges = poster_style.variation_nudges or VARIATION_NUDGES

        from ..services.openai_client import get_meter as _openai_meter
        spend_before_openai = _openai_meter().spent_usd
        spend_before_vertex = get_meter().spent_usd

        recs = await asyncio.gather(*[
            _render_animation_frame(
                build_variation_user_message(base_prompt, nudges[i % len(nudges)]),
                aspect=aspect,
                poster_style=poster_style,
                prompt=parent.get("prompt") or "",
                variation_ref_path=parent_img,
            )
            for i in range(ANIMATION_VARIANT_COUNT)
        ])
        items = await _finalize_animation_batch(recs, spend_before_openai, spend_before_vertex)

        ready = sum(1 for r in items if r.get("status") == "frame_ready")
        log.info("[animation] exploring %d variations of #%d · %d ready · %s · style=%s",
                 ANIMATION_VARIANT_COUNT, body.parent_id, ready, aspect, poster_style.key)
        return {"items": items}
    finally:
        parent_img.unlink(missing_ok=True)


@router.post("/animation/{anim_id}/remove-logo")
async def animation_remove_logo(anim_id: int) -> dict:
    """Re-render an animation's hero FRAME with the 42 logo removed, saved as a NEW
    frame_ready animation record (kept alongside the original). Remove the logo on
    the still BEFORE animating — once it's a Veo video the logo is locked into every
    frame and can't be cheaply inpainted. Uses the shared
    ``card_animation.render_delogo_image`` (image fed back, NO logo part)."""
    await _budget_guard()

    parent = await anim_store.get(anim_id)
    if not parent:
        raise HTTPException(404, "animation not found")
    if parent.get("status") == "animating":
        raise HTTPException(409, detail={"error": "animation_in_flight"})

    frame_bytes = await _media_bytes(
        storage_bucket=parent.get("frame_storage_bucket"),
        storage_path=parent.get("frame_storage_path"),
        local_path=parent.get("frame_local_path"),
    )
    if not frame_bytes:
        raise HTTPException(410, "frame missing — regenerate it first")

    import tempfile as _tf
    _sext = _ext_for_image_bytes(frame_bytes)
    with _tf.NamedTemporaryFile(suffix=f".{_sext}", delete=False) as _stf:
        _stf.write(frame_bytes)
        src_img = Path(_stf.name)

    spend_before = get_meter().spent_usd
    try:
        try:
            image_bytes = await card_animation.render_delogo_image(
                src_img, timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
            )
        except VertexBudgetExceededError as exc:
            raise HTTPException(503, str(exc))
        except asyncio.TimeoutError:
            raise HTTPException(504, "remove-logo timed out — try again")
        except HTTPException:
            raise
        except GeminiNoImageError as exc:
            raise _http_for_no_image(exc, action="remove the logo")
        except Exception as exc:
            if _is_permission_denied(exc):
                raise HTTPException(503, "Gemini access denied — check GCP project status")
            if _is_resource_exhausted(exc):
                raise HTTPException(429, "Image generation is rate-limited right now (quota) — wait a minute and try again.")
            log.exception("[animation] remove-logo failed (anim #%d)", anim_id)
            raise HTTPException(502, f"remove-logo failed: {exc}")
        if not image_bytes:
            raise HTTPException(502, "remove-logo returned empty image")

        frame_cost = round(max(0.0, get_meter().spent_usd - spend_before), 6)
        rec = await anim_store.create(
            prompt=parent.get("prompt"),
            image_prompt_used=(parent.get("image_prompt_used") or "")[:8000],
            status="frame_ready",
            aspect_ratio=parent.get("aspect_ratio"),
            style=parent.get("style"),
            frame_cost_usd=frame_cost,
        )
        new_id = int(rec["id"])
        ext = _ext_for_image_bytes(image_bytes)
        fmime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
        up = await asset_storage.upload(
            data=image_bytes,
            path=f"animation/{new_id}-frame.{ext}",
            mime=fmime,
            bucket=config.producer_media_bucket,
        )
        rec = await anim_store.update(
            new_id,
            frame_storage_bucket=up.get("storage_bucket"),
            frame_storage_path=up.get("storage_path"),
            frame_local_path=up.get("local_path"),
        )
        log.info("[animation] remove-logo #%d → frame #%d ready", anim_id, new_id)
        return _anim_out(rec or {})
    finally:
        src_img.unlink(missing_ok=True)


@router.post("/animation/{anim_id}/refine")
async def animation_refine(anim_id: int, body: RefineRequest) -> dict:
    """Re-render an animation's hero FRAME applying ONLY a user-described change ("make
    the title bigger", "change the background to blue"), saved as a NEW frame_ready record
    (kept alongside the original). Refine the still BEFORE animating — once it's a Veo
    video the change is locked into every frame and can't be cheaply inpainted. The
    general case of /animation/{id}/remove-logo: same plumbing (frame fed back, NO logo
    part, via the shared ``card_animation.render_refine_image``) with the user's change
    carried in the final render directive."""
    await _budget_guard()

    change = (body.instructions or "").strip()[:500]
    if not change:
        raise HTTPException(422, "describe what to change")

    parent = await anim_store.get(anim_id)
    if not parent:
        raise HTTPException(404, "animation not found")
    if parent.get("status") == "animating":
        raise HTTPException(409, detail={"error": "animation_in_flight"})

    frame_bytes = await _media_bytes(
        storage_bucket=parent.get("frame_storage_bucket"),
        storage_path=parent.get("frame_storage_path"),
        local_path=parent.get("frame_local_path"),
    )
    if not frame_bytes:
        raise HTTPException(410, "frame missing — regenerate it first")

    import tempfile as _tf
    _sext = _ext_for_image_bytes(frame_bytes)
    with _tf.NamedTemporaryFile(suffix=f".{_sext}", delete=False) as _stf:
        _stf.write(frame_bytes)
        src_img = Path(_stf.name)

    spend_before = get_meter().spent_usd
    try:
        try:
            image_bytes = await card_animation.render_refine_image(
                src_img, change=change, timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
            )
        except VertexBudgetExceededError as exc:
            raise HTTPException(503, str(exc))
        except asyncio.TimeoutError:
            raise HTTPException(504, "refine timed out — try again")
        except HTTPException:
            raise
        except GeminiNoImageError as exc:
            raise _http_for_no_image(exc, action="apply that change")
        except Exception as exc:
            if _is_permission_denied(exc):
                raise HTTPException(503, "Gemini access denied — check GCP project status")
            if _is_resource_exhausted(exc):
                raise HTTPException(429, "Image generation is rate-limited right now (quota) — wait a minute and try again.")
            log.exception("[animation] refine failed (anim #%d)", anim_id)
            raise HTTPException(502, f"refine failed: {exc}")
        if not image_bytes:
            raise HTTPException(502, "refine returned empty image")

        frame_cost = round(max(0.0, get_meter().spent_usd - spend_before), 6)
        # Fold the change into image_prompt_used so a later Explore/refine of this child
        # inherits the refined intent (anim_store has no feedback_text column).
        refined_prompt = ((parent.get("image_prompt_used") or "")[:7800] + f"\n\n[refined: {change}]")
        rec = await anim_store.create(
            prompt=parent.get("prompt"),
            image_prompt_used=refined_prompt[:8000],
            status="frame_ready",
            aspect_ratio=parent.get("aspect_ratio"),
            style=parent.get("style"),
            frame_cost_usd=frame_cost,
        )
        new_id = int(rec["id"])
        ext = _ext_for_image_bytes(image_bytes)
        fmime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
        up = await asset_storage.upload(
            data=image_bytes,
            path=f"animation/{new_id}-frame.{ext}",
            mime=fmime,
            bucket=config.producer_media_bucket,
        )
        rec = await anim_store.update(
            new_id,
            frame_storage_bucket=up.get("storage_bucket"),
            frame_storage_path=up.get("storage_path"),
            frame_local_path=up.get("local_path"),
        )
        log.info("[animation] refine #%d → frame #%d ready", anim_id, new_id)
        return _anim_out(rec or {})
    finally:
        src_img.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Animate an image the user ALREADY has
#
# Everything above this line animates a frame THIS tool rendered. That left the
# expensive half of the pipeline (Veo) reachable only by first paying for the
# cheap half (Gemini) - so a poster the user already generated, an image sitting
# in their Assets library, or a file on their desktop could not be animated at
# all without regenerating something like it and hoping.
#
# The fix is an import, not a second pipeline. One endpoint copies the bytes
# into an animation record's frame slot and returns it `frame_ready`. From that
# moment the image IS an animation frame, so /animate, Explore, Refine, Remove
# logo, Save, the Library tile and the poller that finishes the MP4 all work on
# it unchanged. No model is called here and nothing is charged.
# ---------------------------------------------------------------------------

def infer_animation_aspect(width: Optional[int], height: Optional[int]) -> str:
    """Pick the Veo shape that crops the image least.

    Veo takes 9:16 or 16:9 and nothing else, so a SQUARE image (which is exactly
    what Create Poster makes) has no exact fit and something must be given up.
    Vertical is the default because that is what 42 posts, and because the crop
    a square takes on the way to 9:16 is the same crop Instagram applies to a
    Reel anyway. The caller can always override.
    """
    if width and height and width > height:
        return "16:9"
    return "9:16"


class AnimationFromImageRequest(BaseModel):
    """Exactly ONE of the three sources, plus optional overrides.

    `prompt` is brief context for the motion prompt (mood and music, never
    visual content). When absent it falls back to whatever the source knows
    about itself: a poster's topic, an asset's display name, nothing at all for
    a raw upload.
    """
    poster_gen_id: Optional[int] = None   # a Create-Poster generation
    asset_id: Optional[int] = None        # a saved producer_assets image
    upload_token: Optional[str] = None    # a file from the user's own computer
    aspect_ratio: Optional[str] = None    # "9:16" | "16:9"; inferred when absent
    prompt: Optional[str] = None
    style: Optional[str] = None


async def _import_bytes_from_poster(gen_id: int) -> tuple[bytes, str, Optional[str]]:
    """(image bytes, brief, style) for a Create-Poster generation.

    The poster's own style key is inherited, which is the one import path where
    we genuinely know the look - so a voxel poster gets the voxel motion prompt
    rather than the deliberately blind `asis` one.
    """
    gen = await ystore.get(gen_id)
    if not gen or gen.get("mode") != POSTER_MODE:
        raise HTTPException(404, "poster not found")
    if gen.get("status") not in (STATUS_READY, STATUS_SAVED):
        raise HTTPException(422, f"cannot animate - poster status is '{gen.get('status')}'")
    data = await _media_bytes(
        storage_bucket=gen.get("storage_bucket"),
        storage_path=gen.get("storage_path"),
        local_path=gen.get("local_path"),
    )
    if not data:
        raise HTTPException(410, "poster image not retrievable")
    return data, (gen.get("topic") or ""), gen.get("style")


async def _import_bytes_from_asset(asset_id: int) -> tuple[bytes, str, Optional[str]]:
    """(image bytes, brief, style) for a saved producer_assets row.

    Style is None: the library holds anything from a rendered poster to a photo,
    so the caller falls back to IMPORTED_STYLE rather than guessing.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT asset_kind, display_name, storage_bucket, storage_path, local_path
                  FROM producer_assets WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "asset not found")
    if row.get("asset_kind") != "image":
        raise HTTPException(422, "only image assets can be animated")
    data = await _media_bytes(
        storage_bucket=row.get("storage_bucket"),
        storage_path=row.get("storage_path"),
        local_path=row.get("local_path"),
    )
    if not data:
        raise HTTPException(410, "asset file not retrievable")
    return data, (row.get("display_name") or ""), None


async def _import_bytes_from_upload(token: str) -> tuple[bytes, str, Optional[str]]:
    """(image bytes, brief, style) for a file the user uploaded from their computer.

    Reads the row written by /poster-human/upload-ref, which already rejected
    non-images and undecodable files - so this is the one source that arrives
    pre-validated.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT local_path FROM yas_uploads WHERE token = %s", (token,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "upload not found")
    path = Path(row["local_path"])
    if not path.is_absolute():
        path = config.asset_root.parent / path
    if not path.exists():
        raise HTTPException(410, "that upload is gone from disk - upload it again")
    return path.read_bytes(), "", None


@router.post("/animation/from-image")
async def animation_from_image(body: AnimationFromImageRequest) -> dict:
    """Bring an image the user already has into the animation store as a frame.

    Three sources, one record shape: a poster from Create Poster, an image saved
    in the Assets library, or a file straight off the user's computer. Returns a
    normal `frame_ready` animation record; the caller then POSTs
    /animation/{id}/animate exactly as it would for a generated frame.

    Costs nothing and calls no model - it copies bytes. The Veo spend and the
    budget guard both stay where they were, on /animate.
    """
    given = [body.poster_gen_id is not None, body.asset_id is not None, bool(body.upload_token)]
    if sum(given) != 1:
        raise HTTPException(400, "pass exactly one of poster_gen_id, asset_id or upload_token")

    if body.poster_gen_id is not None:
        image_bytes, brief, source_style = await _import_bytes_from_poster(body.poster_gen_id)
        origin = f"poster:{body.poster_gen_id}"
    elif body.asset_id is not None:
        image_bytes, brief, source_style = await _import_bytes_from_asset(body.asset_id)
        origin = f"asset:{body.asset_id}"
    else:
        image_bytes, brief, source_style = await _import_bytes_from_upload(body.upload_token or "")
        origin = "upload"

    width, height = _image_dims(image_bytes)
    if not width or not height:
        raise HTTPException(400, "that file is not an image we can read - use JPEG, PNG or WebP")

    aspect = (body.aspect_ratio or "").strip() or infer_animation_aspect(width, height)
    if aspect not in ANIMATION_ASPECTS:
        raise HTTPException(400, "aspect_ratio must be '9:16' or '16:9'")

    # Unknown look -> `asis`, whose motion prompt deliberately names no aesthetic.
    style_key = get_animation_style(body.style or source_style or IMPORTED_STYLE).key
    brief = (body.prompt or brief or "").strip()[:2000]

    rec = await anim_store.create(
        prompt=brief,
        image_prompt_used=f"imported from {origin}",
        status="frame_ready",
        aspect_ratio=aspect,
        style=style_key,
        frame_cost_usd=0,
    )
    anim_id = int(rec["id"])

    ext = _ext_for_image_bytes(image_bytes)
    fmime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    up = await asset_storage.upload(
        data=image_bytes,
        path=f"animation/{anim_id}-frame.{ext}",
        mime=fmime,
        bucket=config.producer_media_bucket,
    )
    # Only the three frame_* columns, exactly as _render_animation_frame writes
    # them. `mime_type` and `bytes` are left alone ON PURPOSE: the poller claims
    # both for the finished MP4, so filling them in here would describe the
    # video with the still's numbers the moment the animation lands.
    updated = await anim_store.update(
        anim_id,
        frame_storage_bucket=up.get("storage_bucket"),
        frame_storage_path=up.get("storage_path"),
        frame_local_path=up.get("local_path"),
    )
    log.info("[animation] imported %s -> anim #%d · %dx%d · %s · %s",
             origin, anim_id, width, height, aspect, style_key)
    return _anim_out(updated or rec)


@router.post("/animation/{anim_id}/animate")
async def animation_animate(anim_id: int, body: AnimationAnimateRequest) -> dict:
    """Stage 3: send the rendered frame to Veo as image-to-video."""
    rec = await anim_store.get(anim_id)
    if not rec:
        raise HTTPException(404, "animation not found")
    if rec.get("status") == "animating":
        raise HTTPException(409, detail={"error": "animation_in_flight"})
    if rec.get("status") not in ("frame_ready", "ready", "failed"):
        raise HTTPException(422, f"cannot animate — status is '{rec.get('status')}'")

    # Pull the rendered frame (Supabase, or local fallback) into a temp file that
    # Veo can read as the start image. Cleaned up after the kickoff (finally below).
    frame_bytes = await _media_bytes(
        storage_bucket=rec.get("frame_storage_bucket"),
        storage_path=rec.get("frame_storage_path"),
        local_path=rec.get("frame_local_path"),
    )
    if not frame_bytes:
        raise HTTPException(410, "frame missing — regenerate it first")
    import tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".png", delete=False) as _ftf:
        _ftf.write(frame_bytes)
        frame_path = Path(_ftf.name)

    duration = int(body.duration_sec or 6)
    if duration not in ANIMATION_DURATIONS:
        raise HTTPException(400, "duration_sec must be 4, 6, or 8")
    with_audio = True if body.with_audio is None else bool(body.with_audio)
    aspect = rec.get("aspect_ratio") or "9:16"

    await _budget_guard()

    motion_prompt = build_motion_prompt_for_style(
        rec.get("style") or DEFAULT_STYLE, rec.get("prompt") or "", with_audio=with_audio
    )
    try:
        op = await asyncio.wait_for(
            card_animation.build_card_i2v_coro(
                motion_prompt,
                start_image=frame_path,
                aspect=aspect,
                duration=duration,
                model=ANIMATE_VEO_MODEL,
                with_audio=with_audio,
                negative_prompt=ANIM_NEGATIVE_PROMPT,
            ),
            timeout=ANIMATE_KICKOFF_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Veo submission timed out — try again")
    except Exception as exc:
        if _is_permission_denied(exc):
            raise HTTPException(503, "Veo access denied — check GCP project status")
        log.exception("[animation] Veo submit failed (anim #%d)", anim_id)
        raise HTTPException(502, f"Veo submission failed: {exc}")
    finally:
        frame_path.unlink(missing_ok=True)

    updated = await anim_store.update(
        anim_id,
        status="animating",
        duration_sec=op["duration_sec"],
        with_audio=op["with_audio"],
        aspect_ratio=op["aspect_ratio"],
        veo_model=op["model"],
        veo_operation_name=op["operation_name"],
        veo_cost_usd=op["cost_usd"],
        motion_prompt_used=motion_prompt[:8000],
        error=None,
    )
    out = _anim_out(updated or {})
    log.info("[animation] anim #%d submitted to Veo · %ds · %s · op=%s",
             anim_id, duration, aspect, op["operation_name"])
    return out


@router.get("/animation/recent")
async def animation_list_recent() -> dict:
    """Last 3 days of animations (local store) for session rehydrate."""
    rows = await anim_store.recent(
        within_hours=HISTORY_WINDOW_HOURS,
        limit=50,
        statuses=("frame_ready", "animating", "ready", "failed"),
    )
    return {"items": [_anim_out(r) for r in rows]}


@router.get("/animation/{anim_id}")
async def animation_get(anim_id: int) -> dict:
    """Current state of one animation (status flips as the poller runs)."""
    rec = await anim_store.get(anim_id)
    if not rec:
        raise HTTPException(404, "animation not found")
    return _anim_out(rec)


@router.get("/animation/{anim_id}/frame")
async def animation_get_frame(anim_id: int):
    """Serve the static first frame — redirect to a signed Supabase URL (or local fallback)."""
    rec = await anim_store.get(anim_id)
    if not rec or not (rec.get("frame_storage_path") or rec.get("frame_local_path")):
        raise HTTPException(404, "frame not found")
    resp = await _media_response(
        storage_bucket=rec.get("frame_storage_bucket"),
        storage_path=rec.get("frame_storage_path"),
        local_path=rec.get("frame_local_path"),
        mime=rec.get("mime_type") or "image/png",
    )
    if resp is None:
        raise HTTPException(410, "frame file not retrievable")
    return resp


@router.get("/animation/{anim_id}/file")
async def animation_get_file(anim_id: int, download: bool = False):
    """Serve the finished MP4 — redirect to a signed Supabase URL (or local fallback).

    `?download=1` forces a save-to-disk (Content-Disposition: attachment) instead
    of inline playback — used by the Library "Download video" button."""
    rec = await anim_store.get(anim_id)
    if not rec:
        raise HTTPException(404, "animation not found")
    if not (rec.get("video_storage_path") or rec.get("video_local_path")):
        raise HTTPException(410, "animation not ready yet")
    resp = await _media_response(
        storage_bucket=rec.get("video_storage_bucket"),
        storage_path=rec.get("video_storage_path"),
        local_path=rec.get("video_local_path"),
        mime="video/mp4",
        headers=_MEDIA_CACHE_HEADERS,
        download_name=f"animation-{anim_id}.mp4" if download else None,
    )
    if resp is None:
        raise HTTPException(410, "animation file not retrievable")
    return resp


@router.post("/animation/{anim_id}/save-to-assets")
async def animation_save(anim_id: int, body: PosterSaveRequest) -> dict:
    """Copy an animation's STATIC FRAME into the Assets library (img_poster —
    frames are branded posters, same category as Create-Poster saves).

    Saving does NOT change `status`: that column drives the animate flow
    (frame_ready → animating → ready) and a saved frame must stay animatable.
    Saved-ness lives in `saved_asset_id` instead.
    """
    rec = await anim_store.get(anim_id)
    if not rec:
        raise HTTPException(404, "animation not found")
    if rec.get("saved_asset_id"):
        raise HTTPException(409, detail={
            "error": "already_saved",
            "existing_asset_id": rec.get("saved_asset_id"),
        })
    if rec.get("status") not in ("frame_ready", "ready"):
        raise HTTPException(422, f"cannot save — status is '{rec.get('status')}'")

    image_bytes = await _media_bytes(
        storage_bucket=rec.get("frame_storage_bucket"),
        storage_path=rec.get("frame_storage_path"),
        local_path=rec.get("frame_local_path"),
    )
    if image_bytes is None:
        raise HTTPException(410, "frame bytes not retrievable")

    # rec["mime_type"] holds the VIDEO mime once animated (the poller writes
    # 'video/mp4') — derive the image mime from the frame bytes instead.
    ext = _ext_for_image_bytes(image_bytes)
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")

    topic = (rec.get("prompt") or "").strip()

    # Describe + embed (best-effort) — same recipe as poster_save.
    ai_description = ""
    try:
        sys_p, user_p = _describe_poster_prompt()
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
            tf.write(image_bytes)
            tmp_path = Path(tf.name)
        try:
            ai_description = await gemini_multimodal(
                user_p, images=[tmp_path], system=sys_p,
                model=config.vertex_gemini_fast_model,
                temperature=0.3, max_tokens=600,
            ) or ""
            ai_description = str(ai_description).strip()
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        if _is_permission_denied(exc):
            log.error("[animation] Gemini permission denied during describe")
        else:
            log.exception("[animation] describe step failed")
        ai_description = f"Yas animation frame: {topic[:200]}"

    embedding = None
    if ai_description:
        try:
            vecs = await gemini_embed([ai_description])
            if vecs and vecs[0]:
                embedding = vecs[0]
        except Exception:
            log.exception("[animation] embedding failed")

    slug = _asset_slug(body.display_name or topic, f"animation_{anim_id}")

    upload_result = await asset_storage.upload(
        data=image_bytes, path=f"instagram/img_poster/{slug}.{ext}", mime=mime,
    )
    attributes = await _thumb_attributes(image_bytes, upload_result, mime)
    width, height = _image_dims(image_bytes)
    display_name = (body.display_name or topic[:80] or f"Animation frame #{anim_id}")
    metadata = {
        "content_category": "img_poster",
        "source": "yas_producer_animation",
        "yas_animation_id": anim_id,
        "topic": topic,
        "prompt_used": (rec.get("image_prompt_used") or "")[:2000],
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name, ai_description,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes, width, height, embedding, metadata, attributes, enabled)
                VALUES (%s, 'image', 'free', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id, slug, display_name, storage_bucket, storage_path,
                          local_path, mime_type, bytes, width, height,
                          ai_description, metadata, created_at,
                          (embedding IS NOT NULL) AS embedded
                """,
                (
                    slug, display_name, ai_description or None,
                    upload_result.get("storage_bucket"), upload_result.get("storage_path"),
                    upload_result.get("local_path"),
                    mime, len(image_bytes), width, height,
                    embedding, Jsonb(metadata), Jsonb(attributes),
                ),
            )
            asset_row = await cur.fetchone()

    # Track saved-ness WITHOUT touching status (the frame stays animatable).
    await anim_store.update(anim_id, saved_asset_id=int(asset_row["id"]))

    out = _row(asset_row) or {}
    if asset_row.get("storage_bucket") and asset_row.get("storage_path"):
        try:
            out["signed_url"] = await asset_storage.url_for(
                storage_bucket=asset_row["storage_bucket"],
                storage_path=asset_row["storage_path"],
                local_path=asset_row.get("local_path"),
            )
        except Exception:
            out["signed_url"] = None
    return out


@router.post("/animation/{anim_id}/open-in-editor")
async def animation_open_in_editor(anim_id: int) -> dict:
    """Seed a NEW Video Editor project with this finished animation MP4 as the
    first clip; return its project id. The Library "Export to video editor"
    button deep-links into it (#producer/video_editor:<project_id>).

    The standalone animation (yas_animation_generations) can't be referenced as a
    VE kind='animation' source — that kind resolves yas_animations, a different id
    space — so we materialise the MP4 as a VE upload first, then seed the project."""
    from ..services import approved_media

    rec = await anim_store.get(anim_id)
    if not rec:
        raise HTTPException(404, "animation not found")
    if rec.get("status") != STATUS_READY or not (
        rec.get("video_storage_path") or rec.get("video_local_path")
    ):
        raise HTTPException(409, "animation video is not ready yet")

    try:
        data, _mime = await approved_media._resolve_media("animation", anim_id)
    except FileNotFoundError:
        raise HTTPException(410, "animation file not retrievable")
    except asset_storage.AssetStorageError as e:
        # The store is down or not answering; the file itself may be fine.
        raise HTTPException(503, f"media storage unavailable, try again shortly: {e}")

    upload = await ve_service.import_upload(
        raw=data, filename=f"animation-{anim_id}.mp4", mime="video/mp4",
    )
    durf = int(upload.get("durationInFrames") or 0) or ve_service._frames(rec.get("duration_sec"))
    project = await ve_service.create_project_from_sources(
        title=(rec.get("prompt") or "Animation")[:80],
        aspect=(rec.get("aspect_ratio") or "9:16"),
        sources=[{
            "kind": "upload",
            "id": int(upload["id"]),
            "duration_frames": durf,
            "title": f"Animation #{anim_id}",
        }],
    )
    return {"project_id": int(project["id"])}


# ===========================================================================
# GENERAL AI VIDEO (Ideation → General video → General AI Video).
#
# A clean, UNBRANDED generator: reference pictures + prompt → still →
# image-to-video. Mirrors the Create-Animation two-step but with NO 42 logo and
# user reference images feeding the still. The video step runs on one of two
# engines, picked per clip: Veo (Vertex, the default) or Seedance 2.5 (via
# Higgsfield, longer clips and more shapes). Records live in their own store
# (yas_general_video_generations); the MP4 is finished by the
# yas_general_video_poller. Finalise stitches selected clips into one final video
# by seeding a Video Editor project (tagged preset "general_video") and exporting
# it via Remotion; "open in editor" seeds the same project without exporting.
# ===========================================================================

GENERAL_VIDEO_ENGINES = ("veo", "seedance")
# Veo renders only these two shapes and these three lengths.
GENERAL_VIDEO_ASPECTS = {"9:16", "16:9"}
GENERAL_VIDEO_DURATIONS = {4, 6, 8}
# Seedance follows the still's shape (image-to-video takes no aspect of its
# own), so the still may be drawn in any shape Seedance accepts.
GENERAL_VIDEO_SEEDANCE_ASPECTS = set(higgsfield.ASPECTS)
GENERAL_VIDEO_STILL_ASPECTS = GENERAL_VIDEO_ASPECTS | GENERAL_VIDEO_SEEDANCE_ASPECTS
GENERAL_VIDEO_SEEDANCE_DEFAULT_RESOLUTION = "720p"
# The Video Editor canvas has three shapes; a stitched Seedance clip in any
# other shape goes onto the nearest one (a single clip is never re-rendered).
_GV_EDITOR_ASPECT = {"4:3": "16:9", "21:9": "16:9", "3:4": "9:16"}
# Gemini 3 Pro Image accepts up to 14 reference images; cap the combined
# picked-assets + uploads set here for render quality + a sane UI.
GENERAL_VIDEO_MAX_REFS = 6

# Unbranded, photoreal still — explicitly NO logo / text (the whole point of the
# "general" generator vs the branded Animation tool).
GENERAL_VIDEO_STILL_SYSTEM = (
    "You are a world-class cinematographer and still-frame artist. Render a "
    "single, photorealistic, high-quality still image that will serve as the "
    "FIRST FRAME of a short video. Compose it cleanly for the requested aspect "
    "ratio. When reference image(s) are attached, use them to guide the subject, "
    "character likeness, style and setting. Do NOT add any logo, watermark, "
    "caption, subtitle or text overlay. No borders, no split screens, no collage. "
    "Natural lighting, realistic detail, cinematic composition. "
    # CONDITIONAL face-realism clause — only bites when a human is actually in
    # frame, so backgrounds / objects / vehicles / landscapes stay unconstrained.
    "If a human face or person appears, render the face photo-realistically: real "
    "skin with visible pores and natural texture, natural facial asymmetry, "
    "catchlights in the eyes, and the face in sharp focus — avoid the tell-tale AI "
    "look (plastic or waxy over-smoothed skin, over-symmetry, distorted, warped or "
    "melted features, extra fingers). For non-human subjects (a background, object, "
    "vehicle, landscape, etc.) render with the same high fidelity appropriate to "
    "that subject. Match exactly what the prompt asks for — never add people who "
    "weren't requested."
)


def _gv_motion_prompt(prompt: str, *, with_audio: bool) -> str:
    """The image-to-video motion clause for a general clip (no branding)."""
    base = (
        "Animate this image into a natural, cinematic short video. Bring the scene "
        "to life with subtle, realistic motion and gentle camera movement while "
        "keeping the composition, subject and style consistent with the first frame."
    )
    if prompt.strip():
        base += f" Scene intent: {prompt.strip()[:600]}."
    base += "" if with_audio else " No audio, silent clip."
    return base


def _gv_aspect_note(aspect: str) -> str:
    """The render directive for the still. Veo's two shapes reuse the Animation
    tool's exact wording; the extra Seedance shapes get the same sentence."""
    if aspect in GENERAL_VIDEO_ASPECTS:
        return card_animation.aspect_render_note(aspect)
    kind = {
        "1:1": "SQUARE",
        "4:3": "4:3 HORIZONTAL (classic landscape)",
        "3:4": "3:4 VERTICAL (portrait)",
        "21:9": "21:9 ULTRA-WIDE (cinematic)",
    }.get(aspect, aspect)
    return f"RENDER A {kind} IMAGE."


def _gv_out(rec: dict) -> dict:
    """Serialise a general-video record for the frontend, adding media URLs."""
    out = dict(rec or {})
    gid = out.get("id")
    if gid is not None and (out.get("frame_storage_path") or out.get("frame_local_path")):
        out["frame_url"] = f"/api/yas-producer/general-video/{gid}/frame"
    if gid is not None and out.get("status") == STATUS_READY:
        out["file_url"] = f"/api/yas-producer/general-video/{gid}/file"
    return out


class GeneralVideoStillRequest(BaseModel):
    prompt: str
    aspect_ratio: str = "9:16"  # Veo: "9:16" | "16:9"; Seedance adds 1:1, 4:3, 3:4, 21:9
    reference_upload_ids: list[int] = []
    reference_asset_ids: list[int] = []  # producer_assets picked from the library


class GeneralVideoAnimateRequest(BaseModel):
    # The status the page showed for this still. When given, the still is
    # claimed only from that status, so a tab that has not seen a newer state
    # (a finished clip, an unconfirmed submit) cannot start another job on it.
    expected_status: Optional[str] = None
    engine: Optional[str] = None        # "veo" (default) | "seedance"
    duration_sec: Optional[int] = None  # Veo 4 | 6 | 8; Seedance 4-30 — default 6
    with_audio: Optional[bool] = None   # default True
    quality: Optional[str] = None       # Veo only: "normal" (fast) | "high" (premium)
    resolution: Optional[str] = None    # Seedance only: "480p" | "720p" | "1080p"


class GeneralVideoBuildRequest(BaseModel):
    clip_ids: list[int]
    aspect: Optional[str] = None
    title: Optional[str] = None


@router.post("/general-video/generate-still")
async def general_video_generate_still(body: GeneralVideoStillRequest) -> dict:
    """Stage 1: render an unbranded still from the prompt + reference pictures."""
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    aspect = (body.aspect_ratio or "9:16").strip()
    if aspect not in GENERAL_VIDEO_STILL_ASPECTS:
        raise HTTPException(
            400, f"aspect_ratio must be one of {', '.join(sorted(GENERAL_VIDEO_STILL_ASPECTS))}",
        )
    # References can be uploaded files AND/OR picked Assets. The still is rendered
    # by Gemini 3 Pro Image (Nano Banana Pro), which accepts up to 14 reference
    # images; we cap the combined set at GENERAL_VIDEO_MAX_REFS for quality + UX.
    upload_ids = [int(x) for x in (body.reference_upload_ids or [])]
    asset_ids = [int(x) for x in (body.reference_asset_ids or [])]
    asset_paths = await _resolve_asset_paths(asset_ids) if asset_ids else []
    upload_paths = await _resolve_upload_paths(upload_ids) if upload_ids else []
    ref_paths = [*asset_paths, *upload_paths][:GENERAL_VIDEO_MAX_REFS]

    await _budget_guard()
    spend_before_vertex = get_meter().spent_usd

    aspect_note = _gv_aspect_note(aspect)
    # Reference images first (as image parts), then the text directive. No logo.
    parts: list[tuple[str, Any]] = [("image", p) for p in ref_paths] + [
        ("text", f"{aspect_note}\n\n{prompt}"),
    ]
    try:
        image_bytes = await card_animation.render_card_image(
            parts,
            system_instruction=GENERAL_VIDEO_STILL_SYSTEM,
            timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
            aspect_ratio=aspect,  # enforce the shape at the Gemini API level
        )
    except VertexBudgetExceededError as exc:
        raise HTTPException(503, str(exc))
    except asyncio.TimeoutError:
        raise HTTPException(504, "still generation timed out — try again")
    except Exception as exc:
        if _is_permission_denied(exc):
            raise HTTPException(503, "Gemini access denied — check GCP project status")
        log.exception("[general-video] still render failed")
        raise HTTPException(502, f"Still render failed: {exc}")
    if not image_bytes:
        raise HTTPException(502, "still generation returned empty bytes")

    frame_cost = max(0.0, get_meter().spent_usd - spend_before_vertex)

    rec = await gv_store.create(
        prompt=prompt,
        image_prompt_used=prompt[:8000],
        status="frame_ready",
        aspect_ratio=aspect,
        reference_upload_ids=(",".join(str(i) for i in upload_ids) or None),
        frame_cost_usd=round(frame_cost, 6),
    )
    gid = int(rec["id"])
    ext = _ext_for_image_bytes(image_bytes)
    fmime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    up = await asset_storage.upload(
        data=image_bytes,
        path=f"general-video/{gid}-frame.{ext}",
        mime=fmime,
        bucket=config.producer_media_bucket,
    )
    rec = await gv_store.update(
        gid,
        frame_storage_bucket=up.get("storage_bucket"),
        frame_storage_path=up.get("storage_path"),
        frame_local_path=up.get("local_path"),
    )
    log.info("[general-video] still #%d ready · %s · $%.4f", gid, aspect, frame_cost)
    return _gv_out(rec or {})


@router.get("/general-video/engines")
async def general_video_engines() -> dict:
    """What each video engine accepts, so the form offers only valid choices.
    Seedance prices are the published list price per second of output."""
    return {
        "default": "veo",
        "veo": {
            "connected": True,
            "aspects": sorted(GENERAL_VIDEO_ASPECTS, key=["9:16", "16:9"].index),
            "durations": sorted(GENERAL_VIDEO_DURATIONS),
        },
        "seedance": {
            "connected": higgsfield.connected(),
            "not_connected_message": None if higgsfield.connected() else higgsfield.NOT_CONNECTED_MESSAGE,
            "aspects": list(higgsfield.ASPECTS),
            "duration_min": higgsfield.DURATION_MIN,
            "duration_max": higgsfield.DURATION_MAX,
            "resolutions": list(higgsfield.RESOLUTIONS),
            "default_resolution": GENERAL_VIDEO_SEEDANCE_DEFAULT_RESOLUTION,
            "price_per_sec_usd": dict(higgsfield.PRICE_PER_SEC_USD),
            "budget_usd": config.seedance_budget_usd or None,
        },
    }


# Seedance submits: three attempts with one Idempotency-Key when the answer is
# lost, waiting this long times the attempt number in between (module level so
# tests can zero it). Higgsfield's docs: after a timeout, a network failure or a
# 5xx the job may exist, so repeat the identical request with the same key.
_GV_SEEDANCE_SUBMIT_ATTEMPTS = 3
_GV_SEEDANCE_RETRY_BACKOFF_SEC = 2.0
# invalid_response is a 2xx without a request id: accepted, so also unknown.
_GV_HF_OUTCOME_UNKNOWN = frozenset({"timeout", "network", "upstream", "invalid_response"})
_GV_HF_RETRY = _GV_HF_OUTCOME_UNKNOWN | {"rate_limited"}
_GV_SEEDANCE_UNCONFIRMED = (
    "Higgsfield did not confirm this request, so it may already be rendering and billing. "
    "Check the Higgsfield dashboard before trying again."
)
# Writing an accepted job onto its row: attempts, and the pause between them
# (module level so tests can zero it).
_GV_RECORD_ATTEMPTS = 3
_GV_RECORD_RETRY_SEC = 1.0
_GV_CLAIMABLE = ("frame_ready", "ready", "failed")
_GV_HF_STATUS = {
    "not_connected": 503, "auth": 503, "credits": 402, "blocked": 503,
    "concurrency": 429, "rate_limited": 429, "invalid": 400, "timeout": 504,
}
_GV_IN_FLIGHT = "This still is already being made into a video."
_GV_CHANGED = "This still has changed since the page loaded it. Look at its card again before generating."


class _GvRecordedFailure(Exception):
    """An animate attempt failed and the row already says so, so the claim on
    the still is kept as it is instead of being rolled back."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _gv_unclaim(gen_id: int, rec: dict) -> None:
    """Put a claimed still back the way it was: the engine never took the job."""
    try:
        await gv_store.update(
            gen_id,
            status=rec.get("status"),
            engine=rec.get("engine"),
            error=rec.get("error"),
            veo_operation_name=rec.get("veo_operation_name"),
            seedance_request_id=rec.get("seedance_request_id"),
            seedance_status_url=rec.get("seedance_status_url"),
        )
    except Exception:  # the stale-claim sweep fails it later
        log.exception("[general-video] #%d could not be released after a failed attempt", gen_id)


async def _gv_record_job(gen_id: int, fields: dict, job_ref: str) -> dict:
    """Write an accepted job onto its row. The engine is already working, and
    billing, so a failed write must not reopen the still for a second job: try
    a few times with a pause, then say so and leave the claim to the stale-claim
    sweep."""
    for attempt in range(1, _GV_RECORD_ATTEMPTS + 1):
        try:
            return await gv_store.update(gen_id, **fields) or {}
        except Exception:
            log.exception("[general-video] #%d: saving %s failed (attempt %d)", gen_id, job_ref, attempt)
            if attempt < _GV_RECORD_ATTEMPTS:
                await asyncio.sleep(_GV_RECORD_RETRY_SEC * attempt)
    raise _GvRecordedFailure(
        500,
        f"The video was started ({job_ref}) but could not be saved here. "
        "Check the provider's dashboard before trying again.",
    )


@router.post("/general-video/{gen_id}/animate")
async def general_video_animate(gen_id: int, body: GeneralVideoAnimateRequest) -> dict:
    """Stage 2: send the rendered still to the chosen engine as image-to-video.

    Every choice is validated first. Then the still is claimed (status
    'animating', one conditional UPDATE) before anything is paid, so a second
    click, a second tab or the page's refresh during a slow submit gets a 409
    instead of starting a second job. If the attempt fails before the engine
    accepts the job, the still is put back as it was."""
    claimable = _GV_CLAIMABLE
    if body.expected_status:
        if body.expected_status not in _GV_CLAIMABLE:
            raise HTTPException(400, f"expected_status must be one of {', '.join(_GV_CLAIMABLE)}")
        claimable = (body.expected_status,)
    rec = await gv_store.get(gen_id)
    if not rec:
        raise HTTPException(404, "clip not found")
    if rec.get("status") == "animating":
        raise HTTPException(409, _GV_IN_FLIGHT)
    if rec.get("status") not in claimable:
        if body.expected_status:
            raise HTTPException(409, _GV_CHANGED)
        raise HTTPException(422, f"cannot animate — status is '{rec.get('status')}'")
    engine = (body.engine or "veo").strip().lower()
    if engine not in GENERAL_VIDEO_ENGINES:
        raise HTTPException(400, "engine must be 'veo' or 'seedance'")
    duration = int(body.duration_sec or 6)
    with_audio = True if body.with_audio is None else bool(body.with_audio)
    resolution = (body.resolution or GENERAL_VIDEO_SEEDANCE_DEFAULT_RESOLUTION).strip().lower()
    if engine == "seedance":
        if not higgsfield.connected():
            raise HTTPException(503, higgsfield.NOT_CONNECTED_MESSAGE)
        if not (higgsfield.DURATION_MIN <= duration <= higgsfield.DURATION_MAX):
            raise HTTPException(
                400, f"duration_sec must be {higgsfield.DURATION_MIN} to {higgsfield.DURATION_MAX} for Seedance",
            )
        if resolution not in higgsfield.RESOLUTIONS:
            raise HTTPException(400, f"resolution must be one of {', '.join(higgsfield.RESOLUTIONS)}")
    else:
        aspect = rec.get("aspect_ratio") or "9:16"
        if aspect not in GENERAL_VIDEO_ASPECTS:
            raise HTTPException(
                422, f"Veo makes only 9:16 and 16:9 videos; this still is {aspect}. Use Seedance for it.",
            )
        if duration not in GENERAL_VIDEO_DURATIONS:
            raise HTTPException(400, "duration_sec must be 4, 6, or 8")

    if await gv_store.claim_for_animation(gen_id, engine, from_statuses=claimable) is None:
        raise HTTPException(409, _GV_IN_FLIGHT)
    try:
        frame_bytes = await _media_bytes(
            storage_bucket=rec.get("frame_storage_bucket"),
            storage_path=rec.get("frame_storage_path"),
            local_path=rec.get("frame_local_path"),
        )
        if not frame_bytes:
            raise HTTPException(410, "still missing — regenerate it first")
        if engine == "seedance":
            return await _gv_animate_seedance(
                gen_id, rec, frame_bytes,
                duration=duration, resolution=resolution, with_audio=with_audio,
            )
        return await _gv_animate_veo(
            gen_id, rec, frame_bytes,
            duration=duration, with_audio=with_audio, quality=body.quality,
        )
    except _GvRecordedFailure as exc:
        raise HTTPException(exc.status, exc.message)
    except Exception:
        await _gv_unclaim(gen_id, rec)
        raise


async def _gv_animate_veo(
    gen_id: int, rec: dict, frame_bytes: bytes, *,
    duration: int, with_audio: bool, quality: Optional[str],
) -> dict:
    """Stage 2 on Veo: send the still to Veo as image-to-video."""
    aspect = rec.get("aspect_ratio") or "9:16"
    # Normal = Veo 3.1 Fast; High = Veo 3.1 Pro (premium, ~5× cost). Same model
    # pair the Talking Head quality toggle uses (config.veo_model[_premium]).
    veo_model = config.veo_model_premium if (quality or "").lower() == "high" else config.veo_model

    await _budget_guard()
    motion_prompt = _gv_motion_prompt(rec.get("prompt") or "", with_audio=with_audio)
    import tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".png", delete=False) as _ftf:
        _ftf.write(frame_bytes)
        frame_path = Path(_ftf.name)
    try:
        op = await asyncio.wait_for(
            card_animation.build_card_i2v_coro(
                motion_prompt,
                start_image=frame_path,
                aspect=aspect,
                duration=duration,
                model=veo_model,
                with_audio=with_audio,
            ),
            timeout=ANIMATE_KICKOFF_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Veo submission timed out — try again")
    except Exception as exc:
        if _is_permission_denied(exc):
            raise HTTPException(503, "Veo access denied — check GCP project status")
        log.exception("[general-video] Veo submit failed (#%d)", gen_id)
        raise HTTPException(502, f"Veo submission failed: {exc}")
    finally:
        frame_path.unlink(missing_ok=True)

    updated = await _gv_record_job(gen_id, {
        "status": "animating",
        "engine": "veo",
        "duration_sec": op["duration_sec"],
        "with_audio": op["with_audio"],
        "aspect_ratio": op["aspect_ratio"],
        "resolution": None,
        "veo_model": op["model"],
        "veo_operation_name": op["operation_name"],
        "veo_cost_usd": op["cost_usd"],
        "motion_prompt_used": motion_prompt[:8000],
        "error": None,
    }, f"Veo operation {op['operation_name']}")
    log.info("[general-video] #%d submitted to Veo · %ds · %s · op=%s",
             gen_id, duration, aspect, op["operation_name"])
    return _gv_out(updated)


async def _gv_write_off(spend_id: int) -> None:
    try:
        await gv_store.write_off_seedance_spend(spend_id=spend_id)
    except Exception:  # it stays counted: the cap errs on the safe side
        log.exception("[general-video] could not write off Seedance spend #%d", spend_id)


async def _gv_submit_seedance(gen_id: int, payload: dict) -> dict:
    """Submit with one Idempotency-Key, repeating the identical request after a
    lost answer: a repeat returns the original job instead of a second one.

    Once any attempt has an unknown outcome, the submit stays unknown whatever
    the later attempts answer: Higgsfield does not consume a key on a request it
    rejects, so a refused repeat (a 429, say) says nothing about the first one."""
    key = secrets.token_hex(16)
    unknown: Optional[higgsfield.HiggsfieldError] = None
    for attempt in range(1, _GV_SEEDANCE_SUBMIT_ATTEMPTS + 1):
        try:
            return await higgsfield.submit(higgsfield.ROUTE_IMAGE, payload, idempotency_key=key)
        except higgsfield.HiggsfieldError as exc:
            if exc.code in _GV_HF_OUTCOME_UNKNOWN and unknown is None:
                unknown = exc
            if exc.code in _GV_HF_RETRY and attempt < _GV_SEEDANCE_SUBMIT_ATTEMPTS:
                log.warning("[general-video] #%d Seedance submit attempt %d: %s (%s); repeating with the same key",
                            gen_id, attempt, exc.message, exc.code)
                await asyncio.sleep(_GV_SEEDANCE_RETRY_BACKOFF_SEC * attempt)
                continue
            if unknown is not None and exc is not unknown:
                raise unknown from exc
            raise
    raise AssertionError("unreachable")


async def _gv_animate_seedance(
    gen_id: int, rec: dict, frame_bytes: bytes, *,
    duration: int, resolution: str, with_audio: bool,
) -> dict:
    """Stage 2 on Seedance: upload the still to Higgsfield and send one
    image-to-video request. The clip takes the still's shape. The poller
    downloads the MP4 when Higgsfield reports it done.

    The clip's cost is reserved in the spend ledger before anything is paid
    (the cap check and the reservation share one lock), written off when
    Higgsfield refuses the submit, and kept when the outcome is unknown."""
    from datetime import datetime, timezone

    motion_prompt = _gv_motion_prompt(rec.get("prompt") or "", with_audio=with_audio)
    cost = higgsfield.estimate_local_usd(duration, resolution)
    cost_usd = round(cost["usd_promo"] if cost["usd_promo"] is not None else cost["usd_list"], 4)
    cap = float(config.seedance_budget_usd or 0)
    spend_id, spent = await gv_store.reserve_seedance_spend(gen_id, cost_usd, cap_usd=cap)
    if spend_id is None:
        raise HTTPException(
            402,
            f"This clip (about ${cost_usd:.2f}) would take Seedance past its "
            f"${cap:.0f} budget for the last 30 days (${spent:.2f} spent). "
            "Raise SEEDANCE_BUDGET_USD or use Veo.",
        )

    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(
        _ext_for_image_bytes(frame_bytes), "image/png",
    )
    try:
        image_url = await higgsfield.upload_file(frame_bytes, mime)
        payload = higgsfield.build_payload(
            higgsfield.ROUTE_IMAGE,
            prompt=motion_prompt,
            image_url=image_url,
            duration=duration,
            resolution=resolution,
            generate_audio=with_audio,
        )
    except ValueError as exc:
        await _gv_write_off(spend_id)
        raise HTTPException(400, str(exc))
    except higgsfield.HiggsfieldError as exc:  # nothing was submitted yet
        await _gv_write_off(spend_id)
        raise HTTPException(_GV_HF_STATUS.get(exc.code, 502), exc.message)

    submitted_at = datetime.now(timezone.utc)
    try:
        job = await _gv_submit_seedance(gen_id, payload)
    except higgsfield.HiggsfieldError as exc:
        log.warning("[general-video] Seedance submit failed (#%d): %s code=%s cid=%s",
                    gen_id, exc.message, exc.code, exc.correlation_id)
        if exc.code not in _GV_HF_OUTCOME_UNKNOWN:
            await _gv_write_off(spend_id)
            raise HTTPException(_GV_HF_STATUS.get(exc.code, 502), exc.message)
        # Higgsfield may have accepted it. Keep the spend counted and put the
        # reason on the card rather than reopening the still for a blind resubmit.
        msg = _GV_SEEDANCE_UNCONFIRMED
        await _gv_record_job(gen_id, {
            "status": "failed",
            "engine": "seedance",
            "duration_sec": duration,
            "with_audio": with_audio,
            "resolution": resolution,
            "seedance_cost_usd": cost_usd,
            "seedance_submitted_at": submitted_at,
            "motion_prompt_used": motion_prompt[:8000],
            "error": msg,
        }, "a Seedance request with no answer")
        raise _GvRecordedFailure(_GV_HF_STATUS.get(exc.code, 502), msg)

    try:
        await gv_store.attach_seedance_request(spend_id, job["request_id"])
    except Exception:  # its cost stays counted even if the job later fails
        log.exception("[general-video] #%d: could not tie spend #%d to Seedance request %s",
                      gen_id, spend_id, job["request_id"])
    updated = await _gv_record_job(gen_id, {
        "status": "animating",
        "engine": "seedance",
        "duration_sec": duration,
        "with_audio": with_audio,
        "resolution": resolution,
        "seedance_request_id": job["request_id"],
        "seedance_status_url": job["status_url"],
        "seedance_cost_usd": cost_usd,
        "seedance_submitted_at": submitted_at,
        "veo_model": None,
        "veo_cost_usd": None,
        "motion_prompt_used": motion_prompt[:8000],
        "error": None,
    }, f"Seedance request {job['request_id']}")
    log.info("[general-video] #%d submitted to Seedance · %ds · %s · %s · req=%s",
             gen_id, duration, rec.get("aspect_ratio"), resolution, job["request_id"])
    return _gv_out(updated)


@router.get("/general-video/recent")
async def general_video_recent() -> dict:
    """Last 3 days of general-video clips (this session's history strip)."""
    rows = await gv_store.recent(
        within_hours=HISTORY_WINDOW_HOURS,
        limit=50,
        statuses=("frame_ready", "animating", "ready", "failed"),
    )
    return {"items": [_gv_out(r) for r in rows]}


@router.get("/general-video/{gen_id}")
async def general_video_get(gen_id: int) -> dict:
    """Current state of one general-video clip (status flips as the poller runs)."""
    rec = await gv_store.get(gen_id)
    if not rec:
        raise HTTPException(404, "clip not found")
    return _gv_out(rec)


@router.get("/general-video/{gen_id}/frame")
async def general_video_get_frame(gen_id: int, download: bool = False):
    """Serve the still first frame — signed Supabase redirect (or local fallback).
    `?download=1` forces a save-to-disk (per-still Download button)."""
    rec = await gv_store.get(gen_id)
    if not rec or not (rec.get("frame_storage_path") or rec.get("frame_local_path")):
        raise HTTPException(404, "still not found")
    resp = await _media_response(
        storage_bucket=rec.get("frame_storage_bucket"),
        storage_path=rec.get("frame_storage_path"),
        local_path=rec.get("frame_local_path"),
        mime=rec.get("mime_type") or "image/png",
        download_name=f"general-video-{gen_id}-still.png" if download else None,
    )
    if resp is None:
        raise HTTPException(410, "still file not retrievable")
    return resp


@router.get("/general-video/{gen_id}/file")
async def general_video_get_file(gen_id: int, download: bool = False):
    """Serve the finished MP4. `?download=1` forces a save-to-disk (per-clip
    Download button)."""
    rec = await gv_store.get(gen_id)
    if not rec:
        raise HTTPException(404, "clip not found")
    if not (rec.get("video_storage_path") or rec.get("video_local_path")):
        raise HTTPException(410, "clip not ready yet")
    resp = await _media_response(
        storage_bucket=rec.get("video_storage_bucket"),
        storage_path=rec.get("video_storage_path"),
        local_path=rec.get("video_local_path"),
        mime="video/mp4",
        headers=_MEDIA_CACHE_HEADERS,
        download_name=f"general-video-{gen_id}.mp4" if download else None,
    )
    if resp is None:
        raise HTTPException(410, "clip file not retrievable")
    return resp


@router.post("/general-video/clear-session")
async def general_video_clear_session() -> dict:
    """Clear the General AI Video session: archive every clip from the last 24h so
    the history strip starts clean. Non-destructive (rows are flagged 'archived',
    not deleted; media stays in storage). Returns the count cleared."""
    n = await gv_store.clear_session(within_hours=24)
    log.info("[general-video] session cleared · %d clip(s) archived", n)
    return {"cleared": n}


@router.post("/general-video/{gen_id}/save-to-assets")
async def general_video_save_still(gen_id: int, body: PosterSaveRequest) -> dict:
    """Copy a general-video STILL frame into the Assets library.

    Tagged metadata.content_category='img_general_still'; the top-level `category`
    is left NULL — the same constraint-safe carve-out as human_character_save
    (category='character' would REQUIRE an archetype we don't collect). The
    general-video record has no saved_asset_id column, so re-saving simply creates
    another asset (no hard double-save gate)."""
    rec = await gv_store.get(gen_id)
    if not rec or not (rec.get("frame_storage_path") or rec.get("frame_local_path")):
        raise HTTPException(404, "still not found")

    image_bytes = await _media_bytes(
        storage_bucket=rec.get("frame_storage_bucket"),
        storage_path=rec.get("frame_storage_path"),
        local_path=rec.get("frame_local_path"),
    )
    if image_bytes is None:
        raise HTTPException(410, "still bytes not retrievable")

    ext = _ext_for_image_bytes(image_bytes)
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    prompt_txt = (rec.get("prompt") or "").strip()

    # Describe + embed (best-effort) so the still is searchable in Assets.
    ai_description = ""
    try:
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
            tf.write(image_bytes)
            tmp_path = Path(tf.name)
        try:
            ai_description = await gemini_multimodal(
                "Describe this image for a searchable media library in one or two "
                "sentences: the subject, setting, style and mood. No preamble.",
                images=[tmp_path],
                system="You are a concise visual cataloguer.",
                model=config.vertex_gemini_fast_model,
                temperature=0.3, max_tokens=400,
            ) or ""
            ai_description = str(ai_description).strip()
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        if _is_permission_denied(exc):
            log.error("[general-video] Gemini permission denied during describe")
        else:
            log.exception("[general-video] describe step failed")
        ai_description = f"AI-generated still: {prompt_txt[:200]}"

    embedding = None
    if ai_description:
        try:
            vecs = await gemini_embed([ai_description])
            if vecs and vecs[0]:
                embedding = vecs[0]
        except Exception:
            log.exception("[general-video] embedding failed")

    slug = _asset_slug(body.display_name or prompt_txt, f"general_still_{gen_id}")

    upload_result = await asset_storage.upload(
        data=image_bytes, path=f"instagram/img_general_still/{slug}.{ext}", mime=mime,
    )
    attributes = await _thumb_attributes(image_bytes, upload_result, mime)
    width, height = _image_dims(image_bytes)
    display_name = (body.display_name or prompt_txt[:80] or f"General still #{gen_id}")
    metadata = {
        "content_category": "img_general_still",
        "source": "yas_producer_general_video",
        "general_video_id": gen_id,
        "prompt_used": prompt_txt[:2000],
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name, ai_description,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes, width, height, embedding, metadata, attributes, enabled)
                VALUES (%s, 'image', NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                RETURNING id, slug, display_name, storage_bucket, storage_path,
                          local_path, mime_type, bytes, width, height,
                          ai_description, metadata, created_at,
                          (embedding IS NOT NULL) AS embedded
                """,
                (
                    slug, display_name, ai_description or None,
                    upload_result.get("storage_bucket"), upload_result.get("storage_path"),
                    upload_result.get("local_path"),
                    mime, len(image_bytes), width, height,
                    embedding, Jsonb(metadata), Jsonb(attributes),
                ),
            )
            asset_row = await cur.fetchone()

    out = _row(asset_row) or {}
    if asset_row.get("storage_bucket") and asset_row.get("storage_path"):
        try:
            out["signed_url"] = await asset_storage.url_for(
                storage_bucket=asset_row["storage_bucket"],
                storage_path=asset_row["storage_path"],
                local_path=asset_row.get("local_path"),
            )
        except Exception:
            out["signed_url"] = None
    return out


async def _gv_build_ve_project(
    clip_ids: list[int], aspect: Optional[str], title: Optional[str],
) -> dict:
    """Materialise the chosen ready clips as Video Editor uploads and seed a NEW
    editor project (tagged preset 'general_video'), laid end-to-end in order.

    General-video clips live in their own id-space (yas_general_video_generations),
    so — like the standalone Animation 'open in editor' — they can't be referenced
    as VE kind='animation'; we import each MP4 as a VE upload first."""
    ids = [int(x) for x in (clip_ids or [])]
    if not ids:
        raise HTTPException(400, "select at least one clip")
    sources: list[dict] = []
    chosen_aspect = aspect
    for cid in ids:
        rec = await gv_store.get(cid)
        if not rec or rec.get("status") != STATUS_READY:
            continue
        data = await _media_bytes(
            storage_bucket=rec.get("video_storage_bucket"),
            storage_path=rec.get("video_storage_path"),
            local_path=rec.get("video_local_path"),
        )
        if not data:
            continue
        up = await ve_service.import_upload(
            raw=data, filename=f"general-video-{cid}.mp4", mime="video/mp4",
        )
        durf = int(up.get("durationInFrames") or 0) or ve_service._frames(rec.get("duration_sec"))
        sources.append({
            "kind": "upload",
            "id": int(up["id"]),
            "duration_frames": durf,
            "title": f"Clip #{cid}",
        })
        chosen_aspect = chosen_aspect or rec.get("aspect_ratio")
    if not sources:
        raise HTTPException(409, "no ready clips to assemble — generate the videos first")
    chosen_aspect = chosen_aspect or "9:16"
    project = await ve_service.create_project_from_sources(
        title=(title or "General AI Video")[:80],
        aspect=_GV_EDITOR_ASPECT.get(chosen_aspect, chosen_aspect),
        sources=sources,
        preset="general_video",
    )
    return project


async def _gv_finalize_single_clip(project_id: int, clips: list[dict]) -> None:
    """No stitch needed for ONE clip — it already IS a finished video. Copy the
    imported clip MP4 straight to the project's composition path and mark the
    render ready, skipping Remotion entirely (fast + dodges the render poller)."""
    import shutil
    src = (clips[0].get("source") or {}) if clips else {}
    upload_id = src.get("upload_id")
    local = await ve_service.ensure_upload_local(int(upload_id)) if upload_id else None
    if not local or not Path(local).exists():
        # Couldn't reuse the imported file — fall back to a real render.
        await ve_service.export_project(project_id)
        return
    out_path = ve_service.VE_ROOT / str(project_id) / "final.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(local, out_path)
    rel = ve_service._to_rel(out_path)
    await ve_service._patch_render(project_id, {
        "composition_status": ve_service.COMP_READY,
        "composition_path": rel,
        "composition_output_path": rel,
        "composition_error": None,
        "render_job_id": None,
    })
    log.info("[general-video] single-clip finalise project=%d → composition ready (no stitch)", project_id)


@router.post("/general-video/stitch")
async def general_video_stitch(body: GeneralVideoBuildRequest) -> dict:
    """Finalise. ONE clip → the clip already IS the final video: copy it to the
    project composition and mark ready (NO Remotion stitch). TWO+ clips → seed the
    VE project and run the real Remotion stitch via the composition poller."""
    project = await _gv_build_ve_project(body.clip_ids, body.aspect, body.title)
    pid = int(project["id"])
    clips = project.get("clips") or []
    if len(clips) <= 1:
        await _gv_finalize_single_clip(pid, clips)
    else:
        await ve_service.export_project(pid)
    return {"project_id": pid}


@router.post("/general-video/open-in-editor")
async def general_video_open_in_editor(body: GeneralVideoBuildRequest) -> dict:
    """Seed a VE project from the chosen clips (no export) and return its id so the
    frontend can deep-link into the editor."""
    project = await _gv_build_ve_project(body.clip_ids, body.aspect, body.title)
    return {"project_id": int(project["id"])}


# ===========================================================================
# Global Producer Roadmap — Step 3 (Preview): brand-consistent caption via RAG.
#
# Embeds the asset's topic, retrieves the nearest high-performing @42abudhabi
# posts (their caption_embedding already exists, 768-d), and prompts gpt-4o to
# write a fresh on-brand caption. Degrades gracefully: if Vertex/DB retrieval
# fails, it still writes a caption from the topic + brand voice alone.
# ===========================================================================

class RoadmapCaptionRequest(BaseModel):
    tool: str
    ref_id: int
    topic: str


@router.post("/roadmap/caption")
async def roadmap_caption(body: RoadmapCaptionRequest) -> dict:
    topic = (body.topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required to generate a caption")

    # Retrieval + the OpenAI→Gemini fallback live in services/caption_service so
    # the campaign worker can write captions too (a worker cannot import a router).
    from ..services import caption_service

    try:
        out = await caption_service.generate_caption(topic)
    except caption_service.CaptionUnavailableError as exc:
        raise HTTPException(
            502, f"{exc}. You can still write one manually.",
        )
    log.info("[roadmap] caption for %s #%d · %d refs",
             body.tool, body.ref_id, len(out["references"]))
    return {"caption": out["caption"], "references": out["references"]}


# ---------------------------------------------------------------------------
# Step 4 (Calendar): save to the pending queue + arm the reminder email.
# Step 5 (Approval): approve + push live to Instagram (self-gated).
# ---------------------------------------------------------------------------

class RoadmapScheduleRequest(BaseModel):
    tool: str
    ref_id: int
    media_type: str = "image"
    caption: str = ""
    scheduled_at: str  # ISO 8601 with offset
    timezone: str = "Asia/Dubai"
    # True for "Post now" — the row is booked at the current instant and Approval
    # auto-publishes it immediately, so it must NOT trigger a reminder email
    # (it would race the poller and fire a bogus "posts in ~1 hour" heads-up).
    immediate: bool = False
    # When present with ≥2 entries, this books a CAROUSEL post. The full ordered
    # slide list ([{tool, ref_id, media_type}], slide 1 == tool/ref_id above).
    carousel_items: Optional[list[dict]] = None
    # Reel cover frame (milliseconds into the video). Single video posts only —
    # carousels cover from slide 1, images have no separate thumbnail.
    thumb_offset: Optional[int] = None


@router.post("/roadmap/schedule")
async def roadmap_schedule(body: RoadmapScheduleRequest) -> dict:
    from ..services import approved_media
    from ..services import scheduled_posts as sched_store
    from ..services.email_service import email_configured

    tool = (body.tool or "").strip()
    if tool not in sched_store.VALID_TOOLS:
        raise HTTPException(400, f"unknown tool '{tool}'")
    media_type = (body.media_type or "image").strip().lower()
    if media_type not in ("image", "video"):
        raise HTTPException(400, "media_type must be 'image' or 'video'")
    if not (body.scheduled_at or "").strip():
        raise HTTPException(400, "scheduled_at is required")

    # Normalise carousel slides (≥2 → a carousel post). Slide 1 is the primary
    # tool/ref_id above; the frontend sends the full ordered list here.
    slides: list[dict] = []
    for it in (body.carousel_items or []):
        t = (it.get("tool") or "").strip()
        if t not in sched_store.VALID_TOOLS:
            raise HTTPException(400, f"unknown tool '{t}' in carousel_items")
        slides.append({
            "tool": t,
            "ref_id": int(it.get("ref_id")),
            "media_type": (it.get("media_type") or "image").strip().lower(),
        })
    is_carousel = len(slides) >= 2

    # Host the media on the public "approved" bucket NOW (booking time) so
    # Instagram can fetch it at approval. Best-effort: if media storage isn't
    # set up the post still queues, and approval just reports why.
    media_url: str | None = None
    upload_warning: str | None = None
    metadata: dict | None = None
    try:
        if is_carousel:
            hosted = await approved_media.upload_carousel(slides)
            media_url = hosted[0]["media_url"]  # thumbnail = first slide
            metadata = {"is_carousel": True, "carousel_items": hosted}
        else:
            media_url = await approved_media.upload_for_post(
                tool=tool, ref_id=int(body.ref_id), media_type=media_type
            )
    except Exception as exc:
        upload_warning = str(exc)
        log.warning("[roadmap] media hosting failed for %s #%s: %s", tool, body.ref_id, exc)
        if is_carousel:
            # Keep the (unhosted) slides so approval can retry hosting.
            metadata = {"is_carousel": True, "carousel_items": slides}

    # Reel cover frame: persist for single video posts so approve() passes it to
    # the Graph API. Carousels cover from slide 1; images have no thumbnail.
    if not is_carousel and media_type == "video" and body.thumb_offset is not None:
        metadata = {**(metadata or {}), "thumb_offset": int(body.thumb_offset)}

    # "Post now": tag the row so the reminder poller skips it — Approval publishes
    # it right away, so a reminder email would be both wrong ("~1 hour") and racy.
    if body.immediate:
        metadata = {**(metadata or {}), "suppress_reminders": True}

    try:
        row = await sched_store.create(
            tool=tool,
            ref_id=int(body.ref_id),
            media_type=media_type,
            caption=body.caption or "",
            scheduled_at=body.scheduled_at,
            timezone=(body.timezone or "Asia/Dubai").strip(),
            media_url=media_url,
            metadata=metadata,
        )
    except Exception as exc:
        log.exception("[roadmap] schedule insert failed")
        raise HTTPException(502, f"could not schedule: {exc}")

    sa = row.get("scheduled_at")
    log.info("[roadmap] scheduled post #%s (%s #%s) for %s · hosted=%s",
             row.get("id"), tool, body.ref_id, sa, bool(media_url))
    return {
        "id": int(row["id"]),
        "scheduled_at": sa.isoformat() if hasattr(sa, "isoformat") else str(sa),
        "email_configured": email_configured(),
        "media_hosted": bool(media_url),
        "media_url": media_url,
        "upload_warning": upload_warning,
    }


@router.post("/roadmap/{post_id}/approve")
async def roadmap_approve(post_id: int) -> dict:
    from ..services import approved_media
    from ..services import scheduled_posts as sched_store
    from ..services import instagram_publish as ig_publish

    row = await sched_store.get(post_id)
    if not row:
        raise HTTPException(404, "scheduled post not found")
    if row.get("status") == "published":
        return {"status": "published", "permalink": row.get("permalink"), "detail": "already published"}

    md = row.get("metadata") or {}
    if md.get("is_carousel"):
        items = md.get("carousel_items") or []
        # Re-host any slide missing a public URL (e.g. creds added after booking).
        if not items or any(not it.get("media_url") for it in items):
            try:
                items = await approved_media.upload_carousel([
                    {"tool": it["tool"], "ref_id": it["ref_id"],
                     "media_type": it.get("media_type") or "image"}
                    for it in items
                ])
                await sched_store._update(
                    post_id, media_url=items[0]["media_url"],
                    metadata={"is_carousel": True, "carousel_items": items},
                )
            except Exception as exc:
                return {"status": "skipped", "permalink": None,
                        "detail": f"Carousel media isn't hosted yet: {exc}"}
        result = await ig_publish.publish_carousel(
            items=items, caption=row.get("caption") or "",
        )
    else:
        # The media is normally hosted at booking time. If it wasn't (e.g. media
        # storage was set up after scheduling), host it now and persist the URL.
        media_url = row.get("media_url")
        if not media_url:
            try:
                media_url = await approved_media.upload_for_post(
                    tool=row["tool"], ref_id=int(row["ref_id"]),
                    media_type=row.get("media_type") or "image",
                )
                await sched_store.set_media_url(post_id, media_url)
            except Exception as exc:
                return {
                    "status": "skipped",
                    "permalink": None,
                    "detail": f"Media isn't hosted yet. Couldn't upload it to public media storage: {exc}",
                }

        result = await ig_publish.publish(
            media_url=media_url,
            caption=row.get("caption") or "",
            media_type=row.get("media_type") or "image",
            thumb_offset=md.get("thumb_offset"),
        )

    if result["status"] == "published":
        await sched_store.mark_published(
            post_id, ig_media_id=result.get("ig_media_id"), permalink=result.get("permalink")
        )
        log.info("[roadmap] published post #%s → %s", post_id, result.get("permalink"))
        return {"status": "published", "permalink": result.get("permalink"), "detail": None}

    if result["status"] == "failed":
        await sched_store.mark_failed(post_id, result.get("detail") or "publish failed")

    # 'skipped' (gated) or 'failed' → tell the UI why; nothing was posted.
    return {"status": "skipped", "permalink": None, "detail": result.get("detail")}


# ---------------------------------------------------------------------------
# Scheduled queue — list the pending posts (Library → Scheduled tab) + cancel.
# Lets the user get back to anything they scheduled for later (the roadmap flow
# itself is one-shot / in-memory).
# ---------------------------------------------------------------------------

# Same-origin file proxy per tool: the thumbnail fallback when a row has no
# hosted media_url yet (e.g. scheduled before media storage was set up), or
# when cancelling it deleted the hosted copy.
_ROADMAP_FILE_PATH = {
    "poster": "/api/yas-producer/poster/{id}/file",
    "animation": "/api/yas-producer/animation/{id}/file",
    "poster_human": "/api/yas-producer/poster-human/{id}/file",
    "talking_head_v2": "/api/yas-producer/talking-head-v2/{id}/composition/file",
    "video_editor": "/api/yas-producer/video-editor/{id}/composition/file",
    # A B-roll is a Video-Editor project, booked under its own tool name.
    "broll": "/api/yas-producer/video-editor/{id}/composition/file",
    "general_video": "/api/yas-producer/general-video/{id}/file",
    # ref_id is the storyboard id (see approved_media._resolve_media).
    "seedance_video": "/api/yas-producer/seedance/storyboards/{id}/file",
}


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (None if v is None else str(v))


def _serialize_scheduled(r: dict) -> dict:
    tool = r.get("tool")
    ref_id = r.get("ref_id")
    media_url = r.get("media_url")
    display_url = media_url
    # Cancelling deletes the hosted copies but keeps media_url on the row, so a
    # cancelled row's thumbnail comes from the proxy, which reads the original.
    if (not display_url or r.get("status") == "cancelled") and tool in _ROADMAP_FILE_PATH:
        display_url = _ROADMAP_FILE_PATH[tool].format(id=ref_id)
    md = r.get("metadata") or {}
    carousel_items = md.get("carousel_items") or []
    return {
        "id": r.get("id"),
        "tool": tool,
        "ref_id": ref_id,
        "media_type": r.get("media_type"),
        "media_url": media_url,
        "display_url": display_url,  # what the UI renders the thumbnail from
        "is_carousel": bool(md.get("is_carousel")),
        "carousel_items": carousel_items,
        "caption": r.get("caption") or "",
        "scheduled_at": _iso(r.get("scheduled_at")),
        "timezone": r.get("timezone"),
        "status": r.get("status"),
        "reminder_count": r.get("reminder_count") or 0,
        "published_at": _iso(r.get("published_at")),
        "permalink": r.get("permalink"),
        "error": r.get("error"),
        "created_at": _iso(r.get("created_at")),
    }


@router.get("/roadmap/scheduled")
async def roadmap_scheduled_list() -> dict:
    """All scheduled posts for the Library's Scheduled queue."""
    from ..services import scheduled_posts as sched_store

    rows = await sched_store.list_recent(limit=100)
    return {"items": [_serialize_scheduled(r) for r in rows]}


async def _delete_hosted_media(row: dict) -> None:
    """Best-effort delete of a post's media from the public bucket on cancel,
    every carousel slide included (the public bucket is anonymous-readable, so
    a leftover slide stays on the internet). asset_storage.parse_public_url
    reads both the S3 and the Supabase URL shapes. Never raises."""
    from ..services import scheduled_posts as sched_store

    try:
        await asset_storage.delete_public_urls(sched_store.hosted_media_urls(row))
    except Exception:
        log.warning("[roadmap] could not delete hosted media for cancelled post", exc_info=True)


@router.post("/roadmap/{post_id}/cancel")
async def roadmap_cancel(post_id: int) -> dict:
    """Cancel a scheduled post (and free its hosted media). Won't touch a
    published one."""
    from ..services import scheduled_posts as sched_store

    row = await sched_store.get(post_id)
    if not row:
        raise HTTPException(404, "scheduled post not found")
    if row.get("status") == "published":
        raise HTTPException(409, "already published — can't cancel")

    await sched_store.cancel(post_id)
    await _delete_hosted_media(row)
    log.info("[roadmap] cancelled scheduled post #%s", post_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Per-project roadmap progress — save/resume each project's step so the user can
# leave a project mid-roadmap and continue it FROM THE LIBRARY at the exact step,
# and starting another project never clobbers a different one.
# ---------------------------------------------------------------------------

class RoadmapStateRequest(BaseModel):
    tool: str
    ref_id: int
    step: str
    state: dict = {}


@router.put("/roadmap/state")
async def roadmap_state_save(body: RoadmapStateRequest) -> dict:
    from ..services import roadmap_state, scheduled_posts as sched_store

    if body.tool not in sched_store.VALID_TOOLS:
        raise HTTPException(400, f"unknown tool '{body.tool}'")
    row = await roadmap_state.save(body.tool, body.ref_id, body.step, body.state or {})
    return {"ok": True, "step": row.get("step")}


@router.get("/roadmap/state/{tool}/{ref_id}")
async def roadmap_state_get(tool: str, ref_id: int) -> dict:
    from ..services import roadmap_state

    row = await roadmap_state.get(tool, ref_id)
    if not row:
        return {}
    return {"tool": row["tool"], "ref_id": row["ref_id"], "step": row["step"], "state": row["state"]}


@router.delete("/roadmap/state/{tool}/{ref_id}")
async def roadmap_state_delete(tool: str, ref_id: int) -> dict:
    from ..services import roadmap_state

    await roadmap_state.delete(tool, ref_id)
    return {"ok": True}
