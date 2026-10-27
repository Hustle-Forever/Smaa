"""Tests for General Trends — the Instagram source.

Module under test: ``app/services/general_trends_instagram.py``.

Everything is offline, and it has to be: the owner's Graph token is EXPIRED
(``OAuthException`` 190/463, "Session has expired on Saturday, 15-Aug-26
07:43:59 PDT", confirmed live 2026-08-19), so a network test would prove
nothing but the expiry. The payloads below are trimmed to the shape Meta
actually returns — the ``business_discovery`` wrapper, ``media.data``,
``+0000`` timestamps that ``datetime.fromisoformat`` cannot read before 3.11,
hashtag media WITHOUT ``like_count`` (the poster hid likes), and the real error
envelope.

The behaviours pinned here are the ones the feature dies without:
  * ONE item per hashtag, never one per post (per-post emission is what made
    "Digital Twins in City Planning — 1 sighting, fit 95");
  * co-tag mining counts DISTINCT POSTS and never double-counts a post that
    carries two seed tags;
  * a post is ranked against ITS OWN account's median, damped and capped on a
    thin sample;
  * the circuit breaker opens on the first Graph 190 and spends no further
    request until ``reset_circuit()``;
  * expiry raises ``InstagramTokenExpired``, not a bare RuntimeError, and the
    message carries the refresh recipe rather than a raw OAuth blob.

No pytest-asyncio in this venv: coroutines are driven with ``asyncio.run`` via
``_run`` (same as tests/test_general_trends_sources.py).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import general_trends_instagram as gti  # noqa: E402
from app.services import general_trends_sources as gts  # noqa: E402
from app.services import instagram_client as ig  # noqa: E402


NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

RAW_KEYS = {
    "source", "source_key", "title", "url", "summary", "image_url", "metric_value",
    "metric_label", "published_at", "region", "category", "extra",
}


def _run(coro):
    return asyncio.run(coro)


def _assert_raw_item(it: dict) -> None:
    assert set(it.keys()) == RAW_KEYS, it.keys()
    assert it["source"] in (gti.SOURCE_HASHTAG, gti.SOURCE_WATCH)
    assert isinstance(it["source_key"], str) and it["source_key"]
    assert isinstance(it["title"], str) and it["title"]
    assert isinstance(it["extra"], dict)


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Circuit + hashtag-id cache are module state; no test may inherit either.
    Credentials are faked at the client module's own seam so nothing in here
    depends on backend/.env."""
    gti.reset_circuit()
    gti.clear_hashtag_cache()
    monkeypatch.setattr(ig, "_require_creds", lambda: ("TOKEN-SECRET", "17841400000000000"))
    yield
    gti.reset_circuit()
    gti.clear_hashtag_cache()


# --------------------------------------------------------------------------- #
# Fake transport
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)[:500]

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient — records every call."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None):
        self.calls.append((str(url), dict(params or {})))
        res = self._handler(str(url), dict(params or {}))
        if isinstance(res, tuple):
            status, payload = res
            return _FakeResponse(status, payload)
        return _FakeResponse(200, res)


def _graph_error(code: int, message: str, subcode: int | None = None):
    err = {"message": message, "type": "OAuthException", "code": code,
           "fbtrace_id": "AbCdEf"}
    if subcode is not None:
        err["error_subcode"] = subcode
    return 400, {"error": err}


EXPIRED_TOKEN_ERROR = _graph_error(
    190,
    "Error validating access token: Session has expired on Saturday, "
    "15-Aug-26 07:43:59 PDT. The current time is Wednesday, 19-Aug-26 "
    "13:05:57 PDT.",
    subcode=463,
)


# --------------------------------------------------------------------------- #
# Fixtures — trimmed real-shaped Graph payloads
# --------------------------------------------------------------------------- #

def _post(mid: str, *, likes: int, comments: int = 0, hours_ago: float = 5.0,
          caption: str = "", media_type: str = "VIDEO", views: int | None = None) -> dict:
    ts = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    m = {
        "id": mid,
        "caption": caption,
        "media_type": media_type,
        "media_product_type": "REELS" if media_type == "VIDEO" else "FEED",
        "media_url": f"https://cdn.example/{mid}.mp4",
        "thumbnail_url": f"https://cdn.example/{mid}.jpg",
        "permalink": f"https://www.instagram.com/reel/{mid}/",
        "timestamp": ts,
        "like_count": likes,
        "comments_count": comments,
    }
    if views is not None:
        m["view_count"] = views
    return m


