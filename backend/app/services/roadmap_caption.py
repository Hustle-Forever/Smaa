"""
Brand-consistent Instagram caption generation — Producer Roadmap, Step 3 (Preview).

RAG over historical @42abudhabi captions: the router embeds the asset's topic
(``gemini_embed``), retrieves the nearest high-performing posts
(``_nearest_instagram_posts``), and prompts an LLM with the references below.

This module is PURE string assembly — no I/O, no DB, no network — so it stays
trivially testable. The orchestration (embed → retrieve → chat) lives in the
yas_producer router.
"""
from __future__ import annotations

CAPTION_SYSTEM = (
    "You are the lead social copywriter for 42 Abu Dhabi — a tuition-free, peer-to-peer "
    "coding school in Abu Dhabi and part of the global 42 Network. You write Instagram "
    "captions in the brand's voice: confident, witty, developer-culture, community-driven, "
    "a little playful, never corporate-stiff. The audience is English-primary.\n\n"
    "You are given the TOPIC of a new post plus REFERENCE captions from @42abudhabi's "
    "best-performing past posts. Study their tone, length, structure, emoji and hashtag "
    "usage, then write ONE fresh caption for the new post.\n\n"
    "RULES:\n"
    "- Match the brand voice and the typical caption length of the references (usually a "
    "few short lines).\n"
    "- Open with a hook. Close with a clear call-to-action when the topic implies one "
    "(apply, register, join, swipe).\n"
    "- Use emoji sparingly and tastefully, the way the references do.\n"
    "- End with 3-8 relevant hashtags on the final line; always include #42AbuDhabi.\n"
    "- NEVER invent specifics (dates, deadlines, prizes, names, venues) that are not in the "
    "topic. If the topic lacks a detail, leave it out.\n"
    "- Output ONLY the caption text — no surrounding quotes, no 'Caption:' label, no notes."
)


def _format_reference(idx: int, ref: dict) -> str:
    """One reference post → a compact block for the prompt."""
    caption = (ref.get("caption") or "").strip()
    summary = (ref.get("ai_summary") or "").strip()
    likes = int(ref.get("like_count") or 0)
    comments = int(ref.get("comments_count") or 0)
    body = caption[:600] if caption else (summary[:400] if summary else "(no caption)")
    return f"[{idx}] ♥{likes} · 💬{comments}\n{body}"


def build_caption_user_message(topic: str, references: list[dict] | None) -> str:
    """Compose the user message: the new topic + the retrieved reference captions."""
    topic = (topic or "").strip()
    refs = references or []
    if refs:
        block = "\n\n".join(_format_reference(i + 1, r) for i, r in enumerate(refs))
    else:
        block = (
            "(no reference posts retrieved — rely on the @42abudhabi brand voice described above)"
        )
    return (
        f"[NEW POST TOPIC]\n{topic}\n\n"
        f"[REFERENCE CAPTIONS FROM @42abudhabi — most relevant, highest-performing]\n{block}\n\n"
        "[YOUR TASK]\nWrite ONE Instagram caption for the new post, in @42abudhabi's voice. "
        "Output only the caption."
    )
