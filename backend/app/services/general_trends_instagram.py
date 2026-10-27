"""General Trends — the Instagram source (Meta Graph API).

Instagram is the platform the owner actually publishes on, so "what is
Instagram doing this week" is the single most actionable signal this feed can
carry. It is also the most constrained: unauthenticated Instagram is dead
(measured 2026-08-19 from a clean IP — ``web_profile_info`` 429s on the FIRST
request, ``/explore/tags/`` 302s to the login wall, ``topsearch`` 401s, the
legacy oEmbed endpoint is retired), so the official Graph API is the only
honest path and **there is deliberately no scraping fallback here**.

Two lanes, one module, very different economics:

``instagram_watch`` — Business Discovery on a roster of accounts that SET
    trends (meme / tech / UAE-culture / education), not accounts we compete
    with. ONE Graph call per account per refresh, ZERO new scopes beyond what
    the competitor scanner already uses, and it works the instant the token is
    live. A post is ranked against THAT ACCOUNT'S OWN recent median — the same
    reasoning as ``trend_finder.scan_one``: @9gag lands 200k likes on a flop
    and a small dev account lands 300 on a hit, so raw engagement is not
    comparable across accounts and would simply rank the biggest account first.
    This lane is NON-MINTING: one post is not a trend. It corroborates a card
    that another source already minted (and hands it a thumbnail and a real
    engagement number); it never creates one on its own.

``instagram`` — hashtag search. ``ig_hashtag_search`` → ``/{hashtag-id}/
    recent_media``, which Meta caps SERVER-SIDE to the last 24 hours: the
    owner's "nothing older than a month" comes free on this edge. This lane is
    MINTING but stays OFF until ``GENERAL_TRENDS_IG_HASHTAGS`` is populated,
    because it needs ``instagram_manage_insights`` — a scope the current token
    is not confirmed to hold — and because it spends a hard quota.

The quota arithmetic, which is the thing everybody gets wrong:
    the limit is **30 unique hashtag NAMES per IG user per rolling 7 days —
    not 30 calls**. Re-querying a name already inside the window costs nothing.
    So a FIXED roster of <= 25 names can be polled on every refresh, all week
    (28 refreshes x 25 tags = 700 reads against a quota of 30, and it fits),
    while swapping one name mid-window burns a fresh slot that does not come
    back for 7 days. Rotation is therefore a WEEKLY decision. ``ROSTER_MAX``
    is 25 rather than 30 so five slots stay free for ad-hoc lookups. Check
    ``GET /{ig-user-id}/recently_searched_hashtags`` before any roster change —
    it lists exactly which names are still inside the window.

Why one item per HASHTAG and not one per post: emitting a card per post is
precisely the failure that produced "Digital Twins in City Planning — 1
sighting, fit 95" from a single Reddit post. A hashtag with 40 posts in 24h is
a trend; one post carrying it is an anecdote.

The part that actually discovers anything is CO-HASHTAG MINING: every caption
in ``recent_media`` is free text we already paid for, so the hashtags inside it
are counted and the rising co-tags are emitted as their own items
(``source_key = "cotag:<name>"``). Those names are read out of captions and
NEVER queried, so they cost zero quota and zero calls. Without this the source
is a dashboard of 25 tags we already chose; with it, it can surface a tag we
had never heard of.

Token reality (measured live 2026-08-19): the owner's token is EXPIRED —
``OAuthException`` code 190 / subcode 463, "Session has expired on Saturday,
15-Aug-26 07:43:59 PDT". Three consequences this module is built around:

  1. ``instagram_client._get`` surfaces that expiry as an HTTP **400, not a
     401**, so any handler keying on 401 misses it entirely. We key on the
     Graph error CODE, never on the HTTP status.
  2. Expiry is a TOKEN-level fact, not a per-unit one. A module-level circuit
     breaker trips on the first 190 so the remaining units fail instantly
     without a network call — otherwise one dead token burns ~40 doomed
     requests per refresh and eats the ~200/hour budget that the *publish*
     path shares. ``reset_circuit()`` is called once per run (same pattern as
     ``_reddit_next_allowed_at`` in general_trends_sources).
  3. Nothing here may ever crash a run. Every failure is an exception out of a
     fetcher, which ``fetch_all`` records as ``stats["instagram"]["error"]``
     and the run continues to cluster/score without Instagram.

House rules kept: parsers are PURE functions over Graph JSON returning
``list[RawItem]`` and never raise on a malformed entry; fetchers are async over
an INJECTED ``httpx.AsyncClient`` and DO raise. That is also why this module
does not call ``instagram_client.fetch_account_media`` — that helper opens its
own client, which would bypass the pipeline's shared client, its User-Agent and
the monkeypatch seam the tests need. It reuses the client module's conventions
(base URL, credential check, error shape) instead.
"""
from __future__ import annotations

import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Collection, Mapping, Optional, Sequence

import httpx

# NOTE: module-object imports, never `from x import name`. general_trends_
# sources imports THIS module for its dispatch table, so the two form a cycle;
# binding the module object (resolved from sys.modules during a circular
# import) and touching attributes at call time is the shape that survives it.
from . import general_trends_sources as gts
from . import instagram_client as ig

log = logging.getLogger("archivist.general_trends_instagram")

RawItem = dict[str, Any]

# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

SOURCE_HASHTAG = "instagram"
SOURCE_WATCH = "instagram_watch"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_HASHTAG: "Instagram",
    SOURCE_WATCH: "Instagram watch",
}

GLOBAL_REGION = "global"

