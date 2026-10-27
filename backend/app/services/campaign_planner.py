"""Invent one week of content ideas for a campaign.

This is the part that makes a campaign *autonomous* rather than a scheduler: given
a standing brief ("cyber security awareness for students") and a quota ("3 posters
and 2 videos"), it decides what those five posts should actually be.

Four inputs, each there for a reason:

* **The brief** — what the campaign is about, in the user's own words.
* **The brand profile** (`agents/producer/memory.build_brand_requirements`) so the
  ideas fit 42's palette, voice and don'ts. The poster *style* prompts already
  enforce the visual law; this enforces the editorial one.
* **Real @42abudhabi posts** nearest the topic, reranked 60/40 on similarity and
  engagement. Grounding ideas in posts that actually performed beats grounding
  them in a model's idea of a good post.
* **Memory of this campaign** — every title it has used (so week 3 doesn't
  re-pitch week 1) and the reviewer's recent decline notes (so it stops repeating
  whatever keeps getting rejected). This is the loop that makes the campaign get
  better instead of just continuing.

Output is strict JSON: one slot per planned post, each with a title, a visual
angle, the message and a call to action. The angle — not the raw brief — becomes
the topic handed to the poster pipeline, which is why five posts in one week look
like five different posts.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..config import config
from .openai_client import BudgetExceededError as OpenAIBudgetExceededError
from .openai_client import chat as openai_chat
from .vertex_client import VertexBudgetExceededError, gemini_multimodal

log = logging.getLogger("archivist.service.campaign_planner")

PLANNER_TIMEOUT_SEC = 90.0
PLANNER_MAX_TOKENS = 3000

PLANNER_SYSTEM = """\
You are the content director for @42abudhabi, a coding school in Abu Dhabi whose \
students learn by peer-to-peer project work with no teachers and no lectures.

You are planning ONE WEEK of Instagram posts for a running campaign. You will be \
given the campaign brief, how many posts of each kind are needed, the brand \
requirements, real posts from this account that performed well, and — crucially — \
the titles this campaign has ALREADY used plus any feedback from the human \
reviewer.

RULES

1. Every slot must be a GENUINELY DIFFERENT post. Different subject, different \
   visual idea, different angle on the brief. If two slots could share an image, \
   you have failed. Vary: what is depicted, the composition, the emotional \
   register (playful / stark / practical), and which part of the brief it covers.
2. NEVER repeat, rephrase or lightly re-skin a title that has already been used. \
   Treat the used-titles list as forbidden territory.
3. If the reviewer left feedback, ACT ON IT. It is the strongest signal you have \
   about what this account actually wants.
4. `angle` is a brief for an image generator, not prose for a human. Describe \
   what is literally SEEN: subject, setting, composition, mood. No brand names, \
   no logos (those are added later), no text instructions.
5. `key_message` is the single idea a viewer should walk away with. One sentence.
6. `cta` is a short, concrete call to action. No "link in bio" unless it makes \
   sense. Vary them across the week.
7. A `poster` slot is one still image. A `video` slot is a short vertical clip \
   generated from a still, so its angle must describe something that can \
   plausibly MOVE (a gesture, a machine running, a camera push) rather than a \
   static diagram.
8. Be specific and concrete. "Phishing awareness" is a bad title; "The email \
   that looks exactly like your bank" is a good one.

Reply with JSON only, in exactly this shape:

{"slots": [
  {"kind": "poster" | "video",
   "title": "short internal name for this post",
   "angle": "what the image shows, for the image generator",
   "key_message": "the one idea",
   "cta": "short call to action",
   "weekday_hint": 0}
]}