def _bd(username: str, media: list[dict], *, followers: int = 1_200_000) -> dict:
    """The exact envelope fetch_account_media returns from the Graph."""
    return {
        "business_discovery": {
            "followers_count": followers,
            "media_count": 5432,
            "username": username,
            "media": {
                "data": media,
                "paging": {"cursors": {"after": "QVFIU..."}},
            },
        },
        "id": "17841400000000000",
    }


def _hashtag_media(media: list[dict]) -> dict:
    return {"data": media, "paging": {"cursors": {"after": "QVFIU..."}}}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_extract_hashtags_dedupes_folds_case_and_ignores_url_fragments():
    caption = (
        "POV: your code compiles first try 😭\n"
        "#Coding #coding #CODING #برمجة #uae_students\n"
        "see https://example.com/page#section and c#"
    )
    tags = gti.extract_hashtags(caption)
    # One data point per post: three spellings of the same tag collapse to one.
    assert tags == ["coding", "برمجة", "uae_students"]
    # "#section" sits behind a '/', "c#" behind a word char — neither is a tag.
    assert "section" not in tags


def test_extract_hashtags_is_empty_on_junk():
    assert gti.extract_hashtags(None) == []
    assert gti.extract_hashtags("") == []
    assert gti.extract_hashtags("no tags here at all") == []


def test_is_useful_cotag_drops_filler_but_keeps_scunthorpe_cases():
    assert not gti.is_useful_cotag("fyp")
    assert not gti.is_useful_cotag("explorepage")
    assert not gti.is_useful_cotag("2026")          # a calendar, not a trend
    assert not gti.is_useful_cotag("pornstar")      # squashed — the word-based
    assert not gti.is_useful_cotag("onlyfansgirl")  # safety screen cannot see it
    # Bare "sex" is deliberately NOT in the list: these must survive.
    assert gti.is_useful_cotag("sussex")
    assert gti.is_useful_cotag("essex")
    assert gti.is_useful_cotag("codingbootcamp")


def test_normalise_roster_caps_at_the_quota_headroom():
    roster = gti.normalise_roster([f"#Tag{i}" for i in range(40)])
    assert len(roster) == gti.ROSTER_MAX == 25
    assert roster[0] == "tag0"
    # Dedupe is case-insensitive: Instagram hashtags are.
    assert gti.normalise_roster(["#Coding", "coding", " CODING "]) == ["coding"]


def test_region_marks_gulf_names_only():
    assert gti.region_for("inabudhabi") == "AE"
    assert gti.region_for("visitabudhabi") == "AE"
    assert gti.region_for("uaestudents") == "AE"
    assert gti.region_for("coding") == "global"


def test_trusted_outperformance_damps_and_caps_a_thin_sample():
    # Full baseline: the ratio is reported as measured.
    assert gti.trusted_outperformance(4.7, 1.0) == 4.7
    # Three posts of 1, 1, 100 -> raw 100x. Damping alone would still claim 50x,
    # so the claim is capped instead.
    assert gti.trusted_outperformance(100.0, 0.5) == 2.0


# --------------------------------------------------------------------------- #
# Watch accounts — per-account median ranking
# --------------------------------------------------------------------------- #

def test_watch_ranks_against_the_accounts_own_median():
    media = [_post(f"1{i}", likes=100, comments=0, caption="routine post") for i in range(6)]
    media.append(_post("hit", likes=900, comments=100,
                       caption="POV: your code compiles first try 😭\n#coding #devlife",
                       views=1_200_000))
    items = gti.parse_watch_media(_bd("9gag", media), username="9gag", now=NOW)

    # Only the outlier survives: six routine posts sit AT the median (1.0x).
    assert len(items) == 1
    it = items[0]
    _assert_raw_item(it)
    assert it["source"] == gti.SOURCE_WATCH
    assert it["source_key"] == "ig:hit"
    assert it["title"] == "POV: your code compiles first try 😭"
    # metric_value is the outperformance, not raw likes: 1000 / median 100.
    assert it["metric_value"] == 10.0
    assert it["metric_label"] == "x the account's usual"
    assert it["extra"]["engagement"] == 1000
    assert it["extra"]["baseline_median"] == 100.0
    assert it["extra"]["baseline_sample"] == 7
    assert it["extra"]["view_count"] == 1_200_000
    assert it["extra"]["hashtags"] == ["coding", "devlife"]
    assert it["published_at"].startswith("2026-08-19T07:00")