# Meta's hashtag quota: unique NAMES per IG user per rolling 7 days.
HASHTAG_UNIQUE_QUOTA_PER_7D = 30
# Poll at most this many names so five slots stay free for ad-hoc lookups the
# owner may want from the UI without waiting out a 7-day window.
ROSTER_MAX = 25

# `recent_media` pages at 50; more than that is not worth a second round trip
# for a 24-hour window.
HASHTAG_PAGE_SIZE = 50
WATCH_PAGE_SIZE = 25

# Business Discovery gives likes/comments on everything and view_count on VIDEO
# only (verified in trend_finder). Carousel children are deliberately NOT
# expanded: a trend item needs one thumbnail, not the whole album.
_WATCH_MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,thumbnail_url,"
    "permalink,timestamp,like_count,comments_count,view_count"
)
# Hashtag media is de-identified by Meta: no owner, no follower count, and
# `like_count` is absent when the poster hid likes.
_HASHTAG_MEDIA_FIELDS = (
    "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count"
)

# Below this many posts an account's median is too noisy to call anything an
# outlier, so the ratio is shrunk toward 1.0 AND capped. Same values and same
# reasoning as trend_finder.MIN_POSTS_FOR_BASELINE / THIN_SAMPLE_MAX_CLAIM —
# copied rather than imported, because those are that module's private policy
# for the competitor feed and the two must be free to diverge.
MIN_POSTS_FOR_BASELINE = 6
THIN_SAMPLE_MAX_CLAIM = 3.0
# A post has to beat its own account's median by this much to be worth a
# sighting. Below it, it is a routine post and would only add noise.
MIN_OUTPERFORMANCE = 1.5
# Per account, per refresh. 16 accounts x 3 = 48 items, which is a rounding
# error next to Reddit's old 280 and cannot flood the feed.
MAX_POSTS_PER_ACCOUNT = 3
# Watch posts older than this are not "what Instagram is doing now". The
# freshness module should carry the same number as an override
# (instagram_watch: 336 hours).
WATCH_MAX_AGE_DAYS = 14

# A co-tag has to appear on this many DISTINCT posts before it is a pattern
# rather than one account's posting habit. With a 24h window of <= 50 posts per
# seed tag, 3 is the point where a name stops being an accident.
MIN_COTAG_POSTS = 3
MAX_COTAGS_PER_SEED = 6
MAX_COTAGS_MERGED = 10

TITLE_MAX_CHARS = 120

# Accounts that SET trends, in the four lanes 42 Abu Dhabi can actually ride.
# Business Discovery only reaches Business/Creator accounts — an unreachable
# handle is a NORMAL outcome (recorded per unit, run continues), not a bug, so
# this roster is deliberately over-provisioned and fully overridable through
# GENERAL_TRENDS_IG_WATCH_ACCOUNTS.
DEFAULT_WATCH_ACCOUNTS: tuple[str, ...] = (
    # internet culture / memes — the formats a student audience actually shares
    "9gag", "memezar", "daquan", "sarcasm_only",
    # tech + AI news, where "what everyone is talking about" starts
    "openai", "nvidia", "github", "mkbhd", "techcrunch", "theverge",
    # UAE culture — the local half of the feed
    "visitabudhabi", "lovindubai", "timeoutabudhabi",
    # coding education — nearest neighbours to what the school teaches
    "freecodecamp", "codecademy", "42network",
)

# A STARTING roster for GENERAL_TRENDS_IG_HASHTAGS (20 names, leaving 10 of the
# 30 free rather than the minimum 5). Not applied automatically: the hashtag
# lane must stay opt-in until `instagram_manage_insights` is actually granted,
# and the first roster the owner commits to is locked in for 7 days.
SUGGESTED_HASHTAGS: tuple[str, ...] = (
    "coding", "programming", "codinglife", "learntocode", "softwareengineer",
    "webdevelopment", "ai", "artificialintelligence", "machinelearning", "techtrends",
    "abudhabi", "dubai", "uae", "inabudhabi", "uaestudents",
    "studentlife", "studywithme", "scholarship", "techcareers", "careerchange",
)

# Names that carry a "this is the Gulf" signal, so the item gets region AE and
# the UAE-relevance scorer can see it. Everything else is global.
_AE_MARKERS: tuple[str, ...] = (
    "abudhabi", "abu_dhabi", "dubai", "uae", "emirat", "sharjah", "alain",
    "khaleej", "gulf",
)

# Filler tags: present on a third of all Instagram captions, correlated with
# nothing, and they would out-count every real co-tag. Dropping them is what
# makes co-tag mining a discovery mechanism instead of a popularity contest.
_COTAG_FILLER: frozenset[str] = frozenset({
    "explore", "explorepage", "explorepages", "viral", "viralpost", "viralvideo",
    "trending", "trend", "fyp", "foryou", "foryoupage", "instagood", "instagram",
    "instadaily", "insta", "photooftheday", "picoftheday", "reels", "reel",
    "reelsinstagram", "reelitfeelit", "like", "likes", "likeforlikes", "l4l",
    "follow", "follow4follow", "followme", "f4f", "comment", "share", "repost",
    "love", "life", "happy", "fun", "funny", "meme", "memes", "lol", "tiktok",
    "video", "photography", "photo", "art", "style", "beautiful", "amazing",
    "bhfyp", "instalike", "followforfollowback", "likeforlike", "smile",
})