`weekday_hint` is 0=Monday..6=Sunday and is only a preference; the scheduler may \
override it. Produce EXACTLY the requested number of slots of each kind."""


def build_planner_message(
    *,
    brief: str,
    posters: int,
    videos: int,
    week_number: int,
    brand_block: str = "",
    exemplars_block: str = "",
    used_titles: list[str] | None = None,
    decline_notes: list[str] | None = None,
) -> str:
    """Assemble the user message. Pure string work — easy to unit-test."""
    parts: list[str] = [
        f"CAMPAIGN BRIEF:\n{brief.strip() or '(no brief given — use the brand voice)'}",
        f"\nTHIS IS WEEK {week_number} of the campaign.",
        f"\nSLOTS NEEDED: {posters} poster(s) and {videos} video(s) "
        f"= {posters + videos} total.",
    ]
    if brand_block:
        parts.append(f"\n{brand_block}")
    if exemplars_block:
        parts.append(f"\n{exemplars_block}")
    if used_titles:
        listed = "\n".join(f"  - {t}" for t in used_titles[:40])
        parts.append(
            "\nTITLES THIS CAMPAIGN HAS ALREADY USED — do not repeat or re-skin "
            f"any of these:\n{listed}"
        )
    if decline_notes:
        listed = "\n".join(f"  - {n}" for n in decline_notes[:10])
        parts.append(
            "\nTHE REVIEWER REJECTED EARLIER POSTS AND SAID:\n"
            f"{listed}\n"
            "Take this seriously — adjust this week's ideas so the same objection "
            "does not apply."
        )
    parts.append(
        f"\nReturn JSON with exactly {posters + videos} slots "
        f"({posters} of kind 'poster', {videos} of kind 'video')."
    )
    return "\n".join(parts)


def parse_slots(raw: str, *, posters: int, videos: int) -> list[dict]:
    """Parse and normalise the model's JSON into exactly the requested slots.

    Tolerant of the usual LLM shapes (a bare list, a fenced block, a wrapper key)
    but strict about the OUTPUT: the caller gets exactly `posters` + `videos`
    entries with every field present, so the worker never has to defend against a
    half-formed idea.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        text = text.removeprefix("json").strip()

    data: Any
    try:
        data = json.loads(text)
    except Exception:
        # Last resort: pull the outermost object out of surrounding prose.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(text[start:end + 1])
        except Exception:
            return []

    if isinstance(data, dict):
        rows = data.get("slots") or data.get("posts") or data.get("items") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    cleaned: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        angle = str(r.get("angle") or r.get("prompt") or "").strip()
        if not title or not angle:
            continue  # unusable without both
        kind = str(r.get("kind") or "poster").strip().lower()
        if kind not in ("poster", "video"):
            kind = "poster"
        hint = r.get("weekday_hint")
        cleaned.append({
            "kind": kind,
            "title": title[:200],
            "angle": angle[:2000],
            "key_message": str(r.get("key_message") or "").strip()[:500],
            "cta": str(r.get("cta") or "").strip()[:200],
            "weekday_hint": int(hint) if isinstance(hint, (int, float)) and 0 <= int(hint) <= 6 else None,
        })

    # Take the requested count per kind. If the model under-delivered on one kind
    # we return what we have — the worker plans a short week rather than nothing,
    # and the next cycle tries again.
    out: list[dict] = []
    for kind, want in (("poster", posters), ("video", videos)):
        picked = [c for c in cleaned if c["kind"] == kind][:want]
        out.extend(picked)
    return out