def test_watch_thin_sample_cannot_manufacture_a_headline():
    media = [_post("a", likes=1), _post("b", likes=1), _post("c", likes=100)]
    items = gti.parse_watch_media(_bd("tiny", media, followers=0), username="tiny", now=NOW)
    assert len(items) == 1
    # raw 100x, confidence 3/6 = 0.5 -> capped at 1 + 0.5*(3-1) = 2.0
    assert items[0]["metric_value"] == 2.0
    assert items[0]["extra"]["baseline_confidence"] == 0.5
    assert items[0]["extra"]["engagement_rate"] is None  # no follower count


def test_watch_drops_stale_posts_but_keeps_them_in_the_baseline():
    media = [_post(f"1{i}", likes=100) for i in range(6)]
    media.append(_post("old", likes=5000, hours_ago=24 * 40, caption="last month's hit"))
    items = gti.parse_watch_media(_bd("9gag", media), username="9gag", now=NOW,
                                  max_age_days=gti.WATCH_MAX_AGE_DAYS)
    assert items == []
    # The stale post still counted toward the median (7 posts), which is what
    # stops a quiet fortnight from turning routine posts into outliers.
    items2 = gti.parse_watch_media(_bd("9gag", media), username="9gag", now=NOW,
                                   max_age_days=90)
    assert len(items2) == 1
    assert items2[0]["extra"]["baseline_sample"] == 7


def test_watch_caps_items_per_account_and_sorts_by_outperformance():
    media = [_post(f"1{i}", likes=100) for i in range(6)]
    media += [_post("x1", likes=400), _post("x2", likes=900), _post("x3", likes=600),
              _post("x4", likes=300)]
    items = gti.parse_watch_media(_bd("9gag", media), username="9gag", now=NOW)
    assert [it["source_key"] for it in items] == ["ig:x2", "ig:x3", "ig:x1"]
    assert len(items) == gti.MAX_POSTS_PER_ACCOUNT == 3


def test_watch_titles_fall_back_when_the_caption_is_only_hashtags():
    media = [_post(f"1{i}", likes=100) for i in range(6)]
    media.append(_post("tagsonly", likes=1000, caption="#coding #meme"))
    media.append(_post("nocap", likes=1000, caption="", media_type="IMAGE"))
    items = gti.parse_watch_media(_bd("9gag", media), username="9gag", now=NOW, max_items=5)
    titles = {it["source_key"]: it["title"] for it in items}
    assert titles["ig:tagsonly"] == "#coding #meme"   # stripping would empty it
    assert titles["ig:nocap"] == "@9gag image"        # never an empty title


def test_watch_parser_never_raises_on_junk():
    assert gti.parse_watch_media(None, username="x") == []
    assert gti.parse_watch_media({}, username="x") == []
    assert gti.parse_watch_media({"business_discovery": {}}, username="x") == []
    assert gti.parse_watch_media(
        {"business_discovery": {"media": {"data": [{"no": "id"}, 7, None]}}},
        username="x") == []


# --------------------------------------------------------------------------- #
# Hashtags — one item per tag, never one per post
# --------------------------------------------------------------------------- #

