"""Trend Finder — AI discovery of accounts worth watching.

The "Research" button. The model proposes handles worth following for 42 Abu
Dhabi, then **every candidate is checked against the real Instagram API** before
it is ever shown. That validation step is the whole point: models cheerfully
invent plausible handles, and Business Discovery only reaches Business/Creator
accounts anyway — so roughly half of any suggestion list is unusable. Verified
live: `@adnoc`, `@42network` and `@42lausanne` all fail to resolve.

Anything already tracked is excluded from the prompt, and anything the user has
**ignored** (`status='excluded'`) is never suggested again — that is what makes
the button safe to press repeatedly.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from . import instagram_client as ig
from . import trend_store as store
from .vertex_client import gemini_text

log = logging.getLogger("archivist.trend_research")

# Validation hits the Graph API once per candidate, so keep the fan-out polite.
_VALIDATE_CONCURRENCY = 4
MAX_CANDIDATES = 12

# ---------------------------------------------------------------------------
# Discovery focus — the four dials on the Research panel.
#
# These steer WHICH accounts the model proposes. They are NOT the feed's
# ranking weights (trend_scoring.DEFAULT_WEIGHTS) — those re-rank posts that
# already exist. Every dial's FIRST option reproduces the previous behaviour
# byte-for-byte, so an omitted dial is not a behaviour change.
# ---------------------------------------------------------------------------

GEO_OPTIONS = ("uae", "global")
RELEVANCE_OPTIONS = ("normal", "high")
ENGAGEMENT_OPTIONS = ("any", "high")
SIZE_OPTIONS = ("any", "small", "large")


@dataclass(frozen=True)
class Focus:
    geo: str = "uae"
    relevance: str = "normal"
    engagement: str = "any"
    size: str = "any"


DEFAULT_FOCUS = Focus()


def normalise_focus(
    *, geo: Optional[str] = None, relevance: Optional[str] = None,
    engagement: Optional[str] = None, size: Optional[str] = None,
) -> Focus:
    """Coerce whatever came off the wire into a valid Focus. Anything
    unrecognised falls back to that dial's default, so a stale frontend can
    never break the call."""
    def pick(value: Optional[str], allowed: tuple[str, ...]) -> str:
        v = str(value or "").strip().lower()
        return v if v in allowed else allowed[0]
    return Focus(
        geo=pick(geo, GEO_OPTIONS),
        relevance=pick(relevance, RELEVANCE_OPTIONS),
        engagement=pick(engagement, ENGAGEMENT_OPTIONS),
        size=pick(size, SIZE_OPTIONS),
    )


_SYSTEM_INTRO = (
    "You help the social team at 42 Abu Dhabi decide which Instagram accounts to "
    "watch for content ideas. 42 Abu Dhabi is a tuition-free, peer-to-peer, "
    "project-based coding school in Abu Dhabi, UAE — students learn without "
    "teachers or lectures, admission runs through an intensive month called the "
    "Piscine, and no prior coding experience is required.\n\n"
)

# The geography dial. "uae" is the previous, unconditional text.
_GEO_SYSTEM = {
    "uae": (
        "Suggest real, currently-active Instagram accounts that are useful to "
        "learn from. Weight the list toward the UAE:\n"
        "- MOSTLY UAE and wider GCC: universities and schools, tech and startup "
        "communities, government and culture accounts, youth and education "
        "initiatives, coworking and innovation hubs.\n"
        "- A FEW international peers of a coding school, for format ideas.\n\n"
    ),
    "global": (
        "Suggest real, currently-active Instagram accounts that are useful to "
        "learn from. Search WORLDWIDE. Do NOT weight the list toward the UAE and "
        "do not pad it with local accounts just because 42 is in Abu Dhabi:\n"
        "- ANY country: coding schools and bootcamps, CS faculties and student "
        "communities, developer-education and maker accounts, hackathons, tech "
        "and startup communities, youth and education initiatives.\n"
        "- Pick them for how well their CONTENT would transfer to 42 Abu Dhabi, "
        "not for where they are.\n\n"
    ),
}

_RELEVANCE_SYSTEM = {
    "normal": "",
    "high": (
        "TIGHT RELEVANCE: every account must be one a coding school could "
        "plausibly borrow a post format from — coding schools, bootcamps, CS "
        "faculties, developer communities, hackathons, student life, tech "
        "education. Drop lifestyle, tourism, retail, property and general news "
        "accounts even when they are popular.\n\n"
    ),
}

_ENGAGEMENT_SYSTEM = {
    "any": "",
    "high": (
        "HIGH ENGAGEMENT ONLY: prefer accounts whose posts reliably pull a lot "
        "of likes and comments and whose comment sections are actually alive. A "
        "quiet account with a big follower count is worse than a smaller one "
        "people argue in.\n\n"
    ),
}

_SIZE_SYSTEM = {
    "any": "",
    "small": (
        "ACCOUNT SIZE: prefer small-to-mid accounts — roughly 5k to 150k "
        "followers — whose output 42's team could realistically match.\n\n"
    ),
    "large": (
        "ACCOUNT SIZE: prefer large, established accounts — roughly 150k "
        "followers and up — with a proven, repeatable content formula.\n\n"
    ),
}

_SYSTEM_RULES = (
    "Rules:\n"
    "- Only accounts you are genuinely confident exist. Do not invent handles.\n"
    "- Prefer ORGANISATIONS over individuals — they post consistently and are "
    "far more likely to be Business/Creator accounts (personal accounts cannot "
    "be read by the API at all).\n"
    "- Give the handle exactly, with no @ and no URL.\n"
    "- `region` must be `uae`, `gcc` or `international`. Use `international` "
    "for anything outside the Gulf — getting this wrong corrupts the team's "
    "region filters.\n"
    "- `why` must say what 42 would specifically learn from them."
)


def build_system(*, focus: Focus = DEFAULT_FOCUS) -> str:
    """Pure — the tests drive this directly, exactly like build_prompt."""
    return (
        _SYSTEM_INTRO
        + _GEO_SYSTEM[focus.geo]
        + _RELEVANCE_SYSTEM[focus.relevance]
        + _ENGAGEMENT_SYSTEM[focus.engagement]
        + _SIZE_SYSTEM[focus.size]
        + _SYSTEM_RULES
    )


# Kept as a module constant for the default path (and for anything that still
# imports it); every per-request call goes through build_system().
_SYSTEM = build_system()

_GEO_LINE = {
    # The default string is pinned by test_research_prompt_states_the_limit.
    "uae": "Aim for roughly three quarters UAE/GCC accounts.",
    "global": ("Search globally — the best accounts anywhere in the world, with "
               "no UAE quota."),
}

_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "handle, no @"},
                    "display_name": {"type": "string"},
                    "category": {"type": "string",
                                 "description": "e.g. university, tech community, "
                                                "government, culture, coding school"},
                    "region": {"type": "string",
                               "enum": ["uae", "gcc", "international"]},
                    "why": {"type": "string",
                            "description": "what 42 would learn from them"},
                },
                "required": ["username", "why", "region"],
            },
        },
    },
    "required": ["candidates"],
}


def build_prompt(
    *, tracked: list[str], excluded: list[str], hint: Optional[str], limit: int,
    focus: Focus = DEFAULT_FOCUS,
) -> str:
    """Pure — the tests drive this directly."""
    parts = [f"Suggest up to {limit} Instagram accounts for 42 Abu Dhabi to watch."]
    if hint:
        parts.append(f"The team specifically wants: {hint.strip()[:300]}")
    if tracked:
        parts.append("ALREADY TRACKED — do not suggest these again:\n"
                     + ", ".join("@" + u for u in sorted(tracked)[:80]))
    if excluded:
        parts.append("REJECTED by the team — never suggest these:\n"
                     + ", ".join("@" + u for u in sorted(excluded)[:80]))
    parts.append(_GEO_LINE[focus.geo])
    if focus.relevance == "high":
        parts.append("Only accounts a coding school could borrow a post format "
                     "from — skip lifestyle, tourism, retail and news.")
    if focus.engagement == "high":
        parts.append("Favour accounts with consistently high likes and comments, "
                     "not just a big follower count.")
    if focus.size == "small":
        parts.append("Favour smaller accounts — roughly 5k to 150k followers.")
    elif focus.size == "large":
        parts.append("Favour large, established accounts — 150k followers and up.")
    return "\n\n".join(parts)


async def _validate_one(
    cand: dict, sem: asyncio.Semaphore, *, default_region: str = "uae",
) -> dict:
    """Prove the handle resolves, and enrich it with real profile numbers."""
    try:
        username = store.validate_username(str(cand.get("username") or ""))
    except store.InvalidHandleError as exc:
        return {**cand, "ok": False, "error": str(exc)}

    async with sem:
        try:
            # One post is enough to prove reachability and pull the profile.
            bd = await ig.fetch_account_media(username, page_size=1)
        except ig.InstagramConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 — unreachable is a normal outcome
            return {
                "username": username, "ok": False,
                "display_name": cand.get("display_name"),
                "why": cand.get("why"),
                "error": _explain(exc, username),
            }

    return {
        "username": username,
        "ok": True,
        "display_name": bd.get("name") or cand.get("display_name"),
        "biography": bd.get("biography"),
        "profile_picture_url": bd.get("profile_picture_url"),
        "followers_count": bd.get("followers_count"),
        "media_count": bd.get("media_count"),
        "category": cand.get("category"),
        "region": (cand.get("region") if cand.get("region") in store.REGIONS
                   else default_region),
        "why": cand.get("why"),
    }


def _size_rank(cand: dict, size: str) -> int:
    """0 = matches the requested size band, 1 = doesn't. Used as a STABLE sort
    key so the trim to `limit` keeps what the user asked for WITHOUT ever
    dropping a reachable handle — Business Discovery is the scarce resource
    here, and an empty result reads as a broken button."""
    if size == "any":
        return 0
    followers = cand.get("followers_count")
    if not isinstance(followers, (int, float)):
        return 1
    if size == "small":
        return 0 if followers < 150_000 else 1
    return 0 if followers >= 150_000 else 1


def _explain(exc: Exception, username: str) -> str:
    raw = str(exc)
    if "Invalid user id" in raw or "empty for" in raw:
        return (f"@{username} can't be read — Instagram's Business Discovery only "
                f"reaches Business or Creator accounts.")
    if "rate limit" in raw.lower():
        return "Instagram rate limit reached — try again shortly."
    return raw[:200]


async def research_competitors(
    *, hint: Optional[str] = None, limit: int = 8,
    geo: Optional[str] = None, relevance: Optional[str] = None,
    engagement: Optional[str] = None, size: Optional[str] = None,
) -> dict:
    """Ask the model, then verify. Returns {candidates, rejected, tried}.

    `candidates` are handles that actually resolved and are not already tracked;
    `rejected` explains, per handle, why it couldn't be used — that transparency
    is what stops the button feeling broken when half the list drops out.

    The four focus dials steer WHICH accounts the model proposes; omitting them
    all reproduces the previous UAE-weighted behaviour exactly."""
    limit = max(1, min(int(limit or 8), MAX_CANDIDATES))
    focus = normalise_focus(geo=geo, relevance=relevance,
                            engagement=engagement, size=size)
    existing = await store.list_competitors()
    tracked = [c["username"] for c in existing]
    excluded = [c["username"] for c in existing if c["status"] == "excluded"]

    prompt = build_prompt(tracked=tracked, excluded=excluded, hint=hint,
                          limit=limit + 4,  # ask for extra; validation culls
                          focus=focus)
    try:
        # Budget headroom matters here: 12 candidates each carrying a prose
        # `why` is ~1.2k tokens of JSON, and 2.5-pro spends thinking tokens out
        # of the SAME max_output_tokens pool. At 2500 the JSON was truncated
        # mid-object and came back unparseable — but only above ~7 candidates,
        # which is why small runs looked fine.
        raw = await gemini_text(
            prompt, system=build_system(focus=focus), json_schema=_SCHEMA,
            temperature=0.6, max_tokens=8000,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("[trend-finder] competitor research call failed")
        raise RuntimeError(f"couldn't reach the research model: {str(exc)[:200]}") from exc

    if not isinstance(raw, dict) or raw.get("_raw") is not None:
        raise RuntimeError("the research model returned an unreadable answer — try again")

    known = {u.lower() for u in tracked}
    proposed, seen = [], set()
    for c in (raw.get("candidates") or []):
        if not isinstance(c, dict):
            continue
        u = store.normalise_username(str(c.get("username") or ""))
        if not u or u in known or u in seen:
            continue
        seen.add(u)
        proposed.append(c)

    if not proposed:
        return {"candidates": [], "rejected": [], "tried": 0,
                "note": "no new accounts to suggest — everything it proposed is "
                        "already tracked or previously rejected"}

    sem = asyncio.Semaphore(_VALIDATE_CONCURRENCY)
    default_region = "international" if focus.geo == "global" else "uae"
    results = await asyncio.gather(
        *(_validate_one(c, sem, default_region=default_region)
          for c in proposed[:limit + 4]),
        return_exceptions=True,
    )

    candidates, rejected = [], []
    for res in results:
        if isinstance(res, BaseException):
            log.warning("[trend-finder] candidate validation errored: %s", res)
            continue
        (candidates if res.get("ok") else rejected).append(res)

    # Stable → "any" is a no-op ordering, so the default path is unchanged.
    candidates.sort(key=lambda c: _size_rank(c, focus.size))

    log.info("[trend-finder] research: %d proposed, %d reachable, %d rejected",
             len(proposed), len(candidates), len(rejected))
    return {
        "candidates": candidates[:limit],
        "rejected": rejected,
        "tried": len(proposed),
    }


async def validate_handle(username: str) -> dict:
    """Check one handle the user typed, before it is saved. Raises ValueError
    with a human-readable reason when it can't be watched."""
    handle = store.validate_username(username)
    try:
        bd = await ig.fetch_account_media(handle, page_size=1)
    except ig.InstagramConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_explain(exc, handle)) from exc
    return {
        "username": handle,
        "display_name": bd.get("name"),
        "biography": bd.get("biography"),
        "profile_picture_url": bd.get("profile_picture_url"),
        "followers_count": bd.get("followers_count"),
        "media_count": bd.get("media_count"),
    }
