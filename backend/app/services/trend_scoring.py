"""Trend Finder — the AI pass that turns competitor posts into usable topics.

Engagement tells you a post worked *for them*. It says nothing about whether 42
Abu Dhabi — a tuition-free, peer-to-peer coding school — could or should make
something like it. That judgement is what this module adds:

  * **topic**            the transferable ANGLE, not a summary of their post
  * **theme**            a reusable bucket so the Themes tab can group things
  * **fit_score**        can 42 actually make this, with what they have
  * **uae_relevance**    does it land with a UAE audience
  * **suggested_angle**  how 42 would do it

Scoring is VISUAL: the thumbnail is sent with the caption, because a caption
alone routinely misreads a post (a photo dump and a data-viz carousel can have
near-identical captions). The scan already downloaded those thumbnails, so this
costs nothing extra.

`opportunity_score` is computed in Python, never by the model — it blends the
three signals with tunable weights so the UI can re-rank instantly without
re-running the AI.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import trend_store as store
from .storage import resolve_local
from .vertex_client import gemini_multimodal

log = logging.getLogger("archivist.trend_scoring")

# Small batches keep the image payload sane and the JSON parseable.
BATCH_SIZE = 8
MAX_IMAGES_PER_BATCH = 8

# Opportunity weights. Outperformance and fit matter most; UAE relevance is the
# tie-breaker that keeps the feed locally grounded.
DEFAULT_WEIGHTS = {"outperformance": 0.4, "fit": 0.4, "uae": 0.2}
# Outperformance is compressed logarithmically before it enters the blend.
#
# A linear cap flattens the top of the feed: measured on real accounts, a big
# account's median is dragged down by its long tail, so @visitabudhabi produced
# posts at 44x, 34x and 22x its own median while @42abudhabi topped out at 4.7x.
# Under a linear 3x cap every one of those tied at full marks and the ranking
# stopped discriminating exactly where it matters. A log curve keeps the busy
# 1x-5x band well spread and still refuses to let a freak viral post run away
# with the feed.
OUTPERFORMANCE_CEILING = 8.0

# The fit below which an AI News story is hidden by default.
#
# MEASURED, not guessed (2026-08-18, 24 real headlines through the news prompt).
# The scored values leave two natural gaps, and 40 sits in the wider one — 17.5
# points of empty space between the highest hidden item (22.5) and the lowest
# shown one (40). That margin is what makes the gate stable: run-to-run the same
# story moves by a median of 3.2 points, so nothing near this line flips between
# ticks. A bar at 70 would sit in a 4.5-point gap and reshuffle every run.
#
# At 40 it hides 25% of real stories, and the six it hid were two Middle East
# politics pieces with no AI in them, a cricket sponsorship, an OpenAI port
# logistics partnership, an NVIDIA infrastructure-security post and an ASUS
# laptop launch. Exactly the intended target.
#
# The feed states the count it is hiding and the Min-fit select is set to this
# rather than to "Any fit", so the gate is always one click from being lifted.
NEWS_MIN_FIT_DEFAULT = 40

_SYSTEM = (
    "You advise the social team at 42 Abu Dhabi — a tuition-free, peer-to-peer, "
    "project-based coding school in Abu Dhabi, UAE. Students learn without "
    "teachers or lectures, admission runs through an intensive month called the "
    "Piscine, and no prior coding experience is required.\n\n"
    "You are shown items that are currently working. Most are POSTS from other "
    "Instagram accounts that performed unusually well for those accounts. Some "
    "are TRENDS — topics and conversations rising across social and news media, "
    "with no single post behind them. Judge both the same way: work out what 42 "
    "could learn from it and whether 42 could realistically make something like "
    "it. For a TREND, the angle is what 42 would post to join that conversation, "
    "not a report about the trend itself.\n\n"
    "Judge fit honestly and specifically:\n"
    "- HIGH fit: student stories and wins, campus and community life, coding "
    "culture, tech explainers, Emirati and UAE cultural moments, recruitment and "
    "admissions, alumni careers, hackathons — things a school with students, a "
    "campus and a phone can shoot.\n"
    "- LOW fit: anything needing budget, assets or access 42 does not have — "
    "luxury travel shoots, celebrity appearances, product launches, aerial "
    "cinematography, paid talent.\n"
    "Do not inflate scores. A beautiful post that 42 cannot make is a LOW fit, "
    "however well it performed.\n\n"
    "The `topic` must be the transferable ANGLE 42 could use, not a description "
    "of their post. Bad: 'Emirates airline cabin crew video'. "
    "Good: 'A day in the life, told by the people who live it'."
)

# The news arm of the same prompt. Two additions, both measured.
#
# 1. The NON-TECHNICAL rule. AI News exists so 42's feed looks current on AI to
#    a general audience — prospective students and their parents — not to
#    engineers. Measured on 24 real headlines (2026-08-18): this rule moves mean
#    fit from 69.7 to 58.3 and buries exactly the right things (an NVIDIA
#    infrastructure-security post 78 → 15, an OpenAI port-logistics partnership
#    68 → 10) while human-interest and UAE stories hold (sign-language AI
#    93 → 95, "UAE leads in practical AI and robotics" 100 → 98).
#
# 2. ENDPOINT ANCHORS. The stock prompt gives categorical HIGH/LOW guidance but
#    never says what 90 means against 70, and the histogram shows the cost:
#    across 111 scored rows the model used 12 distinct values, all multiples of
#    5, with 65% at 90+. Anchoring only the two endpoints is the cheapest known
#    fix for exactly that compression.
#
#    The anchors below are INVENTED, not drawn from any evaluation set. Lifting
#    real examples would let the arm win by leakage and the measurement would
#    mean nothing.
_SYSTEM_NEWS = _SYSTEM + (
    "\n\nThese items are NEWS STORIES about AI. 42 wants to look genuinely "
    "current on AI to prospective students and their parents — not to "
    "engineers.\n\n"
    "Score fit LOW for anything whose interest is technical: model and version "
    "releases, benchmark numbers, funding rounds and valuations, research "
    "papers, API and pricing changes, enterprise infrastructure, chip supply, "
    "and vendor product announcements. A story does not escape this because a "
    "famous lab is in it. It escapes ONLY if there is a plain-language "
    "consequence a 17-year-old in Abu Dhabi would actually care about — what "
    "they can now do, learn, earn, or worry about.\n\n"
    "Score fit HIGH for what AI is doing to jobs, study, money, health, "
    "language, creativity and daily life; for anything happening in the UAE or "
    "the Gulf; and for stories a student would send to a friend.\n\n"
    "Use the WHOLE 0-100 range. Anchors:\n"
    "- 90-100: 42 could post about this tomorrow and its students would care. "
    "Concrete, human, and either local or about their own future.\n"
    "- 0-20: nothing here for a general audience — a version number, a "
    "benchmark table, a funding round, or a story with no AI in it at all.\n"
    "Do not cluster on 80 and 90. If two stories differ in usefulness, give "
    "them different numbers."
)

_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer",
                              "description": "the POST index given in the prompt"},
                    "topic": {"type": "string",
                              "description": "the transferable angle, max 8 words"},
                    "topic_summary": {"type": "string",
                                      "description": "one sentence on why it worked"},
                    "theme": {"type": "string",
                              "description": "a short reusable bucket, e.g. "
                                             "'student success', 'campus life', "
                                             "'Emirati culture', 'tech explainer', "
                                             "'recruitment', 'events'"},
                    "fit_score": {"type": "integer", "description": "0-100"},
                    "fit_reason": {"type": "string",
                                   "description": "one concrete sentence"},
                    "uae_relevance": {"type": "integer", "description": "0-100"},
                    "suggested_angle": {"type": "string",
                                        "description": "how 42 would make this"},
                    "suggested_format": {"type": "string",
                                         "enum": ["reel", "carousel", "static"]},
                    "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "topic", "theme", "fit_score",
                             "uae_relevance", "suggested_angle"],
            },
        },
    },
    "required": ["scores"],
}


# ---------------------------------------------------------------------------
# Pure helpers (no network, no DB — these are what the tests drive)
# ---------------------------------------------------------------------------

def normalise_weights(weights: Optional[dict]) -> dict:
    """Clamp to non-negative and renormalise to sum 1, so any slider combination
    the UI sends produces a comparable 0-100 score."""
    w = dict(DEFAULT_WEIGHTS)
    for key in w:
        raw = (weights or {}).get(key)
        if raw is not None:
            try:
                w[key] = max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    total = sum(w.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in w.items()}


def normalise_outperformance(value: Any) -> float:
    """Map an outperformance ratio onto 0..1 on a log curve.

    1.0 means "typical for that account". The curve spreads the 1x-5x band where
    most real posts sit and saturates at OUTPERFORMANCE_CEILING, so a 44x freak
    and an 8x hit both score full marks rather than the 44x one crowding out
    everything a human would actually want to see."""
    try:
        ratio = max(float(value or 0.0), 0.0)
    except (TypeError, ValueError):
        return 0.0
    return min(math.log1p(ratio) / math.log1p(OUTPERFORMANCE_CEILING), 1.0)


def compute_opportunity(post: dict, weights: Optional[dict] = None) -> float:
    """Blend outperformance, fit and UAE relevance into a single 0-100 score.

    Outperformance is a ratio where 1.0 means "typical for that account"; it is
    capped because a 20x outlier is not five times more useful than a 4x one.
    A post with no AI scoring yet falls back to its outperformance alone rather
    than scoring 0, so a fresh scan still ranks sensibly.

    Only the signals that ACTUALLY EXIST are blended, and the weights are
    renormalised over them. A missing signal must not be scored as zero: a
    partially-parsed reply — `fit_score` dropped, `uae_relevance` present — used
    to lose all 40 fit points and rank *below* a post that was never scored at
    all (22.6 against 31.6 at outperformance 1.0). Absent evidence is not
    evidence of a bad fit.

    Both-present and both-absent are arithmetically unchanged by this, so the
    established competitor ranking is exactly what it was."""
    w = normalise_weights(weights)
    out_norm = normalise_outperformance(post.get("outperformance"))

    fit = post.get("fit_score")
    uae = post.get("uae_relevance")

    # Outperformance used to be the one signal that was never dropped, because
    # every competitor row has it. An Industry Signal row does NOT: a web search
    # cannot honestly count how many outlets ran a story, so that column is left
    # NULL rather than faked at a constant. Without this guard a null scored 0.0
    # and still took its full 40% of the weight, which would rank every news
    # story near the bottom of a blend it does not participate in.
    parts: list[tuple[str, float]] = []
    if post.get("outperformance") is not None:
        parts.append(("outperformance", out_norm))
    if fit is not None:
        parts.append(("fit", min(max(float(fit), 0.0), 100.0) / 100.0))
    if uae is not None:
        parts.append(("uae", min(max(float(uae), 0.0), 100.0) / 100.0))

    total_w = sum(w[key] for key, _ in parts)
    if total_w <= 0:
        # Every available signal was weighted to zero by the sliders. Fall back
        # to raw outperformance rather than returning a flat 0 for everything.
        return round(100.0 * out_norm, 2)
    score = sum(w[key] * value for key, value in parts) / total_w
    return round(100.0 * score, 2)


class ScoringUnavailable(RuntimeError):
    """The scoring model can't be reached at all — a configuration problem, not
    a transient one. Ranking still works on outperformance alone, so this
    downgrades the feature rather than breaking it."""


def _is_unavailable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in (
        "billing", "permission_denied", "403", "api key", "credential",
        "not enabled", "unauthenticated", "401",
    ))


def _explain_unavailable(exc: BaseException) -> str:
    msg = str(exc)
    if "billing" in msg.lower():
        return ("AI scoring is unavailable: the Google Cloud project has billing "
                "disabled. Posts are ranked by engagement until it's enabled, "
                "then re-scan or hit Re-score to fill in the fit scores.")
    return ("AI scoring is unavailable right now — posts are ranked by "
            "engagement only. " + msg[:160])


SORTS = ("opportunity", "outperformance", "engagement", "recent", "fit", "viral")


def viral_score(post: dict) -> float:
    """0-100: how likely this would travel. The `viral` sort's key.

    A news story carries the judge's `viral_potential` in its metadata. A
    competitor post's outperformance IS its measured virality, scaled the same
    way the blend scales it, so the two kinds of row can share one order on the
    combined channel. Missing is 0, not a guess: a story from before the score
    existed sorts to the bottom until the housekeeping tick scores it.
    """
    meta = post.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("viral_potential") is not None:
        try:
            return float(min(max(int(meta["viral_potential"]), 0), 100))
        except (TypeError, ValueError):
            pass
    if post.get("outperformance") is not None:
        return round(100.0 * normalise_outperformance(post.get("outperformance")), 2)
    return 0.0


def rank_posts(
    posts: list[dict], *, sort: str = "opportunity",
    weights: Optional[dict] = None,
) -> list[dict]:
    """Re-score and order the feed. Pure, so the weight sliders re-rank instantly
    without touching the database or the model."""
    w = normalise_weights(weights)
    out = []
    for p in posts:
        row = dict(p)
        # Always recompute: the stored score used whatever weights were live at
        # scoring time, and the caller may be asking for a different blend.
        row["opportunity_score"] = compute_opportunity(row, w)
        out.append(row)

    keys = {
        "opportunity": lambda r: (r.get("opportunity_score") or 0.0),
        "outperformance": lambda r: (r.get("outperformance") or 0.0),
        "engagement": lambda r: (r.get("engagement") or 0),
        "fit": lambda r: (r.get("fit_score") or 0),
        "viral": viral_score,
    }
    if sort == "recent":
        out.sort(key=lambda r: (r.get("posted_at") is not None, r.get("posted_at")),
                 reverse=True)
    else:
        out.sort(key=keys.get(sort, keys["opportunity"]), reverse=True)
    return out


def build_batch_prompt(posts: list[dict], brand_note: str = "") -> str:
    """One prompt describing N items. Images are attached in the same order and
    referenced by index, so the model can tie picture to metrics.

    Competitor posts and news stories can share a batch, so each row is rendered
    from its own `source`: an Instagram post leads with likes and its account's
    average, a news story with its outlet and how widely it was covered. Both
    still answer the same question."""
    lines: list[str] = []
    if brand_note:
        lines.append("WHAT 42 SOUNDS AND LOOKS LIKE:\n" + brand_note.strip() + "\n")
    # A batch of nothing but competitor posts keeps its original wording exactly,
    # so the established path's prompt is byte-for-byte what it always was.
    if any((p.get("source") or "competitor") != "competitor" for p in posts):
        lines.append(
            f"Score the {len(posts)} items below. Image N (in order) is ITEM N — "
            "news stories often have no image. Return one entry per item, echoing "
            "its index."
        )
    else:
        lines.append(
            f"Score the {len(posts)} posts below. Image N (in order) is POST N. "
            "Return one entry per post, echoing its index."
        )
    for i, p in enumerate(posts, start=1):
        lines.append(_render_item(i, p))
    return "\n\n".join(lines)


def _render_item(index: int, p: dict) -> str:
    """One item's block. Split out so each source can lead with the metric that
    actually means something for it."""
    caption = " ".join(str(p.get("caption") or "").split())[:420]
    out = p.get("outperformance")

    if (p.get("source") or "competitor") == "news":
        meta = p.get("metadata") if isinstance(p.get("metadata"), dict) else {}
        bits = [f"STORY {index} (a news item, not a post someone made)"]
        if p.get("platform"):
            bits.append(f"outlet: {p['platform']}")
        if p.get("posted_at"):
            bits.append(f"published: {str(p['posted_at'])[:10]}")
        coverage = meta.get("coverage")
        if coverage and int(coverage) > 1:
            # How many outlets ran the story — the news analogue of engagement,
            # and the only popularity signal this source gives us.
            bits.append(f"covered by {coverage} outlets")
        if p.get("region"):
            bits.append(f"where it matters: {p['region']}")
        bits.append(f"headline: {caption or '(none)'}")
        return "\n".join("  " + b for b in bits)

    bits = [
        f"POST {index}",
        f"account: @{p.get('username')}",
        f"format: {p.get('media_type')}",
        f"engagement: {p.get('like_count') or 0} likes, "
        f"{p.get('comments_count') or 0} comments",
    ]
    if p.get("view_count"):
        bits.append(f"views: {p['view_count']}")
    if out:
        bits.append(f"performed {float(out):.1f}x that account's own average")
    bits.append(f"caption: {caption or '(none)'}")
    return "\n".join("  " + b for b in bits)


def _has_thumb(post: dict) -> bool:
    return bool(post.get("local_thumb_path"))


def partition_batches(posts: list[dict], size: int = BATCH_SIZE) -> list[list[dict]]:
    """Split the pending queue into batches that are safe to score together.

    Two rules, each fixing a real defect:

    **Partition by has-thumbnail.** `build_batch_prompt` tells the model "Image
    N is ITEM N", but `score_batch` builds the image list by SKIPPING items with
    no thumbnail — a compacted list. Mix an imageless row into a batch and every
    later item is described by the previous item's picture, silently corrupting
    fit scores. Invisible until now because every competitor post has a thumb;
    roughly half of all news sources ship no image at all, so this would have
    fired on the first mixed batch and damaged *competitor* scores in it.

    **Partition by source.** Scores are batch-relative — the model is shown
    eight items and grades them against each other — while the feed sorts across
    every batch ever run. Grouping by source makes that bias a constant offset
    within a category instead of depending on which seven rows happened to sit
    next to a post in the database. It also keeps each batch on one system
    prompt, which is what lets news get its own.

    Ordering inside a group is deterministic (outperformance, then id) so the
    same queue always produces the same batches — a precondition for the
    test-retest measurement meaning anything.
    """
    groups: dict[tuple[str, bool], list[dict]] = {}
    for post in posts:
        key = ((post.get("source") or "competitor"), _has_thumb(post))
        groups.setdefault(key, []).append(post)

    out: list[list[dict]] = []
    for key in sorted(groups, key=lambda k: (k[0], not k[1])):
        rows = sorted(
            groups[key],
            key=lambda p: (-(float(p.get("outperformance") or 0.0)), int(p.get("id") or 0)),
        )
        for start in range(0, len(rows), size):
            out.append(rows[start:start + size])
    return out


def system_for(posts: list[dict]) -> str:
    """The system prompt for a batch. `partition_batches` guarantees one source
    per batch, so the first row decides it."""
    source = (posts[0].get("source") or "competitor") if posts else "competitor"
    return _SYSTEM_NEWS if source == "news" else _SYSTEM


# How often the model's reply is unusable, and in what way. Silent drops are
# what made the partial-parse ranking bug invisible for so long; these counters
# are the denominator for any future claim about scoring quality. Process-local
# and monotonic — read them from /api/trends/news/health.
_DROPS: dict[str, int] = {
    "batches_ok": 0,
    "batches_unparseable": 0,
    "batches_failed": 0,
    "entries_bad_index": 0,
    "items_not_returned": 0,
    "fit_missing": 0,
    "uae_missing": 0,
}


def drop_stats() -> dict[str, int]:
    """A snapshot of the scoring-loss counters."""
    return dict(_DROPS)


def parse_batch_response(raw: Any, posts: list[dict]) -> dict[int, dict]:
    """Map the model's reply back onto post ids by the echoed index.

    Tolerant by design: a malformed or short reply scores what it can rather
    than throwing away the whole batch. Every tolerated loss is counted."""
    out: dict[int, dict] = {}
    if not isinstance(raw, dict):
        return out
    for entry in (raw.get("scores") or []):
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            _DROPS["entries_bad_index"] += 1
            continue
        if not (1 <= idx <= len(posts)):
            _DROPS["entries_bad_index"] += 1
            continue
        post = posts[idx - 1]
        score = {
            "topic": str(entry.get("topic") or "").strip()[:160] or None,
            "topic_summary": str(entry.get("topic_summary") or "").strip()[:600] or None,
            "theme": (str(entry.get("theme") or "").strip().lower()[:60] or None),
            "fit_score": _clamp_int(entry.get("fit_score")),
            "fit_reason": str(entry.get("fit_reason") or "").strip()[:600] or None,
            "uae_relevance": _clamp_int(entry.get("uae_relevance")),
            "suggested_angle": str(entry.get("suggested_angle") or "").strip()[:600] or None,
            "suggested_format": _one_of(entry.get("suggested_format"),
                                        ("reel", "carousel", "static")),
            "effort": _one_of(entry.get("effort"), ("low", "medium", "high")),
            "tags": [str(t)[:40] for t in (entry.get("tags") or [])][:8],
        }
        if score["fit_score"] is None:
            _DROPS["fit_missing"] += 1
        if score["uae_relevance"] is None:
            _DROPS["uae_missing"] += 1
        score["opportunity_score"] = compute_opportunity({**post, **score})
        out[int(post["id"])] = score
    missing = len(posts) - len(out)
    if missing > 0:
        _DROPS["items_not_returned"] += missing
    return out


def _clamp_int(value: Any) -> Optional[int]:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _one_of(value: Any, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().lower()
    return v if v in allowed else None


# ---------------------------------------------------------------------------
# The scoring pass
# ---------------------------------------------------------------------------

async def _brand_note() -> str:
    """A short grounding block about 42. Best-effort — scoring still works
    without it, just with less brand nuance."""
    try:
        from ..agents.producer.memory import build_brand_requirements

        brand = await build_brand_requirements()
    except Exception:  # noqa: BLE001
        log.info("[trend-finder] brand context unavailable — scoring without it")
        return ""
    if not isinstance(brand, dict):
        return ""
    # These are the keys build_brand_requirements actually returns — every one
    # is always present, as a list (empty when the brand profile has no data).
    labels = {
        "voice_pillars": "voice",
        "voice_do": "voice — do",
        "voice_dont": "voice — don't",
        "style_pillars": "visual style",
        "brand_dos": "brand do",
        "brand_donts": "brand don't",
    }
    bits: list[str] = []
    for key, label in labels.items():
        val = brand.get(key)
        if isinstance(val, list) and val:
            bits.append(f"{label}: " + "; ".join(str(v)[:90] for v in val[:6]))
        elif isinstance(val, str) and val.strip():
            bits.append(f"{label}: {val.strip()[:300]}")
    return "\n".join(bits)[:1500]


def _thumb_path(post: dict) -> Optional[Path]:
    rel = post.get("local_thumb_path")
    if not rel:
        return None
    try:
        p = resolve_local(str(rel))
        return p if p.is_file() else None
    except Exception:  # noqa: BLE001
        return None


async def score_batch(posts: list[dict], brand_note: str = "") -> dict[int, dict]:
    """Score one batch. Returns {post_id: score}; an empty dict on failure so the
    caller can move on instead of losing the whole run."""
    if not posts:
        return {}
    # Every post in a batch either has a thumbnail or none of them do — see
    # partition_batches — so this list is never compacted relative to `posts`
    # and "Image N is ITEM N" stays true.
    images = [p for p in (_thumb_path(x) for x in posts) if p is not None]
    prompt = build_batch_prompt(posts, brand_note)
    try:
        raw = await gemini_multimodal(
            prompt,
            images=images[:MAX_IMAGES_PER_BATCH] or None,
            system=system_for(posts),
            json_schema=_SCHEMA,
            temperature=0.3,
            max_tokens=6000,
        )
    except Exception as exc:  # noqa: BLE001 — one bad batch must not end the pass
        if _is_unavailable(exc):
            # Billing off, credentials missing, model not enabled — every
            # subsequent batch will fail identically, so stop now rather than
            # logging the same stack trace a dozen times per scan.
            raise ScoringUnavailable(_explain_unavailable(exc)) from exc
        _DROPS["batches_failed"] += 1
        log.exception("[trend-finder] scoring batch failed (%d post(s))", len(posts))
        return {}
    if isinstance(raw, dict) and raw.get("_raw") is not None:
        _DROPS["batches_unparseable"] += 1
        log.warning("[trend-finder] scoring returned unparseable JSON — skipping batch")
        return {}
    _DROPS["batches_ok"] += 1
    return parse_batch_response(raw, posts)


async def score_pending(
    *, limit: int = 200, source: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> int:
    """Score every post waiting on the AI pass. Returns how many were scored.

    `source` narrows the pass to one channel — the news collector scopes itself
    to `'news'` so an unattended tick cannot start chewing through a competitor
    scan's backlog while that scan is mid-flight."""
    pending = await store.unscored_posts(limit=limit, source=source)
    if not pending:
        return 0
    brand_note = await _brand_note()
    total, done = len(pending), 0

    for batch in partition_batches(pending):
        try:
            scores = await score_batch(batch, brand_note)
        except ScoringUnavailable:
            raise  # the caller reports it; retrying the rest is pointless
        for post_id, score in scores.items():
            try:
                await store.save_score(post_id, score)
                done += 1
            except Exception:  # noqa: BLE001
                log.exception("[trend-finder] could not save score for post #%d", post_id)
        if on_progress:
            try:
                await on_progress(done, total)
            except Exception:  # noqa: BLE001 — progress is cosmetic
                pass

    log.info("[trend-finder] scored %d/%d post(s)", done, total)
    return done


async def rescore(post_ids: Optional[list[int]] = None) -> int:
    """Clear scores and run the pass again — e.g. after the brand profile
    changed, or the fit prompt was tuned."""
    cleared = await store.clear_scores(post_ids)
    log.info("[trend-finder] cleared %d score(s) for re-scoring", cleared)
    return await score_pending()