def _tag_payload() -> dict:
    return _hashtag_media([
        # like_count is absent here: the poster hid likes. Must not crash.
        {"id": "p1", "caption": "day 12 of #100daysofcode #coding #fyp",
         "media_type": "IMAGE", "media_url": "https://cdn.example/p1.jpg",
         "permalink": "https://www.instagram.com/p/p1/",
         "timestamp": (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
         "comments_count": 4},
        _post("p2", likes=300, comments=20, hours_ago=6,
              caption="my #100daysofcode setup #coding #desksetup"),
        _post("p3", likes=100, comments=0, hours_ago=20,
              caption="#coding #100daysofcode #explorepage #2026"),
        _post("p4", likes=50, comments=0, hours_ago=10, caption="#coding #desksetup"),
    ])


def test_hashtag_emits_exactly_one_item_for_the_tag():
    parsed = gti.parse_hashtag_media(_tag_payload(), tag="#Coding", now=NOW)
    item = parsed["item"]
    _assert_raw_item(item)
    assert item["source"] == gti.SOURCE_HASHTAG
    assert item["source_key"] == "tag:coding"          # ONE key for 4 posts
    assert item["title"] == "#coding"
    assert item["url"] == "https://www.instagram.com/explore/tags/coding/"
    assert parsed["posts"] == 4
    # Median of (4, 320, 100, 50) = 75 — median, not mean, so one viral post
    # cannot define the tag; and not the post COUNT, which saturates at 50.
    assert item["metric_value"] == 75.0
    assert item["metric_label"] == "median engagement/24h"
    assert item["extra"]["posts"] == 4
    assert item["extra"]["top_engagement"] == 320
    assert len(item["extra"]["examples"]) == 3
    # published_at is the NEWEST post in the window (2h ago), not the oldest.
    assert item["published_at"].startswith("2026-08-19T10:00")


def test_hashtag_cotags_count_distinct_posts_and_drop_filler():
    parsed = gti.parse_hashtag_media(_tag_payload(), tag="coding", now=NOW)
    counts = {name: len(st["post_ids"]) for name, st in parsed["cotags"].items()}
    assert counts["100daysofcode"] == 3
    assert counts["desksetup"] == 2
    assert "coding" not in counts        # the seed is never its own co-tag
    assert "fyp" not in counts           # filler
    assert "explorepage" not in counts   # filler
    assert "2026" not in counts          # a calendar
    # The item carries the ranked co-tags for the UI too.
    assert parsed["item"]["extra"]["co_tags"][0] == ["100daysofcode", 3]


def test_build_cotag_items_needs_corroboration_and_excludes_the_roster():
    parsed = gti.parse_hashtag_media(_tag_payload(), tag="coding", now=NOW)
    items = gti.build_cotag_items(parsed["cotags"], sample_size=parsed["posts"],
                                  min_posts=3, exclude=["coding"])
    assert [it["source_key"] for it in items] == ["cotag:100daysofcode"]
    it = items[0]
    _assert_raw_item(it)
    assert it["source"] == gti.SOURCE_HASHTAG
    assert it["title"] == "#100daysofcode"
    assert it["metric_value"] == 3.0
    assert it["metric_label"] == "co-mentions/24h"
    assert it["extra"]["seed_tags"] == ["coding"]
    assert it["extra"]["sample_size"] == 4
    assert it["published_at"] is not None
    # A roster tag already has its own, better item — it must not corroborate
    # itself through the co-tag path.
    assert gti.build_cotag_items(parsed["cotags"], sample_size=4, min_posts=2,
                                 exclude=["coding", "100daysofcode", "desksetup"]) == []


def test_merge_cotags_never_double_counts_a_post_carrying_two_seeds():
    a = gti.parse_hashtag_media(_tag_payload(), tag="coding", now=NOW)["cotags"]
    # The same four posts also come back under #100daysofcode, so #desksetup is
    # seen twice — but on the SAME posts.
    b = gti.parse_hashtag_media(_tag_payload(), tag="100daysofcode", now=NOW)["cotags"]
    merged = gti.merge_cotags(dict(a), b)
    assert len(merged["desksetup"]["post_ids"]) == 2      # not 4
    assert sorted(merged["desksetup"]["seeds"]) == ["100daysofcode", "coding"]
    assert "coding" in merged                            # a seed of the OTHER tag


def test_hashtag_parser_never_raises_on_junk():
    empty = {"item": None, "posts": 0, "latest": None, "cotags": {}}
    assert gti.parse_hashtag_media(None, tag="coding") == empty
    assert gti.parse_hashtag_media({"data": []}, tag="coding") == empty
    assert gti.parse_hashtag_media({"data": [None, 5, {}]}, tag="") == empty
    # A malformed entry is skipped, the good one still lands.
    parsed = gti.parse_hashtag_media(
        {"data": [{"id": "ok", "caption": "hi", "timestamp": "nonsense",
                   "like_count": 3}]},
        tag="coding", now=NOW)
    assert parsed["item"]["source_key"] == "tag:coding"
    assert parsed["item"]["published_at"] is None


# --------------------------------------------------------------------------- #
# Fetchers over a fake transport
# --------------------------------------------------------------------------- #

def test_fetch_watch_makes_one_call_and_authenticates():
    media = [_post(f"1{i}", likes=100) for i in range(6)] + [_post("hit", likes=1000)]

    def handler(url, params):
        assert url.endswith("/17841400000000000")
        assert params["access_token"] == "TOKEN-SECRET"
        assert "business_discovery.username(9gag)" in params["fields"]
        return _bd("9gag", media)

    client = _FakeClient(handler)
    items = _run(gti.fetch_instagram_watch(client, username="@9gag", now=NOW))
    assert len(client.calls) == 1          # one Graph call per account per refresh
    assert [it["source_key"] for it in items] == ["ig:hit"]


def test_fetch_watch_reports_an_unreadable_handle_without_killing_the_lane():
    client = _FakeClient(lambda url, params: {"id": "17841400000000000"})
    with pytest.raises(gti.InstagramAccountUnreadable):
        _run(gti.fetch_instagram_watch(client, username="someones_personal_account"))
    # Not a lane failure: the breaker stays closed and the next account runs.
    assert gti.circuit_reason() is None


def test_fetch_hashtag_resolves_once_and_caches_the_permanent_id():
    calls = {"search": 0}

    def handler(url, params):
        if "ig_hashtag_search" in url:
            calls["search"] += 1
            return {"data": [{"id": "17841562899999999"}]}
        assert url.endswith("/17841562899999999/recent_media")
        assert params["user_id"] == "17841400000000000"
        return _tag_payload()

    client = _FakeClient(handler)
    first = _run(gti.fetch_instagram_hashtag(client, tag="#coding", now=NOW))
    second = _run(gti.fetch_instagram_hashtag(client, tag="coding", now=NOW))
    assert calls["search"] == 1            # hashtag ids are permanent
    assert len(client.calls) == 3          # 1 search + 2 recent_media

    keys = [it["source_key"] for it in first]
    assert keys[0] == "tag:coding"
    assert "cotag:100daysofcode" in keys
    assert keys == [it["source_key"] for it in second]


def test_fetch_hashtags_merges_cotags_across_the_roster():
    def handler(url, params):
        if "ig_hashtag_search" in url:
            return {"data": [{"id": "id-" + params["q"]}]}
        return _tag_payload()

    client = _FakeClient(handler)
    items = _run(gti.fetch_instagram_hashtags(
        client, tags=["coding", "100daysofcode"], now=NOW, min_cotag_posts=2))
    keys = [it["source_key"] for it in items]
    # One item per roster tag ...
    assert keys[:2] == ["tag:coding", "tag:100daysofcode"]
    # ... plus co-tags mined from captions, with roster names excluded because
    # they already have their own items.
    assert "cotag:coding" not in keys
    assert "cotag:100daysofcode" not in keys
    cotag = [it for it in items if it["source_key"] == "cotag:desksetup"]
    assert cotag and cotag[0]["metric_value"] == 2.0   # 2 distinct posts, not 4
    assert cotag[0]["extra"]["sample_size"] == 8       # 2 tags x 4 posts


# --------------------------------------------------------------------------- #
# Token expiry + the circuit breaker
# --------------------------------------------------------------------------- #

def test_expired_token_raises_instagram_token_expired_with_the_recipe():
    client = _FakeClient(lambda url, params: EXPIRED_TOKEN_ERROR)
    with pytest.raises(gti.InstagramTokenExpired) as excinfo:
        _run(gti.fetch_instagram_watch(client, username="9gag"))
    msg = str(excinfo.value)
    # Not a bare RuntimeError — _err_str renders the CLASS NAME into the run
    # card, so the class is half the message.
    assert type(excinfo.value) is gti.InstagramTokenExpired
    assert isinstance(excinfo.value, RuntimeError)
    assert "15-Aug-26 07:43:59 PDT" in msg          # when it died
    assert "fb_exchange_token" in msg               # the CORRECT exchange
    assert "/me/accounts" in msg                    # the permanent fix
    assert "190/463" in msg
    # The token itself must never reach a log line or a run card.
    assert "TOKEN-SECRET" not in msg
    # And it must survive general_trends_sources' 300-char cap intact.
    assert len(f"{type(excinfo.value).__name__}: {msg}") <= 300


def test_circuit_opens_on_the_first_190_and_spends_no_further_request():
    client = _FakeClient(lambda url, params: EXPIRED_TOKEN_ERROR)
    with pytest.raises(gti.InstagramTokenExpired):
        _run(gti.fetch_instagram_watch(client, username="9gag"))
    assert len(client.calls) == 1

    # Every remaining unit of this run fails instantly, without a network call:
    # one dead token must not burn ~40 doomed requests and eat the rate limit
    # the publish path shares.
    for handle in ("memezar", "openai", "visitabudhabi"):
        with pytest.raises(gti.InstagramTokenExpired):
            _run(gti.fetch_instagram_watch(client, username=handle))
    with pytest.raises(gti.InstagramTokenExpired):
        _run(gti.fetch_instagram_hashtag(client, tag="coding"))
    assert len(client.calls) == 1
    assert "access token expired" in (gti.circuit_reason() or "")


def test_reset_circuit_closes_it_for_the_next_run():
    client = _FakeClient(lambda url, params: EXPIRED_TOKEN_ERROR)
    with pytest.raises(gti.InstagramTokenExpired):
        _run(gti.fetch_instagram_watch(client, username="9gag"))
    assert gti.circuit_reason() is not None

    gti.reset_circuit()
    assert gti.circuit_reason() is None

    media = [_post(f"1{i}", likes=100) for i in range(6)] + [_post("hit", likes=1000)]
    ok = _FakeClient(lambda url, params: _bd("9gag", media))
    items = _run(gti.fetch_instagram_watch(ok, username="9gag", now=NOW))
    assert [it["source_key"] for it in items] == ["ig:hit"]


def test_a_roster_run_aborts_the_moment_the_token_dies():
    def handler(url, params):
        if "ig_hashtag_search" in url:
            return EXPIRED_TOKEN_ERROR
        return _tag_payload()

    client = _FakeClient(handler)
    with pytest.raises(gti.InstagramTokenExpired):
        _run(gti.fetch_instagram_hashtags(
            client, tags=list(gti.SUGGESTED_HASHTAGS), now=NOW))
    # 20 tags in the roster, exactly ONE request spent.
    assert len(client.calls) == 1


def test_rate_limit_pauses_the_whole_lane_but_scope_errors_only_the_hashtags():
    client = _FakeClient(lambda url, params: _graph_error(4, "Application request limit reached"))
    with pytest.raises(gti.InstagramQuotaExhausted):
        _run(gti.fetch_instagram_watch(client, username="9gag"))
    assert gti.circuit_reason() is not None
    gti.reset_circuit()

    # A missing scope on the hashtag edges says nothing about business_discovery,
    # which runs on scopes we already hold — so it must not take the watch lane
    # down with it.
    def handler(url, params):
        if "ig_hashtag_search" in url:
            return _graph_error(
                10, "Application does not have permission for this action")
        media = [_post(f"1{i}", likes=100) for i in range(6)] + [_post("hit", likes=1000)]
        return _bd("9gag", media)

    client2 = _FakeClient(handler)
    with pytest.raises(gti.InstagramScopeMissing):
        _run(gti.fetch_instagram_hashtag(client2, tag="coding"))
    assert gti.circuit_reason(gti.SCOPE_HASHTAG) is not None
    assert gti.circuit_reason(gti.SCOPE_ALL) is None
    items = _run(gti.fetch_instagram_watch(client2, username="9gag", now=NOW))
    assert [it["source_key"] for it in items] == ["ig:hit"]


def test_missing_credentials_disable_the_lane_without_raising_config_error(monkeypatch):
    def _boom():
        raise ig.InstagramConfigError("INSTAGRAM_ACCESS_TOKEN is not set.")

    monkeypatch.setattr(ig, "_require_creds", _boom)
    client = _FakeClient(lambda url, params: {})
    with pytest.raises(gti.InstagramSourceUnavailable) as excinfo:
        _run(gti.fetch_instagram_watch(client, username="9gag"))
    # Converted, not propagated: the run card should say "instagram", not name
    # a client module the general-trends feed does not otherwise use.
    assert not isinstance(excinfo.value, ig.InstagramConfigError)
    assert "instagram:" in str(excinfo.value)
    assert client.calls == []


def test_a_transient_5xx_costs_one_unit_not_the_lane():
    client = _FakeClient(lambda url, params: (503, {"error": {"message": "Sorry, something went wrong."}}))
    with pytest.raises(gti.InstagramTransientError):
        _run(gti.fetch_instagram_watch(client, username="9gag"))
    assert gti.circuit_reason() is None

    # A 429 that DOES carry a Graph code is the real throttle, and the code
    # wins over the status: the lane pauses so the publish path keeps budget.
    throttled = _FakeClient(lambda url, params: (
        429, {"error": {"message": "User request limit reached", "code": 17}}))
    with pytest.raises(gti.InstagramQuotaExhausted):
        _run(gti.fetch_instagram_watch(throttled, username="9gag"))
    assert gti.circuit_reason() is not None


def test_an_unrelated_graph_error_is_per_unit_not_per_lane():
    client = _FakeClient(lambda url, params: _graph_error(
        100, "Unsupported get request. Object with ID does not exist", subcode=33))
    with pytest.raises(gti.InstagramAccountUnreadable):
        _run(gti.fetch_instagram_watch(client, username="ghost"))
    assert gti.circuit_reason() is None


def test_watch_accounts_helper_survives_a_bad_handle():
    media = [_post(f"1{i}", likes=100) for i in range(6)] + [_post("hit", likes=1000)]

    def handler(url, params):
        if "username(ghost)" in params.get("fields", ""):
            return {"id": "17841400000000000"}   # empty business_discovery
        return _bd("9gag", media)

    client = _FakeClient(handler)
    items = _run(gti.fetch_instagram_watch_accounts(
        client, usernames=["ghost", "@9gag"], now=NOW))
    assert [it["source_key"] for it in items] == ["ig:hit"]


# --------------------------------------------------------------------------- #
# Wiring the rest of the feed depends on
# --------------------------------------------------------------------------- #

def test_source_ids_are_registered_in_general_trends_sources():
    missing = [s for s in (gti.SOURCE_HASHTAG, gti.SOURCE_WATCH)
               if s not in gts.SOURCE_IDS]
    if missing:
        pytest.skip(
            "general_trends_sources.SOURCE_IDS does not list "
            f"{missing} yet — see the Instagram report: SOURCE_IDS, "
            "SOURCE_LABELS, NON_MINTING_SOURCES (instagram_watch), "
            "SERIAL_SOURCES and the build_fetch_plan/_dispatch branches.")
    for sid in (gti.SOURCE_HASHTAG, gti.SOURCE_WATCH):
        assert gts.SOURCE_LABELS.get(sid)


def test_watch_lane_is_declared_non_minting():
    non_minting = getattr(gts, "NON_MINTING_SOURCES", None)
    if non_minting is None:
        pytest.skip("general_trends_sources.NON_MINTING_SOURCES not landed yet")
    # One post is not a trend: instagram_watch may corroborate, never mint.
    assert gti.SOURCE_WATCH in non_minting
    # Hashtags aggregate many posts, so that lane DOES mint.
    assert gti.SOURCE_HASHTAG not in non_minting


def test_default_roster_fits_the_quota():
    assert len(gti.SUGGESTED_HASHTAGS) <= gti.ROSTER_MAX
    assert len(set(gti.SUGGESTED_HASHTAGS)) == len(gti.SUGGESTED_HASHTAGS)
    assert gti.ROSTER_MAX < gti.HASHTAG_UNIQUE_QUOTA_PER_7D  # slots for ad-hoc lookups
    assert 12 <= len(gti.DEFAULT_WATCH_ACCOUNTS) <= 20
    assert all(a == gti.clean_tag(a).replace("#", "") or a.islower()
               for a in gti.DEFAULT_WATCH_ACCOUNTS)