async def _ask(system: str, user: str) -> str:
    """One planning call: gpt-4o, then Gemini. Both metered."""
    try:
        raw = await openai_chat(
            user,
            system=system,
            model="gpt-4o",
            temperature=0.95,          # high: the whole job is variety
            max_tokens=PLANNER_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        if isinstance(raw, str) and raw.strip():
            return raw
    except OpenAIBudgetExceededError as exc:
        log.warning("[planner] OpenAI budget exhausted (%s) — trying Gemini", exc)
    except Exception as exc:
        log.warning("[planner] OpenAI planning failed (%s) — trying Gemini", exc)

    try:
        out = await gemini_multimodal(
            user,
            system=system,
            model=config.vertex_gemini_fast_model,
            temperature=0.95,
            max_tokens=PLANNER_MAX_TOKENS,
            thinking_budget=0,
        )
        if isinstance(out, dict):
            out = out.get("_raw") or json.dumps(out)
        if isinstance(out, str) and out.strip():
            log.info("[planner] via Gemini fallback")
            return out
    except VertexBudgetExceededError as exc:
        log.error("[planner] Gemini planning budget exhausted: %s", exc)
    except Exception:
        log.exception("[planner] Gemini planning failed")
    return ""


class PlanningFailedError(RuntimeError):
    """Neither provider produced a usable plan. The worker leaves the cycle
    unplanned and retries on a later tick rather than inventing filler."""


async def plan_cycle(
    *,
    brief: str,
    posters: int,
    videos: int,
    week_number: int,
    used_titles: list[str] | None = None,
    decline_notes: list[str] | None = None,
) -> list[dict]:
    """Produce this week's slots. Raises `PlanningFailedError` if it can't.

    Brand and exemplar context are best-effort: if the brand profile hasn't been
    built yet, or Instagram has never been ingested, planning still works — it
    just leans on the brief alone.
    """
    want = posters + videos
    if want <= 0:
        return []

    brand_block = ""
    exemplars_block = ""
    try:
        from ..agents.producer import memory
        req = await memory.build_brand_requirements()
        brand_block = memory.format_brand_requirements(req)
    except Exception:
        log.warning("[planner] no brand profile available — planning from the brief alone")

    try:
        from ..agents.producer import memory
        from .vertex_client import gemini_embed
        vecs = await gemini_embed([brief or "42 Abu Dhabi coding school"])
        if vecs and vecs[0]:
            posts = await memory._nearest_instagram_posts(vecs[0], k=5)
            if posts:
                exemplars_block = memory._format_instagram_block(posts)
    except Exception:
        log.warning("[planner] no Instagram exemplars available", exc_info=True)

    user_msg = build_planner_message(
        brief=brief,
        posters=posters,
        videos=videos,
        week_number=week_number,
        brand_block=brand_block,
        exemplars_block=exemplars_block,
        used_titles=used_titles,
        decline_notes=decline_notes,
    )

    raw = await _ask(PLANNER_SYSTEM, user_msg)
    slots = parse_slots(raw, posters=posters, videos=videos)

    # One retry, telling the model what was wrong. Mirrors the poster pipeline's
    # top-up loop: a single reprompt fixes most malformed or short replies.
    if len(slots) < want:
        log.info("[planner] got %d/%d slots — retrying once", len(slots), want)
        retry_msg = user_msg + (
            f"\n\nYour previous reply produced only {len(slots)} usable slot(s). "
            f"Return EXACTLY {want} slots, each with a non-empty title and angle, "
            f"as JSON."
        )
        raw2 = await _ask(PLANNER_SYSTEM, retry_msg)
        slots2 = parse_slots(raw2, posters=posters, videos=videos)
        if len(slots2) > len(slots):
            slots = slots2

    if not slots:
        raise PlanningFailedError(
            "could not plan this week's posts — the planning model returned nothing usable"
        )
    log.info("[planner] week %d planned · %d slot(s): %s",
             week_number, len(slots), ", ".join(s["title"][:40] for s in slots))
    return slots


def poster_topic_for(slot: dict) -> str:
    """Turn a planned slot into the topic string the poster pipeline expects.

    The angle leads because it describes what to draw; the message and CTA follow
    so the on-image text has something to say. Stage 1 expands this into a full
    scene description.
    """
    bits = [slot.get("angle") or slot.get("title") or ""]
    if slot.get("key_message"):
        bits.append(f"The single message: {slot['key_message']}")
    if slot.get("cta"):
        bits.append(f"Call to action: {slot['cta']}")
    return "\n".join(b for b in bits if b).strip()


def caption_direction_for(slot: dict) -> str:
    """Extra direction for the caption, so it matches the idea the image came
    from instead of restating the campaign brief."""
    bits = []
    if slot.get("key_message"):
        bits.append(f"The post's message: {slot['key_message']}")
    if slot.get("cta"):
        bits.append(f"End with this call to action (reword it naturally): {slot['cta']}")
    return "\n".join(bits)