# Hashtags are squashed words, so the shared safety screen — which matches on
# WORDS and phrases precisely so "top ornament" cannot trip a porn rule — is
# structurally unable to see inside "#pornstar". This substring list is the
# complement to it, not a duplicate of it, and it is deliberately tiny: only
# stems with no innocent English host word. Bare "sex" is EXCLUDED by design
# (sussex/essex/middlesex); "xxx" costs us #xxxtentacion, which is an accepted
# trade for the owner's "no sexual content" rule.
_COTAG_UNSAFE: tuple[str, ...] = (
    "porn", "onlyfans", "nsfw", "xxx", "hentai", "escort", "camgirl",
    "sextape", "nudes", "thirsttrap", "masturbat", "milf", "boobs", "bikinigirl",
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class InstagramSourceUnavailable(RuntimeError):
    """The whole Instagram lane is unusable for this run.

    Distinct from a single unreadable handle: this one trips the circuit so the
    remaining units stop before they spend a request.
    """


class InstagramTokenExpired(InstagramSourceUnavailable):
    """Graph error code 190 — expired, revoked or invalid access token.

    Named separately because ``_err_str`` in general_trends_sources renders the
    exception CLASS NAME into the run card, and "InstagramTokenExpired" is an
    instruction while "HTTP 400 from graph.facebook.com" is noise.
    """


class InstagramQuotaExhausted(InstagramSourceUnavailable):
    """Application/user rate limit (codes 4/17/32/613) — back off for this run."""


class InstagramScopeMissing(InstagramSourceUnavailable):
    """A permission the token does not hold (codes 10/200/3).

    Scoped to the hashtag lane when it is the hashtag edges that were refused:
    Business Discovery keeps working on the scopes we already have.
    """


class InstagramAccountUnreadable(RuntimeError):
    """One handle or tag cannot be read. A normal outcome, NOT a lane failure.

    Business Discovery only reaches Business/Creator accounts, so an
    unresolvable handle is an expected result rather than a bug (the competitor
    scanner learned the same lesson).
    """


class InstagramTransientError(RuntimeError):
    """HTTP 429/5xx with no Graph error code — this unit, this refresh, only."""


# --------------------------------------------------------------------------- #
# Circuit breaker — module state, cleared once per run
# --------------------------------------------------------------------------- #

SCOPE_ALL = "all"
SCOPE_HASHTAG = "hashtag"

# scope -> (exception class, message). Storing the pieces rather than the
# exception instance keeps every re-raise a fresh object with its own traceback.
_circuit: dict[str, tuple[type, str]] = {}

# Hashtag ids are PERMANENT (Meta's own guarantee), so this cache is keyed by
# name only and deliberately survives reset_circuit(): the ids never go stale,
# and a cache hit saves one Graph call per tag per process, not quota.
_hashtag_ids: dict[str, str] = {}


def reset_circuit() -> None:
    """Clear the per-run breaker. Call ONCE at the start of every refresh."""
    _circuit.clear()


def circuit_reason(scope: str = SCOPE_ALL) -> Optional[str]:
    """The message that tripped the breaker for ``scope`` (None when closed)."""
    hit = _circuit.get(SCOPE_ALL) or (_circuit.get(scope) if scope != SCOPE_ALL else None)
    return hit[1] if hit else None


def clear_hashtag_cache() -> None:
    """Forget resolved hashtag ids (tests; not needed in production)."""
    _hashtag_ids.clear()


def _trip(exc: BaseException, *, scope: str = SCOPE_ALL) -> None:
    if scope not in _circuit:
        _circuit[scope] = (type(exc), str(exc))
        log.warning("general_trends: instagram lane disabled for this run (%s): %s",
                    scope, exc)


def _check_circuit(scope: str = SCOPE_ALL) -> None:
    """Raise immediately if this run already proved the lane is dead."""
    for key in (SCOPE_ALL, scope):
        hit = _circuit.get(key)
        if hit:
            cls, msg = hit
            raise cls(msg)


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

def _clean_handle(value: Any) -> str:
    """"@9gag" / " 9gag/ " → "9gag"."""
    s = str(value or "").strip().lstrip("@").strip("/")
    if "instagram.com/" in s:
        s = s.rsplit("instagram.com/", 1)[-1].split("/")[0]
    return s.strip()


def clean_tag(value: Any) -> str:
    """"#Coding Life" → "codinglife". Instagram tags are case-insensitive."""
    s = str(value or "").strip().lstrip("#").strip()
    s = re.sub(r"\s+", "", s)
    return s.lower()


def normalise_roster(values: Sequence[Any], *, limit: int = ROSTER_MAX) -> list[str]:
    """Config strings → a deduped, capped hashtag roster.

    The cap is the quota rule made mechanical: a 40-name roster would not
    "mostly work", it would burn all 30 slots and then fail for 7 days.
    """
    out: list[str] = []
    for v in values or []:
        tag = clean_tag(v)
        if tag and tag not in out:
            out.append(tag)
    if len(out) > limit:
        log.warning("general_trends: instagram roster truncated %d -> %d names "
                    "(Meta allows %d UNIQUE names per 7 days)",
                    len(out), limit, HASHTAG_UNIQUE_QUOTA_PER_7D)
    return out[:limit]


def region_for(name: str) -> str:
    """AE when the name itself carries a Gulf marker, else global."""
    low = str(name or "").lower()
    return "AE" if any(m in low for m in _AE_MARKERS) else GLOBAL_REGION


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Graph timestamps are ``2026-08-19T10:15:33+0000`` — not ISO-parseable
    before 3.11, so try strptime first and fall back for the ``Z`` form."""
    if not raw:
        return None
    s = str(raw).strip()
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def engagement(media: Mapping[str, Any]) -> int:
    """Likes + comments. Views are excluded on purpose: view_count exists on
    VIDEO only, so including it would make the median incomparable across media
    types. On hashtag media ``like_count`` is simply absent when the poster hid
    likes, and this then degrades to a comment count rather than to a crash."""
    return _int(media.get("like_count")) + _int(media.get("comments_count"))


def trusted_outperformance(raw: float, confidence: float) -> float:
    """Damp AND cap a raw engagement ratio by how much baseline we really have.

    Shrinking alone does not help: a 100x computed from three posts is still
    50x after halving, so a freshly-added account would rocket to the top of
    the feed on noise. Pure, so the tests drive it directly.
    """
    conf = max(0.0, min(float(confidence), 1.0))
    damped = 1.0 + (float(raw) - 1.0) * conf
    if conf >= 1.0:
        return round(damped, 3)
    ceiling = 1.0 + conf * (THIN_SAMPLE_MAX_CLAIM - 1.0)
    return round(min(damped, ceiling), 3)


# A '#' straight after a word character or a slash is not a hashtag — it is a
# URL fragment ("example.com/#section") or a stray. Tags may be non-Latin: the
# Gulf half of this feed is full of Arabic hashtags, and \w covers them.
_HASHTAG_RE = re.compile(r"(?<![\w/])#(\w{2,100})", re.UNICODE)


def extract_hashtags(text: Optional[str], *, limit: int = 30) -> list[str]:
    """Hashtag names (no '#', lower-cased, first-seen order, deduped).

    Deduped WITHIN one caption because co-occurrence counts posts, not
    mentions: a poster writing "#coding #coding #coding" is one data point.
    """
    if not text:
        return []
    out: list[str] = []
    for m in _HASHTAG_RE.finditer(str(text)):
        tag = m.group(1).lower()
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= limit:
            break
    return out


def is_useful_cotag(tag: str) -> bool:
    """Filler, pure numbers and squashed-explicit tags are never emitted."""
    t = clean_tag(tag)
    if len(t) < 3 or t in _COTAG_FILLER:
        return False
    if t.isdigit():           # "#2026" is a calendar, not a trend
        return False
    return not any(bad in t for bad in _COTAG_UNSAFE)


def _first_line(text: Optional[str]) -> str:
    for line in str(text or "").splitlines():
        line = gts.collapse_ws(line)
        if line:
            return line
    return ""


def _post_title(caption: Optional[str], *, username: str, media_type: Any) -> str:
    """A human title for one post.

    Prefer the caption's first line with the hashtag tail removed — "POV: your
    code compiles first try" reads as a trend, "#coding #meme #fyp" does not.
    Falls back to the raw first line when stripping tags leaves nothing (many
    meme accounts post tags only), and finally to a synthetic label so the item
    is never dropped by fetch_all's empty-title filter.
    """
    line = _first_line(caption)
    stripped = gts.collapse_ws(_HASHTAG_RE.sub(" ", line))
    if len(stripped) >= 15:
        line = stripped
    if not line:
        kind = str(media_type or "post").replace("_", " ").lower()
        return f"@{username} {kind}"
    return gts.cap_text(line, TITLE_MAX_CHARS)


def tag_url(tag: str) -> str:
    return f"https://www.instagram.com/explore/tags/{clean_tag(tag)}/"


# --------------------------------------------------------------------------- #
# Graph plumbing — the injected client, our own error classification
# --------------------------------------------------------------------------- #

def _creds() -> tuple[str, str]:
    """(token, ig-user-id), converting the client module's config error.

    Converted rather than propagated so a missing token reads as "the Instagram
    source is unavailable" in the run card instead of as an unrelated
    ``InstagramConfigError`` from a module the feed does not otherwise use.
    """
    try:
        return ig._require_creds()
    except ig.InstagramConfigError as exc:
        err = InstagramSourceUnavailable(f"instagram: {exc}")
        _trip(err)
        raise err from exc


def _base_url() -> str:
    return ig._base_url()


def _error_block(response: httpx.Response) -> dict[str, Any]:
    payload = ig._safe_json(response)
    err = payload.get("error") if isinstance(payload, dict) else None
    return err if isinstance(err, dict) else {}


def _expiry_hint(message: str) -> str:
    m = re.search(r"Session has expired on ([^.]+)", message or "")
    return gts.collapse_ws(m.group(1))[:40] if m else ""


def _graph_exception(response: httpx.Response, *, scope: str) -> BaseException:
    """Classify a non-200 Graph response and trip the breaker when it is fatal.

    Keyed on the Graph error CODE, never on the HTTP status: Meta returns the
    expired-token error as an HTTP **400**, so every handler that keys on 401
    silently misses the one failure that actually happens.
    """
    err = _error_block(response)
    code = _int(err.get("code"))
    subcode = _int(err.get("error_subcode"))
    message = gts.collapse_ws(str(err.get("message") or ""))[:300]

    if code == 190:
        when = _expiry_hint(message)
        exc: BaseException = InstagramTokenExpired(
            "instagram: access token expired"
            + (f" ({when})" if when else "")
            + ". Refresh INSTAGRAM_ACCESS_TOKEN — exchange with "
            "grant_type=fb_exchange_token, then take the non-expiring Page "
            f"token from /me/accounts. Graph error 190/{subcode or 0}."
        )
        _trip(exc)
        return exc

    if code in (4, 17, 32, 613):
        exc = InstagramQuotaExhausted(
            f"instagram: Graph rate limit reached (code {code}) — the lane is "
            "paused for this run so the publish path keeps its budget."
        )
        _trip(exc)
        return exc

    if code in (3, 10, 200):
        exc = InstagramScopeMissing(
            f"instagram: the token lacks a permission for this call (code {code}). "
            "The hashtag lane needs instagram_manage_insights on a Business "
            f"account. {message}"
        )
        _trip(exc, scope=scope)
        return exc

    if response.status_code == 429 or response.status_code >= 500:
        # A throttle or a Graph hiccup with no error code of its own. One unit
        # loses one refresh; retrying inside the fetcher would only spend more
        # of the budget the publish path shares, and the next run is 6h away.
        return InstagramTransientError(
            f"instagram: HTTP {response.status_code} from the Graph API"
            + (f": {message}" if message else "")
        )

    # Everything else (100/33 unknown object, 803 alias, malformed query) is a
    # per-unit fact: this handle or tag is unreadable, the lane is fine.
    return InstagramAccountUnreadable(
        f"instagram: Graph error {code or response.status_code}"
        + (f"/{subcode}" if subcode else "")
        + (f": {message}" if message else "")
    )


async def _graph_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
    *,
    scope: str = SCOPE_ALL,
) -> dict[str, Any]:
    """One Graph GET over the INJECTED client. Raises on anything but 200.

    The access token goes in the query string and must never reach a log line
    or an exception message — only Meta's own error text is ever quoted.
    """
    token, _acct = _creds()
    _check_circuit(scope)
    r = await client.get(f"{_base_url()}{path}", params={**params, "access_token": token})
    if r.status_code == 200:
        data = ig._safe_json(r)
        return data if isinstance(data, dict) else {}
    raise _graph_exception(r, scope=scope)


# --------------------------------------------------------------------------- #
# Parsers — pure, never raise on a malformed entry
# --------------------------------------------------------------------------- #

def _media_list(payload: Any) -> list[dict]:
    """The ``media.data`` array out of either a full response or a bd block."""
    if not isinstance(payload, dict):
        return []
    block = payload.get("business_discovery")
    if isinstance(block, dict):
        payload = block
    media = payload.get("media")
    if isinstance(media, dict):
        data = media.get("data")
    else:
        data = payload.get("data")
    return [m for m in (data or []) if isinstance(m, dict)]


def parse_watch_media(
    payload: Any,
    *,
    username: str,
    now: Optional[datetime] = None,
    max_age_days: int = WATCH_MAX_AGE_DAYS,
    min_outperformance: float = MIN_OUTPERFORMANCE,
    max_items: int = MAX_POSTS_PER_ACCOUNT,
    region: Optional[str] = None,
) -> list[RawItem]:
    """Business Discovery payload → the posts that OUTPERFORMED this account.

    The median is taken over the whole returned page BEFORE the age filter, so
    a quiet fortnight cannot shrink the baseline and turn ordinary posts into
    outliers. Ranking, damping and capping mirror ``trend_finder.scan_one``.
    """
    username = _clean_handle(username) or "unknown"
    now = now or datetime.now(timezone.utc)
    reg = region or region_for(username)
    media = _media_list(payload)
    if not media:
        return []

    rows: list[dict[str, Any]] = []
    for m in media:
        try:
            mid = str(m.get("id") or "").strip()
            if not mid:
                continue
            rows.append({
                "media": m,
                "id": mid,
                "eng": engagement(m),
                "ts": _parse_ts(m.get("timestamp")),
            })
        except Exception as e:  # pragma: no cover — defensive, per house rule
            log.debug("instagram_watch: skipped malformed media: %s", e)

    if not rows:
        return []

    engagements = [r["eng"] for r in rows]
    median = float(statistics.median(engagements)) if engagements else 0.0
    baseline = median if median > 0 else 1.0
    confidence = min(len(rows) / MIN_POSTS_FOR_BASELINE, 1.0)

    account = payload.get("business_discovery") if isinstance(payload, dict) else None
    account = account if isinstance(account, dict) else (payload if isinstance(payload, dict) else {})
    followers = _int(account.get("followers_count"))

    cutoff = now - timedelta(days=max(0, int(max_age_days)))
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        ts = r["ts"]
        if ts is not None and ts < cutoff:
            continue
        if r["eng"] <= 0:
            continue
        over = trusted_outperformance(r["eng"] / baseline, confidence)
        if over < min_outperformance:
            continue
        scored.append((over, r))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    out: list[RawItem] = []
    for over, r in scored[: max(0, int(max_items))]:
        m = r["media"]
        caption = str(m.get("caption") or "")
        tags = extract_hashtags(caption, limit=12)
        out.append(gts.make_item(
            source=SOURCE_WATCH,
            source_key=f"ig:{r['id']}",
            title=_post_title(caption, username=username, media_type=m.get("media_type")),
            url=str(m.get("permalink") or "") or None,
            summary=caption,
            image_url=(m.get("thumbnail_url") or m.get("media_url") or None),
            # Outperformance, NOT raw engagement: the ranking layer divides a
            # metric by that source's median, and raw likes across accounts of
            # wildly different sizes would simply re-rank the feed by follower
            # count — the exact mistake trend_finder exists to avoid.
            metric_value=over,
            metric_label="x the account's usual",
            published_at=_iso(r["ts"]) if r["ts"] else None,
            region=reg,
            category="instagram_post",
            extra={
                "username": username,
                "outperformance": over,
                "engagement": r["eng"],
                "like_count": _int(m.get("like_count")),
                "comments_count": _int(m.get("comments_count")),
                # VIDEO only — never invented for images or carousels.
                "view_count": _int(m["view_count"]) if m.get("view_count") is not None else None,
                "engagement_rate": round(r["eng"] / followers, 6) if followers else None,
                "media_type": m.get("media_type"),
                "media_product_type": m.get("media_product_type"),
                "permalink": m.get("permalink"),
                "hashtags": tags,
                "baseline_median": median,
                "baseline_sample": len(rows),
                "baseline_confidence": round(confidence, 2),
                "followers_count": followers or None,
            },
        ))
    return out


def parse_hashtag_media(
    payload: Any,
    *,
    tag: str,
    now: Optional[datetime] = None,
    max_age_days: Optional[float] = None,
    region: Optional[str] = None,
    window_label: str = "24h",
) -> dict[str, Any]:
    """recent_media (or top_media) payload → ONE hashtag item + co-tag stats.

    Returns ``{"item": RawItem|None, "posts": int, "latest": iso|None,
    "cotags": {name: {...}}}``. The co-tag stats are handed back raw rather
    than emitted here so a multi-tag fetch can MERGE them across seeds — a name
    that shows up under three different seed tags is a much stronger signal
    than the same count under one.
    """
    tag = clean_tag(tag)
    now = now or datetime.now(timezone.utc)
    reg = region or region_for(tag)
    empty: dict[str, Any] = {"item": None, "posts": 0, "latest": None, "cotags": {}}
    if not tag:
        return empty

    cutoff = (now - timedelta(days=float(max_age_days))) if max_age_days else None
    posts: list[dict[str, Any]] = []
    cotags: dict[str, dict[str, Any]] = {}

    for m in _media_list(payload):
        try:
            mid = str(m.get("id") or "").strip()
            ts = _parse_ts(m.get("timestamp"))
            if cutoff is not None and ts is not None and ts < cutoff:
                continue
            caption = str(m.get("caption") or "")
            eng = engagement(m)
            posts.append({
                "id": mid,
                "ts": ts,
                "eng": eng,
                "caption": caption,
                "permalink": m.get("permalink"),
                "image": m.get("media_url"),
            })
            for name in extract_hashtags(caption):
                if name == tag or not is_useful_cotag(name):
                    continue
                stat = cotags.setdefault(name, {
                    "tag": name, "post_ids": [], "seeds": [], "latest": None,
                    "examples": [],
                })
                key = mid or (str(m.get("permalink") or "") + caption[:40])
                if key not in stat["post_ids"]:
                    stat["post_ids"].append(key)
                if tag not in stat["seeds"]:
                    stat["seeds"].append(tag)
                if ts is not None and (stat["latest"] is None or _iso(ts) > stat["latest"]):
                    stat["latest"] = _iso(ts)
                link = m.get("permalink")
                if link and len(stat["examples"]) < 3:
                    stat["examples"].append(str(link))
        except Exception as e:  # pragma: no cover — defensive, per house rule
            log.debug("instagram: skipped malformed hashtag media: %s", e)

    if not posts:
        return empty

    dated = [p["ts"] for p in posts if p["ts"] is not None]
    latest = _iso(max(dated)) if dated else None
    engs = [p["eng"] for p in posts]
    # Median, not mean: one viral post inside a 24h window would otherwise
    # define the tag's whole score. And the MEDIAN is the metric rather than
    # the post COUNT because the count saturates at the page size (50) and
    # would read identically for a busy tag and a dead one.
    median_eng = float(statistics.median(engs)) if engs else 0.0
    top = max(posts, key=lambda p: (p["eng"], p["id"]))

    item = gts.make_item(
        source=SOURCE_HASHTAG,
        source_key=f"tag:{tag}",
        title=f"#{tag}",
        url=tag_url(tag),
        summary=_first_line(top["caption"]),
        image_url=top.get("image") or None,
        metric_value=median_eng,
        metric_label=f"median engagement/{window_label}",
        published_at=latest,
        region=reg,
        category="instagram_hashtag",
        extra={
            "hashtag": tag,
            "posts": len(posts),
            "window": window_label,
            "median_engagement": median_eng,
            "top_engagement": top["eng"],
            "examples": [
                {
                    "permalink": p.get("permalink"),
                    "engagement": p["eng"],
                    "thumb": p.get("image"),
                }
                for p in sorted(posts, key=lambda p: -p["eng"])[:3]
            ],
            "co_tags": [
                [name, len(stat["post_ids"])]
                for name, stat in sorted(
                    cotags.items(), key=lambda kv: (-len(kv[1]["post_ids"]), kv[0]))
            ][:MAX_COTAGS_MERGED],
        },
    )
    return {"item": item, "posts": len(posts), "latest": latest, "cotags": cotags}


def merge_cotags(
    into: dict[str, dict[str, Any]],
    more: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Fold one seed's co-tag stats into the running total.

    Post ids are unioned, not added: the same post surfaces under every seed
    tag it carries, and counting it twice would manufacture a trend out of one
    prolific account.
    """
    for name, stat in (more or {}).items():
        cur = into.setdefault(name, {
            "tag": name, "post_ids": [], "seeds": [], "latest": None, "examples": [],
        })
        for pid in stat.get("post_ids") or []:
            if pid not in cur["post_ids"]:
                cur["post_ids"].append(pid)
        for seed in stat.get("seeds") or []:
            if seed not in cur["seeds"]:
                cur["seeds"].append(seed)
        for ex in stat.get("examples") or []:
            if ex not in cur["examples"] and len(cur["examples"]) < 3:
                cur["examples"].append(ex)
        latest = stat.get("latest")
        if latest and (cur["latest"] is None or latest > cur["latest"]):
            cur["latest"] = latest
    return into


def build_cotag_items(
    cotags: Mapping[str, dict[str, Any]],
    *,
    sample_size: int,
    min_posts: int = MIN_COTAG_POSTS,
    limit: int = MAX_COTAGS_MERGED,
    exclude: Collection[str] = (),
    window_label: str = "24h",
) -> list[RawItem]:
    """Rising co-tags → RawItems. Zero quota: these names are never queried.

    ``exclude`` is normally the seed roster: a roster tag already has its own,
    better item, and emitting it again as a co-tag would let the source
    corroborate itself.
    """
    skip = {clean_tag(x) for x in (exclude or ())}
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for name, stat in (cotags or {}).items():
        if name in skip or not is_useful_cotag(name):
            continue
        n = len(stat.get("post_ids") or [])
        if n < max(1, int(min_posts)):
            continue
        ranked.append((n, name, stat))
    ranked.sort(key=lambda t: (-t[0], t[1]))

    out: list[RawItem] = []
    for n, name, stat in ranked[: max(0, int(limit))]:
        seeds = [str(s) for s in (stat.get("seeds") or [])][:4]
        with_what = ", ".join(f"#{s}" for s in seeds) or "the roster"
        out.append(gts.make_item(
            source=SOURCE_HASHTAG,
            source_key=f"cotag:{name}",
            title=f"#{name}",
            url=tag_url(name),
            summary=(f"Co-tagged with {with_what} on {n} of {sample_size} Instagram "
                     f"posts in the last {window_label}."),
            image_url=None,
            metric_value=float(n),
            metric_label=f"co-mentions/{window_label}",
            published_at=stat.get("latest"),
            region=region_for(name),
            category="instagram_cotag",
            extra={
                "hashtag": name,
                "seed_tags": seeds,
                "posts": n,
                "sample_size": sample_size,
                "share": round(n / sample_size, 4) if sample_size else None,
                "examples": list(stat.get("examples") or [])[:3],
                "window": window_label,
            },
        ))
    return out


# --------------------------------------------------------------------------- #
# Fetchers — async over an injected client, DO raise
# --------------------------------------------------------------------------- #

async def fetch_instagram_watch(
    client: httpx.AsyncClient,
    *,
    username: str,
    page_size: int = WATCH_PAGE_SIZE,
    max_age_days: int = WATCH_MAX_AGE_DAYS,
    min_outperformance: float = MIN_OUTPERFORMANCE,
    max_items: int = MAX_POSTS_PER_ACCOUNT,
    now: Optional[datetime] = None,
) -> list[RawItem]:
    """One watch account → its overperforming recent posts. ONE Graph call.

    Uses only ``business_discovery``, i.e. exactly the scopes the competitor
    scanner already relies on: this lane starts working the moment the token is
    refreshed, with no Meta app review at all.
    """
    _check_circuit()
    handle = _clean_handle(username)
    if not handle:
        raise ValueError("instagram_watch: empty username")
    _token, acct_id = _creds()

    fields = (
        f"business_discovery.username({handle})"
        "{followers_count,media_count,username,"
        f"media.limit({max(1, int(page_size))}){{{_WATCH_MEDIA_FIELDS}}}}}"
    )
    data = await _graph_get(client, f"/{acct_id}", {"fields": fields})
    bd = data.get("business_discovery")
    if not isinstance(bd, dict):
        # Meta returns an empty body rather than an error for a non-Business /
        # non-discoverable handle. Normal outcome, reported per unit.
        raise InstagramAccountUnreadable(
            f"instagram_watch: @{handle} is not readable through Business "
            "Discovery (Business/Creator accounts only)."
        )
    return parse_watch_media(
        bd, username=handle, now=now, max_age_days=max_age_days,
        min_outperformance=min_outperformance, max_items=max_items,
    )


async def resolve_hashtag_id(client: httpx.AsyncClient, tag: str) -> str:
    """Hashtag name → its permanent Graph id, cached for the process.

    Ids never change, so this is a pure saving of one call per tag per process.
    It does NOT save quota: the 30-per-7-days budget is spent by *querying a
    name*, and re-querying a name already inside the window is free anyway.
    """
    name = clean_tag(tag)
    if not name:
        raise ValueError("instagram: empty hashtag")
    cached = _hashtag_ids.get(name)
    if cached:
        return cached
    _check_circuit(SCOPE_HASHTAG)
    _token, acct_id = _creds()
    data = await _graph_get(
        client, "/ig_hashtag_search",
        {"user_id": acct_id, "q": name},
        scope=SCOPE_HASHTAG,
    )
    rows = data.get("data") if isinstance(data, dict) else None
    hid = ""
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        hid = str(rows[0].get("id") or "").strip()
    if not hid:
        raise InstagramAccountUnreadable(f"instagram: no hashtag id for #{name}")
    _hashtag_ids[name] = hid
    return hid


async def _fetch_hashtag_parsed(
    client: httpx.AsyncClient,
    *,
    tag: str,
    limit: int,
    mode: str,
    region: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One tag's media, already parsed. Shared by the single- and multi-tag
    fetchers so the edge choice and the recency rule live in ONE place."""
    _check_circuit(SCOPE_HASHTAG)
    name = clean_tag(tag)
    if not name:
        raise ValueError("instagram: empty hashtag")
    _token, acct_id = _creds()
    hid = await resolve_hashtag_id(client, name)

    edge = "top_media" if str(mode).lower() == "top" else "recent_media"
    data = await _graph_get(
        client, f"/{hid}/{edge}",
        {
            "user_id": acct_id,
            "fields": _HASHTAG_MEDIA_FIELDS,
            "limit": max(1, int(limit)),
        },
        scope=SCOPE_HASHTAG,
    )
    window = "24h" if edge == "recent_media" else "top"
    parsed = parse_hashtag_media(
        data, tag=name, now=now, region=region, window_label=window,
        # top_media carries no recency guarantee, so it gets the feed's ceiling.
        max_age_days=None if edge == "recent_media" else 30,
    )
    parsed["tag"] = name
    parsed["window"] = window
    return parsed


async def fetch_instagram_hashtag(
    client: httpx.AsyncClient,
    *,
    tag: str,
    limit: int = HASHTAG_PAGE_SIZE,
    mode: str = "recent",
    region: Optional[str] = None,
    emit_cotags: bool = True,
    min_cotag_posts: int = MIN_COTAG_POSTS,
    max_cotags: int = MAX_COTAGS_PER_SEED,
    exclude_cotags: Collection[str] = (),
    now: Optional[datetime] = None,
) -> list[RawItem]:
    """One hashtag → ONE item for the tag, plus its rising co-tags.

    ``mode="recent"`` hits ``/recent_media``, which Meta caps to the last 24
    hours server-side — that is the recency guarantee, for free. ``"top"`` hits
    ``/top_media`` (50 most popular, no recency guarantee at all), so that path
    filters on ``timestamp`` client-side before anything is aggregated.
    """
    parsed = await _fetch_hashtag_parsed(
        client, tag=tag, limit=limit, mode=mode, region=region, now=now)
    name = parsed["tag"]
    window = parsed["window"]
    out: list[RawItem] = []
    if parsed["item"] is not None:
        out.append(parsed["item"])
    if emit_cotags:
        out.extend(build_cotag_items(
            parsed["cotags"],
            sample_size=parsed["posts"],
            min_posts=min_cotag_posts,
            limit=max_cotags,
            exclude=list(exclude_cotags) + [name],
            window_label=window,
        ))
    return out


async def fetch_instagram_hashtags(
    client: httpx.AsyncClient,
    *,
    tags: Sequence[str],
    limit: int = HASHTAG_PAGE_SIZE,
    mode: str = "recent",
    min_cotag_posts: int = MIN_COTAG_POSTS,
    max_cotags: int = MAX_COTAGS_MERGED,
    now: Optional[datetime] = None,
) -> list[RawItem]:
    """The whole roster in one unit: per-tag items + co-tags merged across seeds.

    Serial on purpose. The ~200 calls/hour budget is shared with the publish
    path, and a roster of 25 tags is 25 calls; firing them concurrently buys
    seconds and risks a code-4 throttle that would take the publish step down
    with it.

    A single unreadable tag is swallowed (it is one name out of 25); anything
    that trips the breaker — expired token, quota, missing scope — aborts the
    rest of the roster immediately, which is the entire point of the breaker.
    """
    roster = normalise_roster(tags)
    if not roster:
        return []
    items: list[RawItem] = []
    cotags: dict[str, dict[str, Any]] = {}
    sample = 0
    failures: list[str] = []

    for name in roster:
        try:
            parsed = await _fetch_hashtag_parsed(
                client, tag=name, limit=limit, mode=mode, now=now)
            if parsed["item"] is not None:
                items.append(parsed["item"])
            sample += parsed["posts"]
            merge_cotags(cotags, parsed["cotags"])
        except InstagramSourceUnavailable:
            raise
        except (InstagramAccountUnreadable, InstagramTransientError,
                ValueError, httpx.HTTPError) as exc:
            failures.append(f"#{name}: {exc}")
            log.info("general_trends: instagram tag #%s skipped: %s", name, exc)

    items.extend(build_cotag_items(
        cotags, sample_size=sample, min_posts=min_cotag_posts,
        limit=max_cotags, exclude=roster,
        window_label="24h" if str(mode).lower() != "top" else "top",
    ))
    if failures and not items and len(failures) == len(roster):
        # EVERY tag failed: that is a source failure worth showing. A quiet
        # roster that simply produced nothing is not.
        raise InstagramAccountUnreadable("instagram: " + "; ".join(failures)[:280])
    return items


async def fetch_instagram_watch_accounts(
    client: httpx.AsyncClient,
    *,
    usernames: Sequence[str],
    **kwargs: Any,
) -> list[RawItem]:
    """Convenience for a single-unit watch lane. Per-account failures are kept
    as failures only when EVERY account failed — one unreadable handle out of
    sixteen is a normal Business Discovery outcome, not a source outage."""
    handles = [h for h in (_clean_handle(u) for u in (usernames or [])) if h]
    if not handles:
        return []
    out: list[RawItem] = []
    failures: list[str] = []
    for handle in handles:
        try:
            out.extend(await fetch_instagram_watch(client, username=handle, **kwargs))
        except InstagramSourceUnavailable:
            raise
        except (InstagramAccountUnreadable, InstagramTransientError,
                ValueError, httpx.HTTPError) as exc:
            failures.append(f"@{handle}: {exc}")
            log.info("general_trends: instagram watch @%s skipped: %s", handle, exc)
    if failures and not out and len(failures) == len(handles):
        raise InstagramAccountUnreadable("instagram_watch: " + "; ".join(failures)[:280])
    return out


__all__ = [
    "SOURCE_HASHTAG", "SOURCE_WATCH", "SOURCE_LABELS",
    "DEFAULT_WATCH_ACCOUNTS", "SUGGESTED_HASHTAGS",
    "HASHTAG_UNIQUE_QUOTA_PER_7D", "ROSTER_MAX",
    "InstagramSourceUnavailable", "InstagramTokenExpired",
    "InstagramQuotaExhausted", "InstagramScopeMissing", "InstagramAccountUnreadable",
    "InstagramTransientError",
    "reset_circuit", "circuit_reason", "clear_hashtag_cache",
    "SCOPE_ALL", "SCOPE_HASHTAG",
    "clean_tag", "normalise_roster", "region_for", "tag_url",
    "extract_hashtags", "is_useful_cotag", "engagement", "trusted_outperformance",
    "parse_watch_media", "parse_hashtag_media", "merge_cotags", "build_cotag_items",
    "resolve_hashtag_id", "fetch_instagram_hashtag", "fetch_instagram_hashtags",
    "fetch_instagram_watch", "fetch_instagram_watch_accounts",
]
