"""Generate an Instagram caption for a topic, in the @42abudhabi voice.

Extracted verbatim from the body of `POST /roadmap/caption` so non-request
callers can use it: the campaign worker writes a caption for every item it
generates, and it cannot import a router (`main.py` imports both — an import
cycle). The route keeps its HTTP shell and calls this.

Two things worth knowing:

* **The voice comes from retrieval, not just a prompt.** The topic is embedded
  and the nearest real @42abudhabi posts are pulled in as exemplars, reranked
  60/40 on similarity and engagement. So captions imitate posts that actually
  performed rather than a generic idea of the brand.
* **Two providers, because one caption failing must not block a post.** gpt-4o
  first, Gemini Flash on any failure — including the budget being exhausted,
  which is the common one. Both dead is the only error.

`roadmap_caption` stays pure string assembly; all the I/O is here.
"""
from __future__ import annotations

import logging

from ..config import config
from .openai_client import BudgetExceededError as OpenAIBudgetExceededError
from .openai_client import chat as openai_chat
from .roadmap_caption import CAPTION_SYSTEM, build_caption_user_message
from .vertex_client import VertexBudgetExceededError, gemini_embed, gemini_multimodal

log = logging.getLogger("archivist.service.caption")

# How many real posts to retrieve as voice exemplars.
REFERENCE_COUNT = 6


class CaptionUnavailableError(RuntimeError):
    """Both providers failed. The caller decides what that means — the route
    turns it into a 502, the campaign worker records it as an attempt failure."""


async def nearest_references(topic: str, *, k: int = REFERENCE_COUNT) -> list[dict]:
    """Real posts closest to `topic`. Best-effort: retrieval failing degrades the
    caption to topic-only rather than failing the caller."""
    try:
        from ..agents.producer.memory import _nearest_instagram_posts
        vecs = await gemini_embed([topic])
        if vecs and vecs[0]:
            return await _nearest_instagram_posts(vecs[0], k=k)
    except Exception:
        log.exception("[caption] retrieval failed — using topic-only fallback")
    return []


def summarise_references(references: list[dict], *, limit: int = REFERENCE_COUNT) -> list[dict]:
    """Trim exemplars to what a UI needs to show its work."""
    return [
        {
            "caption": ((r.get("caption") or r.get("ai_summary") or "").strip())[:280],
            "permalink": r.get("permalink"),
            "likes": int(r.get("like_count") or 0),
        }
        for r in references[:limit]
    ]


async def generate_caption(topic: str, *, extra_direction: str | None = None) -> dict:
    """Write one caption for `topic`.

    `extra_direction` is appended to the user message — the campaign planner uses
    it to pass the item's angle and call to action, so the caption matches the
    idea the image was drawn from instead of restating the topic.

    Returns `{"caption": str, "references": [...], "provider": str}`.
    Raises `CaptionUnavailableError` when both providers fail.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required to generate a caption")

    references = await nearest_references(topic)
    user_msg = build_caption_user_message(topic, references)
    if extra_direction and extra_direction.strip():
        user_msg += f"\n\nADDITIONAL DIRECTION FOR THIS POST:\n{extra_direction.strip()}"

    caption: str | None = None
    provider = ""

    # --- OpenAI first ---
    try:
        caption = await openai_chat(
            user_msg,
            system=CAPTION_SYSTEM,
            model="gpt-4o",
            max_tokens=500,
            temperature=0.7,
        )
        if isinstance(caption, str) and caption.strip():
            provider = "openai:gpt-4o"
            log.info("[caption] via OpenAI gpt-4o")
    except OpenAIBudgetExceededError as exc:
        log.warning("[caption] OpenAI budget exhausted (%s) — falling back to Gemini", exc)
    except Exception as exc:
        log.warning("[caption] OpenAI failed (%s) — falling back to Gemini", exc)

    # --- Gemini fallback, same system prompt and same retrieved exemplars ---
    if not (isinstance(caption, str) and caption.strip()):
        try:
            out = await gemini_multimodal(
                user_msg,
                system=CAPTION_SYSTEM,
                model=config.vertex_gemini_fast_model,
                temperature=0.7,
                max_tokens=600,
                thinking_budget=0,  # disable thinking to prevent truncation
            )
            if isinstance(out, dict):  # rare on plain text
                out = out.get("_raw") or ""
            caption = out
            if isinstance(caption, str) and caption.strip():
                provider = f"gemini:{config.vertex_gemini_fast_model}"
                log.info("[caption] via Gemini fallback (%s)", config.vertex_gemini_fast_model)
        except VertexBudgetExceededError as exc:
            log.error("[caption] Gemini fallback budget exhausted: %s", exc)
        except Exception:
            log.exception("[caption] Gemini fallback failed")

    if not (isinstance(caption, str) and caption.strip()):
        raise CaptionUnavailableError(
            "caption generation failed — both OpenAI and Gemini are unavailable"
        )

    return {
        "caption": caption.strip(),
        "references": summarise_references(references),
        "provider": provider,
    }
