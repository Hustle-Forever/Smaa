"""Tests for General Trends — sources, parsers and fetchers.

Module under test: ``app/services/general_trends_sources.py``.

Everything is offline. The fixtures below are TRIMMED samples of the real
feeds, captured once on 2026-08-19 (2–3 entries each, real markup), so the
parsers are exercised against what the sources actually emit — including the
awkward bits: Google's ``ht:`` namespace and "5000+" traffic strings, Reddit's
HTML-escaped ``<table>`` content, Know Your Meme's ``<img>`` inside
``description``, trends24's unquoted attributes and duplicated entries.

Fetchers are tested through a fake ``httpx.AsyncClient`` (a tiny object with an
``async get``) and ``fetch_all`` through monkeypatched ``fetch_<source>``
functions — that is the pipeline's monkeypatch seam and it must keep working.

No pytest-asyncio in this venv: coroutines are driven with ``asyncio.run`` via
``_run`` (same as tests/test_trend_finder.py).
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import general_trends_sources as gts  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """fetch_all pauses between serial-lane units and backs off on 429 via
    gts._sleep; never sleep for real in tests (tests that care re-patch it)."""
    async def _instant(seconds):
        return None
    monkeypatch.setattr(gts, "_sleep", _instant)


def _run(coro):
    return asyncio.run(coro)


RAW_KEYS = {
    "source", "source_key", "title", "url", "summary", "image_url", "metric_value",
    "metric_label", "published_at", "region", "category", "extra",
}


def _assert_raw_item(it: dict) -> None:
    assert set(it.keys()) == RAW_KEYS, it.keys()
    assert it["source"] in gts.SOURCE_IDS
    assert isinstance(it["source_key"], str) and it["source_key"]
    assert isinstance(it["title"], str) and it["title"]
    assert isinstance(it["summary"], str) and len(it["summary"]) <= 600
    assert it["url"] is None or isinstance(it["url"], str)
    assert it["image_url"] is None or isinstance(it["image_url"], str)
    assert it["metric_value"] is None or isinstance(it["metric_value"], float)
    assert it["published_at"] is None or isinstance(it["published_at"], str)
    assert isinstance(it["region"], str) and it["region"]
    assert isinstance(it["extra"], dict)


# =========================================================================== #
# Fixtures — trimmed real markup
# =========================================================================== #

GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
\t<channel>
\t\t<title>Daily Search Trends</title>
\t\t<description>Recent searches</description>
\t\t<link>https://trends.google.com/trending/rss?geo=US</link>
\t\t<item>
\t\t\t<title>coco gauff</title>
\t\t\t<ht:approx_traffic>5000+</ht:approx_traffic>
\t\t\t<description/>
\t\t\t<link>https://trends.google.com/trending/rss?geo=US</link>
\t\t\t<pubDate>Tue, 18 Aug 2026 12:50:00 -0700</pubDate>
\t\t\t<ht:picture>https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSr20Ccvgbf</ht:picture>
\t\t\t<ht:picture_source>Sofascore</ht:picture_source>
\t\t\t<ht:news_item>
\t\t\t\t<ht:news_item_title>Gauff vs Li preview: Cincinnati Open R32</ht:news_item_title>
\t\t\t\t<ht:news_item_snippet/>
\t\t\t\t<ht:news_item_url>https://www.sofascore.com/news/coco-gauff-vs-ann-li</ht:news_item_url>
\t\t\t\t<ht:news_item_picture>https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSr20Ccvgbf</ht:news_item_picture>
\t\t\t\t<ht:news_item_source>Sofascore</ht:news_item_source>
\t\t\t</ht:news_item>
\t\t\t<ht:news_item>
\t\t\t\t<ht:news_item_title>Bejlek vs. Alexandrova Prediction at the Western &amp; Southern Open</ht:news_item_title>
\t\t\t\t<ht:news_item_snippet/>
\t\t\t\t<ht:news_item_url>https://www.bleachernation.com/picks/2026/08/17/bejlek-vs-alexandrova/</ht:news_item_url>
\t\t\t\t<ht:news_item_source>Bleacher Nation</ht:news_item_source>
\t\t\t</ht:news_item>
\t\t</item>
\t\t<item>
\t\t\t<title>billy gibbons</title>
\t\t\t<ht:approx_traffic>2,000+</ht:approx_traffic>
\t\t\t<description/>
\t\t\t<link>https://trends.google.com/trending/rss?geo=US</link>
\t\t\t<pubDate>Tue, 18 Aug 2026 12:40:00 -0700</pubDate>
\t\t\t<ht:news_item>
\t\t\t\t<ht:news_item_title>Frank Beard, longtime ZZ Top drummer, dead at 77</ht:news_item_title>
\t\t\t\t<ht:news_item_url>https://abcnews.com/GMA/Culture/frank-beard-dead-77/story?id=119913099</ht:news_item_url>
\t\t\t\t<ht:news_item_source>ABC News</ht:news_item_source>
\t\t\t</ht:news_item>
\t\t</item>
\t\t<item>
\t\t\t<title></title>
\t\t\t<ht:approx_traffic>200+</ht:approx_traffic>
\t\t</item>
\t</channel>
</rss>
"""

BLUESKY_JSON = {
    "recIdStr": "3909814706394792740",
    "trends": [
        {
            "topic": "cbd98b40f918",
            "displayName": "Ossoff criticizes Trump's work ethic",
            "description": "Ossoff says Trump skips duties to travel, referencing a Qatar-gifted plane.",
            "link": "/profile/did:plc:qrz3lhbyuxbeilrc6nekdqme/feed/cbd98b40f918",
            "startedAt": "2026-08-16T23:00:01.075981+00:00",
            "postCount": 3801,
            "status": "trending",
            "category": "politics",
            "actors": [{"did": "did:plc:4llrhdclvdlmmynkwsmg5tdc", "handle": "atrupar.com"}],
        },
        {
            "topic": "b11b956c96e9",
            "displayName": "Hayden Panettiere dies at 36",
            "description": "Fans mourn the Heroes actress, recalling her iconic \"save the cheerleader\" role.",
            "link": "/profile/did:plc:qrz3lhbyuxbeilrc6nekdqme/feed/b11b956c96e9",
            "startedAt": "2026-08-17T03:19:40.689102Z",
            "postCount": 6969,
            "status": "cooling",
            "category": "entertainment",
            "actors": [],
        },
        {"topic": "deadbeef0000", "displayName": "", "postCount": 1},
        "not-a-dict",
    ],
}

REDDIT_ATOM = (
    '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
    'xmlns:media="http://search.yahoo.com/mrss/"><category term="memes" label="r/memes"/>'
    '<updated>2026-08-18T20:10:20+00:00</updated><id>/r/memes/top.rss?t=day&amp;limit=3</id>'
    '<title>rising submissions : memes</title>'
    # 1) image post with media:thumbnail
    '<entry><author><name>/u/Frequent-Loquat1941</name><uri>https://www.reddit.com/user/Frequent-Loquat1941</uri></author>'
    '<category term="memes" label="r/memes"/><content type="html">&lt;table&gt; &lt;tr&gt;&lt;td&gt; '
    '&lt;a href=&quot;https://www.reddit.com/r/memes/comments/1vrx67n/how_to_upset_turkish_people/&quot;&gt; '
    '&lt;img src=&quot;https://preview.redd.it/w95kis2mc6kh1.jpeg?width=640&amp;amp;crop=smart&quot; '
    'alt=&quot;How to upset Turkish people&quot; title=&quot;How to upset Turkish people&quot; /&gt; &lt;/a&gt; '
    '&lt;/td&gt;&lt;td&gt; &amp;#32; submitted by &amp;#32; &lt;a href=&quot;https://www.reddit.com/user/Frequent-Loquat1941&quot;&gt; '
    '/u/Frequent-Loquat1941 &lt;/a&gt; &lt;br/&gt; &lt;span&gt;&lt;a href=&quot;https://i.redd.it/w95kis2mc6kh1.jpeg&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt; '
    '&amp;#32; &lt;span&gt;&lt;a href=&quot;https://www.reddit.com/r/memes/comments/1vrx67n/how_to_upset_turkish_people/&quot;&gt;[comments]&lt;/a&gt;&lt;/span&gt; '
    '&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</content><id>t3_1vrx67n</id>'
    '<media:thumbnail url="https://preview.redd.it/w95kis2mc6kh1.jpeg?width=640&amp;crop=smart" />'
    '<link href="https://www.reddit.com/r/memes/comments/1vrx67n/how_to_upset_turkish_people/" />'
    '<updated>2026-08-18T18:12:04+00:00</updated><published>2026-08-18T18:12:04+00:00</published>'
    '<title>How to upset Turkish people</title></entry>'
    # 2) text post — NO media:thumbnail, no img; real self-post markup shape
    '<entry><author><name>/u/someone</name></author><category term="OutOfTheLoop" label="r/OutOfTheLoop"/>'
    '<content type="html">&lt;!-- SC_OFF --&gt;&lt;div class=&quot;md&quot;&gt;&lt;p&gt;What is going on with the '
    'nihilist penguin memes? Every third post on my feed is a penguin saying nothing matters.&lt;/p&gt;&lt;/div&gt;'
    '&lt;!-- SC_ON --&gt; &amp;#32; submitted by &amp;#32; &lt;a href=&quot;https://www.reddit.com/user/someone&quot;&gt; /u/someone &lt;/a&gt; '
    '&lt;span&gt;&lt;a href=&quot;https://www.reddit.com/r/OutOfTheLoop/comments/1vrabc/whats_up/&quot;&gt;[link]&lt;/a&gt;&lt;/span&gt; '
    '&lt;span&gt;&lt;a href=&quot;https://www.reddit.com/r/OutOfTheLoop/comments/1vrabc/whats_up/&quot;&gt;[comments]&lt;/a&gt;&lt;/span&gt;</content>'
    '<id>t3_1vrabc</id><link href="https://www.reddit.com/r/OutOfTheLoop/comments/1vrabc/whats_up/" />'
    '<updated>2026-08-18T16:28:57Z</updated><published>2026-08-18T16:28:57Z</published>'
    "<title>What's up with the nihilist penguin?</title></entry>"
    # 3) broken entry — no title
    '<entry><id>t3_broken</id><link href="https://www.reddit.com/r/memes/comments/broken/" /></entry>'
    '</feed>'
)

KYM_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Know Your Meme Newsfeed</title>
    <link>https://knowyourmeme.com</link>
    <item>
      <guid isPermaLink="false">post:95621</guid>
      <title>TikTokers Are Flipping The Script On Reportedly Inappropriate Uses Of The Viral 'Matcha Filter'</title>
      <pubDate>Tue, 18 Aug 2026 12:46:01 -0400</pubDate>
      <link>https://knowyourmeme.com/memes/matcha-filter</link>
      <description>&lt;img alt="Matchafiltercover" src="https://i.kym-cdn.com/entries/icons/mobile/000/057/309/matchafiltercover.jpg" /&gt;&lt;p&gt;The Matcha Filter or Wild Heart Whisper Filter, is a TikTok filter that makes the subject of a video look psychedelic by turning them different shades of green and black, resembling matcha.&lt;/p&gt;
&lt;p&gt;The filter went viral in mid-August 2026 as the subject of a trend set to a remix of the Filipino song "Halik Sobrang Diin part two".&lt;/p&gt;</description>
    </item>
    <item>
      <guid isPermaLink="false">post:95626</guid>
      <title>Social Media Users React To Miguel's Chopped Haircut In Coco 2</title>
      <pubDate>Tue, 18 Aug 2026 15:26:55 -0400</pubDate>
      <link>https://knowyourmeme.com/memes/miguels-chopped-haircut-in-coco-2-miguel-taper-fade</link>
      <description>&lt;p&gt;Miguel's Chopped Haircut in Coco 2, also known as Miguel Taper Fade, refers to a series of memes and online reactions to the first official concept art for the animated movie Coco 2.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>
"""

WIKI_TODAY = {
    "items": [{
        "project": "en.wikipedia", "access": "all-access",
        "year": "2026", "month": "08", "day": "17",
        "articles": [
            {"article": "Hayden_Panettiere", "views": 10392637, "rank": 1},
            {"article": "Main_Page", "views": 7006788, "rank": 2},
            {"article": "Wladimir_Klitschko", "views": 1581846, "rank": 3},
            {"article": "Special:Search", "views": 863288, "rank": 5},
            {"article": "Wikipedia:Featured_pictures", "views": 750159, "rank": 6},
            {"article": "Spider-Man:_Brand_New_Day", "views": 260426, "rank": 9},
            {"article": "File:Something.jpg", "views": 100000, "rank": 10},
            {"article": "Portal:Current_events", "views": 90000, "rank": 11},
            {"article": "Help:Contents", "views": 80000, "rank": 12},
            {"article": "Talk:Foo", "views": 70000, "rank": 13},
            {"article": "-", "views": 60000, "rank": 14},
            {"article": "Deaths_in_2026", "views": 200000, "rank": 15},   # falling ↓
            {"article": "Nihilist_Penguin", "views": 50000, "rank": 16},  # new to the list
        ],
    }],
}

WIKI_YESTERDAY = {
    "items": [{
        "project": "en.wikipedia", "access": "all-access",
        "year": "2026", "month": "08", "day": "16",
        "articles": [
            {"article": "Hayden_Panettiere", "views": 12000, "rank": 40},
            {"article": "Main_Page", "views": 7100000, "rank": 1},
            {"article": "Wladimir_Klitschko", "views": 5000, "rank": 300},
            {"article": "Spider-Man:_Brand_New_Day", "views": 260426, "rank": 9},  # flat = not rising
            {"article": "Deaths_in_2026", "views": 250000, "rank": 8},
        ],
    }],
}

YOUTUBE_JSON = {
    "kind": "youtube#videoListResponse",
    "items": [
        {
            "kind": "youtube#video", "id": "dQw4w9WgXcQ",
            "snippet": {
                "publishedAt": "2026-08-17T15:00:12Z",
                "channelId": "UCuAXFkgsw1L7xaCfnd5JJOw",
                "title": "I built a game in 42 hours",
                "description": "Chaos.\n\nMore chaos.",
                "thumbnails": {
                    "default": {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/default.jpg"},
                    "high": {"url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"},
                },
                "channelTitle": "Some Creator",
                "tags": ["gamedev", "42"],
                "categoryId": "20",
            },
            "statistics": {"viewCount": "1234567", "likeCount": "45000", "commentCount": "3000"},
        },
        {
            "kind": "youtube#video", "id": "abc123",
            "snippet": {
                "publishedAt": "2026-08-18T09:30:00Z",
                "title": "Trailer",
                "thumbnails": {"medium": {"url": "https://i.ytimg.com/vi/abc123/mqdefault.jpg"}},
                "channelTitle": "Studio",
                "categoryId": "999",
            },
            "statistics": {},
        },
        {"kind": "youtube#video", "id": "", "snippet": {"title": "no id"}},
    ],
}

# Two hourly lists, most recent FIRST — the parser must only read the first.
# The real page uses unquoted attributes and repeats "Dune".
TRENDS24_HTML = (
    '<!doctype html><html><body><div id=timeline-container class="">'
    '<div class="px-2 scroll-smooth flex gap-x-4 w-fit pt-8">'
    '<div class=list-container><h3 class=title data-timestamp=1787081080.333>Tue Aug 18 2026 19:24:40 GMT+0000</h3>'
    '<ol class=trend-card__list>'
    '<li><span class=trend-name><a href="https://twitter.com/search?q=GTA%206" class=trend-link>GTA 6</a>'
    '<span class=tweet-count data-count="">Under 10K</span></span></li>'
    '<li><span class=trend-name><a href="https://twitter.com/search?q=Dune" class=trend-link>Dune</a>'
    '<span class=tweet-count data-count="52400">52K</span></span></li>'
    '<li><span class=trend-name><a href="https://twitter.com/search?q=Dune" class=trend-link>Dune</a>'
    '<span class=tweet-count data-count=""></span></span></li>'
    '<li><span class=trend-name><a href="https://twitter.com/search?q=%23NBA2K27" class=trend-link>#NBA2K27</a>'
    '<span class=tweet-count data-count=""></span></span></li>'
    '</ol></div>'
    '<div class=list-container><h3 class=title data-timestamp=1787077467.265>Tue Aug 18 2026 18:24:27 GMT+0000</h3>'
    '<ol class=trend-card__list>'
    '<li><span class=trend-name><a href="https://twitter.com/search?q=Older" class=trend-link>Older Trend</a>'
    '<span class=tweet-count data-count=""></span></span></li>'
    '</ol></div>'
    '</div></div></body></html>'
)


# =========================================================================== #
# 1. Identity constants
# =========================================================================== #

def test_source_ids_and_labels_agree():
    assert gts.SOURCE_IDS == (
        "google_trends", "bluesky", "reddit", "kym", "kym_entries", "kym_editorials",
        "wikipedia", "youtube", "youtube_shorts", "x_trends",
        "hacker_news", "lemmy", "github", "google_news", "tiktok",
        "instagram", "instagram_watch")
    assert set(gts.SOURCE_LABELS) == set(gts.SOURCE_IDS)
    assert gts.SOURCE_LABELS["kym"] == "Know Your Meme"
    assert gts.SOURCE_LABELS["kym_entries"] == "Know Your Meme (entries)"
    assert gts.SOURCE_LABELS["kym_editorials"] == "Know Your Meme (explainers)"
    assert gts.SOURCE_LABELS["youtube_shorts"] == "YouTube Shorts (sounds)"
    assert gts.SOURCE_LABELS["x_trends"] == "X (via trends24)"
    assert gts.SOURCE_LABELS["hacker_news"] == "Hacker News"
    assert gts.SOURCE_LABELS["instagram_watch"] == "Instagram (watch)"


def test_only_format_registries_may_mint_a_trend():
    """A trend is a participatory FORMAT — a hashtag people posted under, a
    meme template being remade. Only a registry of things people are MAKING can
    create a card; everything else reports what HAPPENED and can merely
    corroborate.

    Measured on a full live fetch: 328 items kept, only 28 from a format
    registry. The other 300 (google_news 80, reddit 45, lemmy 40, wikipedia 40,
    github 30, hacker_news 28, bluesky 25, google_trends 12) were headlines,
    repos and search spikes — nothing a social team can make a version of, and
    every one of them used to become a card."""
    assert gts.MINTING_SOURCES == frozenset({
        "tiktok", "kym", "kym_entries", "instagram", "youtube_shorts"})
    assert gts.MINTING_SOURCES <= set(gts.SOURCE_IDS)
    for reporter in ("hacker_news", "lemmy", "github", "google_news", "bluesky",
                     "google_trends", "youtube", "reddit", "wikipedia",
                     "x_trends", "instagram_watch",
                     # An ARTICLE ABOUT a format is not the format. KYM's
                     # explainers are the best corroboration in the system and
                     # must never become a card in their own right.
                     "kym_editorials"):
        assert reporter in gts.NON_MINTING_SOURCES, reporter
        assert gts.is_minting(reporter) is False
    for registry in gts.MINTING_SOURCES:
        assert gts.is_minting(registry) is True


def test_the_two_minting_sets_are_complements_and_cannot_drift():
    """NON_MINTING_SOURCES is derived, not typed out twice."""
    assert gts.MINTING_SOURCES | gts.NON_MINTING_SOURCES == set(gts.SOURCE_IDS)
    assert gts.MINTING_SOURCES & gts.NON_MINTING_SOURCES == frozenset()
    assert gts.is_minting(None) is False
    assert gts.is_minting("not_a_source") is False


def test_region_slugs_and_reddit_ua():
    assert gts.REGION_SLUGS["US"] == "united-states"
    assert gts.REGION_SLUGS["AE"] == "united-arab-emirates"
    assert set(gts.REGION_SLUGS) == {"US", "AE", "GB", "IN", "SA", "EG"}
    assert gts.REDDIT_UA == "42studio-trends/1.0 (by u/42studio)"


def test_reddit_url_uses_top_of_day_not_rising():
    """/rising is pre-consensus by construction — 0 of 167 rising posts captured
    in the live run survived even 80 minutes. /top?t=day is the day's consensus
    and costs the same one throttled request."""
    assert gts.REDDIT_TOP_URL == "https://www.reddit.com/r/{sub}/top.rss"
    assert gts.REDDIT_TOP_PARAMS == {"t": "day"}
    assert "rising" not in gts.REDDIT_TOP_URL
    assert not hasattr(gts, "REDDIT_RISING_URL")


# =========================================================================== #
# 2. Helpers
# =========================================================================== #

@pytest.mark.parametrize("raw,expected", [
    ("2000+", 2000.0),
    ("2,000+", 2000.0),
    ("5K+", 5000.0),
    ("5k+", 5000.0),
    ("1.2M+", 1_200_000.0),
    ("200+", 200.0),
    ("  100 ", 100.0),
    ("", None),
    (None, None),
    ("lots", None),
])
def test_traffic_to_float(raw, expected):
    assert gts.traffic_to_float(raw) == expected


def test_html_to_text_and_first_img():
    html = '<img src="https://x/y.jpg" alt="a"/><p>Hello   <b>world</b>\n\n again</p>'
    assert gts.html_to_text(html) == "Hello world again"
    assert gts.first_img_src(html) == "https://x/y.jpg"
    assert gts.first_img_src("<p>no image</p>") is None
    assert gts.html_to_text("") == ""
    assert gts.first_img_src(None) is None


def test_cap_text_caps_at_600():
    long = "word " * 300
    out = gts.cap_text(long)
    assert len(out) <= 600
    assert out.endswith("…")
    assert gts.cap_text("short") == "short"


def test_parse_dates():
    assert gts.parse_rfc822("Tue, 18 Aug 2026 12:50:00 -0700") == "2026-08-18T19:50:00+00:00"
    assert gts.parse_rfc822("garbage") is None
    assert gts.parse_rfc822("") is None
    assert gts.parse_iso("2026-08-18T16:28:57Z") == "2026-08-18T16:28:57+00:00"
    assert gts.parse_iso("2026-08-16T23:00:01.075981+00:00") == "2026-08-16T23:00:01.075981+00:00"
    assert gts.parse_iso("not a date") is None
    assert gts.parse_iso(None) is None


# =========================================================================== #
# 3. Google Trends
# =========================================================================== #

def test_parse_google_trends_rss():
    items = gts.parse_google_trends_rss(GOOGLE_RSS, "us")
    assert len(items) == 2               # the empty-title item is skipped
    for it in items:
        _assert_raw_item(it)
    a, b = items
    assert a["source"] == "google_trends"
    assert a["source_key"] == "US:coco gauff"
    assert a["title"] == "coco gauff"
    assert a["region"] == "US"
    assert a["metric_value"] == 5000.0 and a["metric_label"] == "searches"
    assert a["url"] == "https://www.sofascore.com/news/coco-gauff-vs-ann-li"   # first news url
    assert a["image_url"].startswith("https://encrypted-tbn0.gstatic.com/")
    assert a["summary"] == (
        "Gauff vs Li preview: Cincinnati Open R32 · "
        "Bejlek vs. Alexandrova Prediction at the Western & Southern Open")
    assert a["published_at"] == "2026-08-18T19:50:00+00:00"
    assert len(a["extra"]["news_items"]) == 2
    assert a["extra"]["news_items"][1]["source"] == "Bleacher Nation"
    assert a["extra"]["picture_source"] == "Sofascore"
    # "2,000+" and a missing picture
    assert b["metric_value"] == 2000.0
    assert b["image_url"] is None
    assert b["url"].startswith("https://abcnews.com/")


def test_parse_google_trends_empty_and_junk():
    assert gts.parse_google_trends_rss("", "US") == []
    assert gts.parse_google_trends_rss("<rss><channel></channel></rss>", "US") == []
    assert gts.parse_google_trends_rss("<not xml", "US") == []


# =========================================================================== #
# 4. Bluesky
# =========================================================================== #

def test_parse_bluesky():
    items = gts.parse_bluesky(BLUESKY_JSON)
    assert len(items) == 2               # empty displayName + non-dict skipped
    for it in items:
        _assert_raw_item(it)
    a, b = items
    assert a["source"] == "bluesky"
    assert a["source_key"] == "cbd98b40f918"
    assert a["title"] == "Ossoff criticizes Trump's work ethic"
    assert a["url"] == "https://bsky.app/profile/did:plc:qrz3lhbyuxbeilrc6nekdqme/feed/cbd98b40f918"
    assert a["summary"].startswith("Ossoff says Trump")
    assert a["metric_value"] == 3801.0 and a["metric_label"] == "posts"
    assert a["published_at"] == "2026-08-16T23:00:01.075981+00:00"
    assert a["region"] == "global"
    assert a["category"] == "politics"
    assert a["extra"]["status"] == "trending"
    assert a["image_url"] is None
    assert b["published_at"] == "2026-08-17T03:19:40.689102+00:00"   # 'Z' handled
    assert b["category"] == "entertainment"


def test_parse_bluesky_junk():
    assert gts.parse_bluesky({}) == []
    assert gts.parse_bluesky({"trends": "nope"}) == []
    assert gts.parse_bluesky(None) == []
    assert gts.parse_bluesky([]) == []


# =========================================================================== #
# 5. Reddit
# =========================================================================== #

def test_parse_reddit_atom():
    items = gts.parse_reddit_atom(REDDIT_ATOM, "r/memes")
    assert len(items) == 2               # the no-title entry is skipped
    for it in items:
        _assert_raw_item(it)
    a, b = items
    assert a["source"] == "reddit"
    assert a["source_key"] == "t3_1vrx67n"
    assert a["title"] == "How to upset Turkish people"
    assert a["url"] == "https://www.reddit.com/r/memes/comments/1vrx67n/how_to_upset_turkish_people/"
    assert a["image_url"] == "https://preview.redd.it/w95kis2mc6kh1.jpeg?width=640&crop=smart"
    assert a["summary"] == ""            # image post: boilerplate stripped to nothing
    assert a["published_at"] == "2026-08-18T18:12:04+00:00"
    assert a["region"] == "global"
    assert a["metric_value"] is None and a["metric_label"] is None
    assert a["extra"]["sub"] == "memes"
    assert a["extra"]["author"] == "/u/Frequent-Loquat1941"
    # text post: no thumbnail, content stripped to the actual text
    assert b["source_key"] == "t3_1vrabc"
    assert b["image_url"] is None
    assert b["summary"].startswith("What is going on with the nihilist penguin memes?")
    assert "submitted by" not in b["summary"]
    assert "[link]" not in b["summary"] and "[comments]" not in b["summary"]
    assert b["published_at"] == "2026-08-18T16:28:57+00:00"


def test_parse_reddit_empty_feed():
    assert gts.parse_reddit_atom(
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>rising</title></feed>', "memes") == []
    assert gts.parse_reddit_atom("", "memes") == []


# =========================================================================== #
# 6. Know Your Meme
# =========================================================================== #

def test_parse_kym_rss():
    items = gts.parse_kym_rss(KYM_RSS)
    assert len(items) == 2
    for it in items:
        _assert_raw_item(it)
    a, b = items
    assert a["source"] == "kym"
    assert a["source_key"] == "matcha-filter"
    assert a["url"] == "https://knowyourmeme.com/memes/matcha-filter"
    assert a["image_url"] == "https://i.kym-cdn.com/entries/icons/mobile/000/057/309/matchafiltercover.jpg"
    assert a["summary"].startswith("The Matcha Filter or Wild Heart Whisper Filter")
    assert "<p>" not in a["summary"] and "<img" not in a["summary"]
    assert a["published_at"] == "2026-08-18T16:46:01+00:00"
    assert a["region"] == "global"
    assert a["extra"]["guid"] == "post:95621"
    # entry without an <img>
    assert b["source_key"] == "miguels-chopped-haircut-in-coco-2-miguel-taper-fade"
    assert b["image_url"] is None
    assert b["summary"].startswith("Miguel's Chopped Haircut in Coco 2")


def test_parse_kym_empty():
    assert gts.parse_kym_rss("<rss><channel/></rss>") == []
    assert gts.parse_kym_rss("") == []


# =========================================================================== #
# 7. Wikipedia
# =========================================================================== #

def test_parse_wikipedia_top_filters_and_rising_only():
    items = gts.parse_wikipedia_top(WIKI_TODAY, WIKI_YESTERDAY)
    keys = [it["source_key"] for it in items]
    # housekeeping gone
    for bad in ("Main_Page", "Special:Search", "Wikipedia:Featured_pictures", "File:Something.jpg",
                "Portal:Current_events", "Help:Contents", "Talk:Foo", "-"):
        assert bad not in keys
    # falling / flat gone, rising + new kept
    assert "Deaths_in_2026" not in keys
    assert "Spider-Man:_Brand_New_Day" not in keys
    assert keys == ["Hayden_Panettiere", "Wladimir_Klitschko", "Nihilist_Penguin"]
    for it in items:
        _assert_raw_item(it)
    a = items[0]
    assert a["title"] == "Hayden Panettiere"
    assert a["url"] == "https://en.wikipedia.org/wiki/Hayden_Panettiere"
    assert a["metric_value"] == 10392637.0 and a["metric_label"] == "views"
    assert a["extra"]["rank"] == 1
    assert a["extra"]["views_yesterday"] == 12000.0
    assert a["extra"]["views_delta"] == 10392637.0 - 12000.0
    assert a["published_at"] == "2026-08-17T00:00:00+00:00"
    assert a["region"] == "global"
    assert "10,392,637 views" in a["summary"]
    new = items[2]
    assert new["extra"]["views_yesterday"] == 0.0     # absent yesterday ⇒ rising from 0
    assert new["extra"]["views_delta"] == 50000.0


def test_parse_wikipedia_top_missing_yesterday_keeps_all_non_housekeeping():
    items = gts.parse_wikipedia_top(WIKI_TODAY, None)
    keys = [it["source_key"] for it in items]
    assert "Main_Page" not in keys and "Special:Search" not in keys
    assert "Deaths_in_2026" in keys           # can't compare ⇒ kept
    assert "Spider-Man:_Brand_New_Day" in keys
    assert all(it["extra"]["views_delta"] is None for it in items)
    assert items[0]["url"] == "https://en.wikipedia.org/wiki/Hayden_Panettiere"
    # empty / junk payloads
    assert gts.parse_wikipedia_top({}, None) == []
    assert gts.parse_wikipedia_top({"items": []}, WIKI_YESTERDAY) == []
    assert gts.parse_wikipedia_top(None, None) == []


# =========================================================================== #
# 8. YouTube
# =========================================================================== #

def test_parse_youtube():
    items = gts.parse_youtube(YOUTUBE_JSON, "ae")
    assert len(items) == 2               # the id-less video is skipped
    for it in items:
        _assert_raw_item(it)
    a, b = items
    assert a["source"] == "youtube"
    assert a["source_key"] == "dQw4w9WgXcQ"
    assert a["title"] == "I built a game in 42 hours"
    assert a["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert a["image_url"] == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"   # high preferred
    assert a["metric_value"] == 1234567.0 and a["metric_label"] == "views"
    assert a["published_at"] == "2026-08-17T15:00:12+00:00"
    assert a["region"] == "AE"
    assert a["category"] == "gaming"
    assert a["summary"] == "Chaos. More chaos."
    assert a["extra"]["channel"] == "Some Creator"
    assert a["extra"]["tags"] == ["gamedev", "42"]
    # fallbacks: medium thumb, unknown category id kept raw, no stats
    assert b["image_url"] == "https://i.ytimg.com/vi/abc123/mqdefault.jpg"
    assert b["category"] == "999"
    assert b["metric_value"] is None


def test_parse_youtube_junk():
    assert gts.parse_youtube({}, "US") == []
    assert gts.parse_youtube({"items": None}, "US") == []
    assert gts.parse_youtube(None, "US") == []


# =========================================================================== #
# 9. trends24 (X)
# =========================================================================== #

def test_parse_trends24_first_list_only_and_dedupe():
    items = gts.parse_trends24(TRENDS24_HTML, "us")
    titles = [it["title"] for it in items]
    assert titles == ["GTA 6", "Dune", "#NBA2K27"]      # duplicate Dune collapsed
    assert "Older Trend" not in titles                  # second (older) list ignored
    for it in items:
        _assert_raw_item(it)
    a, dune = items[0], items[1]
    assert a["source"] == "x_trends"
    assert a["source_key"] == "US:GTA 6"
    assert a["url"] == "https://twitter.com/search?q=GTA%206"
    assert a["metric_value"] is None and a["metric_label"] == "tweets"
    assert a["region"] == "US"
    assert a["published_at"] == "2026-08-18T19:24:40.333000+00:00"   # from data-timestamp
    assert a["extra"]["rank"] == 1
    assert dune["metric_value"] == 52400.0
    assert dune["extra"]["rank"] == 2


def test_parse_trends24_no_list():
    assert gts.parse_trends24("<html><body><p>nothing here</p></body></html>", "US") == []
    assert gts.parse_trends24("", "US") == []


# =========================================================================== #
# 10. Fetchers over a fake client
# =========================================================================== #

class _FakeResponse:
    def __init__(self, status: int = 200, *, text: str = "", payload=None, headers=None):
        self.status_code = status
        self._text = text
        self._payload = payload
        self.headers = httpx.Headers(headers or {})
        self.request = httpx.Request("GET", "https://example.test/x")

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        if self._payload is None:
            return json.loads(self._text)
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request,
                response=httpx.Response(self.status_code, request=self.request))


class _FakeClient:
    """Records every GET; answers from a routing function."""

    def __init__(self, route):
        self.route = route
        self.calls: list[dict] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return self.route(url, params or {}, headers or {})


def test_fetch_google_trends_passes_geo_and_parses():
    client = _FakeClient(lambda u, p, h: _FakeResponse(text=GOOGLE_RSS))
    items = _run(gts.fetch_google_trends(client, "ae"))
    assert client.calls[0]["url"] == gts.GOOGLE_TRENDS_URL
    assert client.calls[0]["params"] == {"geo": "AE"}
    assert [it["region"] for it in items] == ["AE", "AE"]
    assert items[0]["source_key"] == "AE:coco gauff"


def test_fetch_google_trends_raises_on_http_error():
    client = _FakeClient(lambda u, p, h: _FakeResponse(503))
    with pytest.raises(httpx.HTTPStatusError):
        _run(gts.fetch_google_trends(client, "US"))


def test_fetch_bluesky_and_kym():
    c1 = _FakeClient(lambda u, p, h: _FakeResponse(payload=BLUESKY_JSON))
    items = _run(gts.fetch_bluesky(c1))
    assert c1.calls[0]["url"] == gts.BLUESKY_TRENDS_URL
    assert c1.calls[0]["params"]["limit"] == 25
    assert len(items) == 2
    c2 = _FakeClient(lambda u, p, h: _FakeResponse(text=KYM_RSS))
    assert len(_run(gts.fetch_kym(c2))) == 2
    assert c2.calls[0]["url"] == gts.KYM_NEWSFEED_URL


def test_fetch_reddit_sends_bot_ua_and_uses_top_of_day_rss_not_json():
    client = _FakeClient(lambda u, p, h: _FakeResponse(text=REDDIT_ATOM))
    items = _run(gts.fetch_reddit(client, "memes"))
    call = client.calls[0]
    assert call["url"] == "https://www.reddit.com/r/memes/top.rss"
    assert ".json" not in call["url"]
    assert call["headers"]["User-Agent"] == gts.REDDIT_UA
    assert call["params"] == {"t": "day", "limit": 25}   # t=day, not /rising
    assert len(items) == 2 and items[0]["extra"]["sub"] == "memes"
    with pytest.raises(ValueError):
        _run(gts.fetch_reddit(client, ""))


def test_fetch_wikipedia_uses_d1_vs_d2():
    def route(url, p, h):
        if url.endswith("/2026/08/18"):
            return _FakeResponse(payload=WIKI_TODAY)
        if url.endswith("/2026/08/17"):
            return _FakeResponse(payload=WIKI_YESTERDAY)
        return _FakeResponse(404)
    client = _FakeClient(route)
    items = _run(gts.fetch_wikipedia(client, today=date(2026, 8, 19)))
    assert [c["url"][-10:] for c in client.calls] == ["2026/08/18", "2026/08/17"]
    assert [it["source_key"] for it in items] == [
        "Hayden_Panettiere", "Wladimir_Klitschko", "Nihilist_Penguin"]


def test_fetch_wikipedia_falls_back_to_d2_vs_d3_when_d1_is_404():
    def route(url, p, h):
        if url.endswith("/2026/08/18"):
            return _FakeResponse(404)             # today's data hasn't landed yet
        if url.endswith("/2026/08/17"):
            return _FakeResponse(payload=WIKI_TODAY)
        if url.endswith("/2026/08/16"):
            return _FakeResponse(payload=WIKI_YESTERDAY)
        return _FakeResponse(404)
    client = _FakeClient(route)
    items = _run(gts.fetch_wikipedia(client, today=date(2026, 8, 19)))
    assert [c["url"][-10:] for c in client.calls] == ["2026/08/18", "2026/08/17", "2026/08/16"]
    assert len(items) == 3


def test_fetch_wikipedia_missing_previous_day_degrades_to_unfiltered():
    def route(url, p, h):
        if url.endswith("/2026/08/18"):
            return _FakeResponse(payload=WIKI_TODAY)
        return _FakeResponse(404)
    items = _run(gts.fetch_wikipedia(_FakeClient(route), today=date(2026, 8, 19)))
    keys = [it["source_key"] for it in items]
    assert "Deaths_in_2026" in keys and "Main_Page" not in keys


def test_fetch_wikipedia_raises_when_both_days_404():
    client = _FakeClient(lambda u, p, h: _FakeResponse(404))
    with pytest.raises(httpx.HTTPStatusError):
        _run(gts.fetch_wikipedia(client, today=date(2026, 8, 19)))


def test_fetch_youtube_requires_key():
    client = _FakeClient(lambda u, p, h: _FakeResponse(payload=YOUTUBE_JSON))
    with pytest.raises(ValueError):
        _run(gts.fetch_youtube(client, "US", ""))
    assert client.calls == []            # never hit the network without a key
    items = _run(gts.fetch_youtube(client, "us", "KEY123"))
    call = client.calls[0]
    assert call["url"] == gts.YOUTUBE_VIDEOS_URL
    assert call["params"]["chart"] == "mostPopular"
    assert call["params"]["regionCode"] == "US"
    assert call["params"]["key"] == "KEY123"
    assert call["params"]["part"] == "snippet,statistics"
    assert len(items) == 2 and items[0]["region"] == "US"


def test_fetch_trends24_slug_and_unknown_geo():
    client = _FakeClient(lambda u, p, h: _FakeResponse(text=TRENDS24_HTML))
    items = _run(gts.fetch_trends24(client, "ae"))
    assert client.calls[0]["url"] == "https://trends24.in/united-arab-emirates/"
    assert items[0]["source_key"] == "AE:GTA 6"
    with pytest.raises(ValueError):
        _run(gts.fetch_trends24(client, "ZZ"))


# =========================================================================== #
# 11. build_fetch_plan
# =========================================================================== #

def test_build_fetch_plan_full():
    plan = gts.build_fetch_plan(
        sources=list(gts.SOURCE_IDS), geos=["US", "ae", "ZZ"],
        subreddits=["memes", "r/videos"], youtube_api_key="KEY")
    units = [(u["source"], u["region"], u["unit"]) for u in plan]
    assert ("google_trends", "US", "google_trends:US") in units
    assert ("google_trends", "AE", "google_trends:AE") in units
    assert ("google_trends", "ZZ", "google_trends:ZZ") in units   # google accepts any geo
    assert ("youtube", "US", "youtube:US") in units
    assert ("youtube", "ZZ", "youtube:ZZ") in units
    assert ("x_trends", "US", "x_trends:US") in units
    assert ("x_trends", "AE", "x_trends:AE") in units
    assert ("x_trends", "ZZ", "x_trends:ZZ") not in units          # no trends24 slug
    assert ("reddit", "global", "reddit:memes") in units
    assert ("reddit", "global", "reddit:videos") in units          # "r/" prefix stripped
    assert ("bluesky", "global", "bluesky") in units
    assert ("kym", "global", "kym") in units
    assert ("wikipedia", "global", "wikipedia") in units
    for u in plan:
        assert set(u) >= {"source", "label", "region", "unit", "params"}
        assert u["label"] == gts.SOURCE_LABELS[u["source"]]
    yt = [u for u in plan if u["source"] == "youtube"][0]
    assert yt["params"] == {"region": "US", "api_key": "KEY"}


def test_unknown_source_id_is_ignored():
    plan = gts.build_fetch_plan(
        sources=["youtube", "google_trends", "myspace_pulse"],
        geos=["US"], subreddits=[], youtube_api_key="")
    assert [u["unit"] for u in plan] == ["google_trends:US"]
    assert gts.build_fetch_plan(sources=[], geos=["US"], subreddits=[], youtube_api_key="k") == []
    # dedupe + blanks
    plan = gts.build_fetch_plan(
        sources=["reddit", "reddit"], geos=[], subreddits=["memes", "", "memes"], youtube_api_key="")
    assert [u["unit"] for u in plan] == ["reddit:memes"]


def test_unconfigured_sources_emit_no_units_instead_of_failing_at_fetch_time():
    """youtube's "no key ⇒ no unit" pattern, now shared by tiktok/instagram: a
    source with nothing configured must vanish from the plan, not blow up mid-run."""
    plan = gts.build_fetch_plan(
        sources=["youtube", "tiktok", "instagram", "instagram_watch"],
        geos=["US"], subreddits=[], youtube_api_key="",
        tiktok_regions=[], ig_hashtags=[], ig_watch_accounts=[])
    assert plan == []
    # ...and None (the default) behaves exactly like an empty list
    assert gts.build_fetch_plan(
        sources=["tiktok", "instagram", "instagram_watch"],
        geos=["US"], subreddits=[], youtube_api_key="") == []


# =========================================================================== #
# 12. fetch_all — the monkeypatch seam
# =========================================================================== #

def _plan_for(*sources: str, geos=("US", "AE"), subs=("memes",), key="KEY"):
    return gts.build_fetch_plan(
        sources=list(sources), geos=list(geos), subreddits=list(subs), youtube_api_key=key)


def test_fetch_all_gathers_items_and_stats(monkeypatch):
    calls: list[str] = []

    async def fake_google(client, geo):
        calls.append(f"g:{geo}")
        return gts.parse_google_trends_rss(GOOGLE_RSS, geo)

    async def fake_bsky(client, limit=25):
        calls.append("b")
        return gts.parse_bluesky(BLUESKY_JSON)

    async def fake_reddit(client, sub, limit=25):
        calls.append(f"r:{sub}")
        return gts.parse_reddit_atom(REDDIT_ATOM, sub)

    monkeypatch.setattr(gts, "fetch_google_trends", fake_google)
    monkeypatch.setattr(gts, "fetch_bluesky", fake_bsky)
    monkeypatch.setattr(gts, "fetch_reddit", fake_reddit)

    plan = _plan_for("google_trends", "bluesky", "reddit")
    items, stats = _run(gts.fetch_all(plan, client=object()))   # a client is required by signature only
    assert sorted(calls) == ["b", "g:AE", "g:US", "r:memes"]
    assert len(items) == 2 + 2 + 2 + 2
    for it in items:
        _assert_raw_item(it)
    assert set(stats) == {"google_trends", "bluesky", "reddit"}
    assert stats["google_trends"] == {"ok": True, "count": 4, "error": None, "ms": stats["google_trends"]["ms"]}
    assert stats["bluesky"]["ok"] is True and stats["bluesky"]["count"] == 2
    assert stats["reddit"]["count"] == 2 and stats["reddit"]["error"] is None
    for st in stats.values():
        assert set(st) == {"ok", "count", "error", "ms"}
        assert isinstance(st["ms"], int) and st["ms"] >= 0
    # plan order preserved
    assert [it["source"] for it in items] == ["google_trends"] * 4 + ["bluesky"] * 2 + ["reddit"] * 2


def test_fetch_all_records_failures_and_continues(monkeypatch):
    async def fake_google(client, geo):
        if geo == "AE":
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("GET", "https://trends.google.com/x"),
                response=httpx.Response(429, request=httpx.Request("GET", "https://trends.google.com/x")))
        return gts.parse_google_trends_rss(GOOGLE_RSS, geo)

    async def fake_kym(client):
        raise httpx.ConnectTimeout("timed out")

    async def fake_bsky(client, limit=25):
        return gts.parse_bluesky(BLUESKY_JSON)

    monkeypatch.setattr(gts, "fetch_google_trends", fake_google)
    monkeypatch.setattr(gts, "fetch_kym", fake_kym)
    monkeypatch.setattr(gts, "fetch_bluesky", fake_bsky)

    items, stats = _run(gts.fetch_all(_plan_for("google_trends", "kym", "bluesky"), client=object()))
    # google: one unit ok, one failed ⇒ source ok, error recorded
    assert stats["google_trends"]["ok"] is True
    assert stats["google_trends"]["count"] == 2
    assert "google_trends:AE" in stats["google_trends"]["error"]
    assert "429" in stats["google_trends"]["error"]
    # kym: every unit failed ⇒ not ok, zero items, timeout named
    assert stats["kym"] == {"ok": False, "count": 0, "error": stats["kym"]["error"], "ms": stats["kym"]["ms"]}
    assert "timeout" in stats["kym"]["error"].lower()
    # bluesky untouched by the others' failures
    assert stats["bluesky"]["ok"] is True and stats["bluesky"]["count"] == 2
    assert len(items) == 4


def test_fetch_all_caps_per_unit_and_drops_empty_titles(monkeypatch):
    async def fake_google(client, geo):
        many = [gts.make_item(source="google_trends", source_key=f"{geo}:{i}", title=f"t{i}", region=geo)
                for i in range(50)]
        many.append(gts.make_item(source="google_trends", source_key=f"{geo}:blank", title="  ", region=geo))
        return many

    monkeypatch.setattr(gts, "fetch_google_trends", fake_google)
    items, stats = _run(gts.fetch_all(
        _plan_for("google_trends", geos=("US",)), client=object(), max_items_per_unit=7))
    assert len(items) == 7
    assert stats["google_trends"]["count"] == 7
    assert all(it["title"].strip() for it in items)


def test_fetch_all_respects_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def slow(client, geo):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [gts.make_item(source="google_trends", source_key=f"{geo}:x", title="x", region=geo)]

    monkeypatch.setattr(gts, "fetch_google_trends", slow)
    plan = _plan_for("google_trends", geos=tuple(f"G{i}" for i in range(10)))
    items, stats = _run(gts.fetch_all(plan, client=object(), concurrency=2))
    assert len(items) == 10
    assert peak <= 2
    assert stats["google_trends"]["count"] == 10


def test_fetch_all_youtube_skipped_without_key(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("youtube must not be fetched without a key")

    monkeypatch.setattr(gts, "fetch_youtube", never)
    plan = _plan_for("youtube", key="")
    assert plan == []
    items, stats = _run(gts.fetch_all(plan, client=object()))
    assert items == [] and stats == {}


def test_fetch_all_dispatches_youtube_and_trends24_with_params(monkeypatch):
    seen: list[tuple] = []

    async def fake_yt(client, region, api_key):
        seen.append(("yt", region, api_key))
        return gts.parse_youtube(YOUTUBE_JSON, region)

    async def fake_t24(client, geo):
        seen.append(("t24", geo))
        return gts.parse_trends24(TRENDS24_HTML, geo)

    async def fake_wiki(client, *, today=None):
        seen.append(("wiki", today))
        return gts.parse_wikipedia_top(WIKI_TODAY, WIKI_YESTERDAY)

    monkeypatch.setattr(gts, "fetch_youtube", fake_yt)
    monkeypatch.setattr(gts, "fetch_trends24", fake_t24)
    monkeypatch.setattr(gts, "fetch_wikipedia", fake_wiki)
    items, stats = _run(gts.fetch_all(
        _plan_for("youtube", "x_trends", "wikipedia", geos=("US", "AE", "ZZ"), key="K"), client=object()))
    assert ("yt", "US", "K") in seen and ("yt", "AE", "K") in seen and ("yt", "ZZ", "K") in seen
    assert ("t24", "US") in seen and ("t24", "AE") in seen
    assert ("t24", "ZZ") not in seen
    assert ("wiki", None) in seen
    assert stats["youtube"]["count"] == 6
    assert stats["x_trends"]["count"] == 6
    assert stats["wikipedia"]["count"] == 3
    assert len(items) == 15


def test_default_client_uses_config_user_agent():
    from app.config import config
    client = gts.default_client()
    try:
        assert client.headers["User-Agent"] == config.user_agent
        assert client.follow_redirects is True
    finally:
        _run(client.aclose())



# --------------------------------------------------------------------------- #
# fetch_all: rate-limited hosts get one lane + a 429 retry
# --------------------------------------------------------------------------- #

def _http_429(url: str = "https://www.reddit.com/r/memes/top.rss", retry_after: str | None = None):
    req = httpx.Request("GET", url)
    headers = {"retry-after": retry_after} if retry_after else {}
    resp = httpx.Response(429, request=req, headers=headers)
    return httpx.HTTPStatusError("429", request=req, response=resp)


def test_fetch_all_runs_reddit_units_one_at_a_time_with_a_pause(monkeypatch):
    """REGRESSION: the first live run pulled 7 subreddits concurrently and Reddit
    answered 429 to six of them. Reddit is a SERIAL source: one lane, polite pause."""
    active = 0
    peak = 0
    slept: list[float] = []

    async def reddit(client, sub, limit=25):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [gts.make_item(source="reddit", source_key=f"t3_{sub}", title=sub)]

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gts, "fetch_reddit", reddit)
    monkeypatch.setattr(gts, "_sleep", fake_sleep)
    plan = gts.build_fetch_plan(sources=["reddit"], geos=[], subreddits=["a", "b", "c", "d"], youtube_api_key="")
    items, stats = _run(gts.fetch_all(plan, client=object(), concurrency=6))
    assert len(items) == 4
    assert peak == 1, "reddit units must not overlap"
    assert slept == [gts.SERIAL_UNIT_DELAY_SEC] * 4
    assert stats["reddit"] == {"ok": True, "count": 4, "error": None, "ms": stats["reddit"]["ms"]}


def test_fetch_all_retries_a_429_and_honours_retry_after(monkeypatch):
    calls: list[str] = []
    slept: list[float] = []

    async def reddit(client, sub, limit=25):
        calls.append(sub)
        if len(calls) == 1:
            raise _http_429(retry_after="7")
        return [gts.make_item(source="reddit", source_key="t3_ok", title="ok")]

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gts, "fetch_reddit", reddit)
    monkeypatch.setattr(gts, "_sleep", fake_sleep)
    plan = gts.build_fetch_plan(sources=["reddit"], geos=[], subreddits=["memes"], youtube_api_key="")
    items, stats = _run(gts.fetch_all(plan, client=object()))
    assert [it["title"] for it in items] == ["ok"]
    assert calls == ["memes", "memes"]
    assert slept[0] == 7.0                       # Retry-After wins over the default backoff
    assert stats["reddit"]["ok"] is True and stats["reddit"]["error"] is None


def test_fetch_all_gives_up_after_the_retry_budget_and_records_the_429(monkeypatch):
    calls = 0
    slept: list[float] = []

    async def reddit(client, sub, limit=25):
        nonlocal calls
        calls += 1
        raise _http_429()

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gts, "fetch_reddit", reddit)
    monkeypatch.setattr(gts, "_sleep", fake_sleep)
    plan = gts.build_fetch_plan(sources=["reddit"], geos=[], subreddits=["memes"], youtube_api_key="")
    items, stats = _run(gts.fetch_all(plan, client=object()))
    assert items == []
    assert calls == 1 + gts.RATE_LIMIT_RETRIES
    assert slept[:gts.RATE_LIMIT_RETRIES] == [gts.RATE_LIMIT_BACKOFF_SEC] * gts.RATE_LIMIT_RETRIES
    assert stats["reddit"]["ok"] is False
    assert "HTTP 429" in (stats["reddit"]["error"] or "")


def test_non_429_errors_are_not_retried(monkeypatch):
    calls = 0

    async def google(client, geo):
        nonlocal calls
        calls += 1
        req = httpx.Request("GET", "https://trends.google.com/x")
        raise httpx.HTTPStatusError("503", request=req, response=httpx.Response(503, request=req))

    monkeypatch.setattr(gts, "fetch_google_trends", google)
    plan = gts.build_fetch_plan(sources=["google_trends"], geos=["US"], subreddits=[], youtube_api_key="")
    items, stats = _run(gts.fetch_all(plan, client=object()))
    assert calls == 1 and items == [] and stats["google_trends"]["ok"] is False


def test_retry_after_is_clamped_and_tolerates_garbage():
    assert gts._retry_after_seconds(_http_429(retry_after="999")) == gts.RATE_LIMIT_MAX_WAIT_SEC
    assert gts._retry_after_seconds(_http_429(retry_after="soon")) == gts.RATE_LIMIT_BACKOFF_SEC
    assert gts._retry_after_seconds(_http_429()) == gts.RATE_LIMIT_BACKOFF_SEC



# --------------------------------------------------------------------------- #
# fetch_reddit: paces itself off Reddit's rate-limit headers
# --------------------------------------------------------------------------- #

def _reddit_atom(title: str = "post") -> str:
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_abc</id>'
        f'<title>{title}</title><link href="https://www.reddit.com/r/memes/comments/abc/x/"/>'
        '<published>2026-08-18T18:12:04+00:00</published></entry></feed>'
    )


class _SeqClient:
    """Returns the queued responses in order and records the calls."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self._responses.pop(0)


def test_reddit_wait_from_headers():
    ok_full = {"x-ratelimit-remaining": "9", "x-ratelimit-reset": "40"}
    ok_spent = {"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "34"}
    assert gts._reddit_wait_from_headers(httpx.Headers(ok_full), status_code=200) == 0.0
    assert gts._reddit_wait_from_headers(httpx.Headers(ok_spent), status_code=200) == 35.0
    assert gts._reddit_wait_from_headers(httpx.Headers({"x-ratelimit-reset": "17"}), status_code=429) == 18.0
    assert gts._reddit_wait_from_headers(httpx.Headers({"retry-after": "9"}), status_code=429) == 9.0
    assert gts._reddit_wait_from_headers(httpx.Headers({}), status_code=429) == gts.RATE_LIMIT_BACKOFF_SEC
    assert gts._reddit_wait_from_headers(httpx.Headers({"x-ratelimit-reset": "9999"}), status_code=429) == gts.REDDIT_MAX_WAIT_SEC
    assert gts._reddit_wait_from_headers(httpx.Headers({"x-ratelimit-remaining": "junk"}), status_code=200) == 0.0


def test_fetch_reddit_waits_for_the_window_before_the_next_call(monkeypatch):
    """REGRESSION: live Reddit answers remaining=0 / reset=N after ONE anonymous
    request; the next subreddit must wait N+1s or it 429s."""
    slept: list[float] = []
    now = [1000.0]

    async def fake_sleep(sec):
        slept.append(round(sec, 1)); now[0] += sec
    monkeypatch.setattr(gts, "_sleep", fake_sleep)
    monkeypatch.setattr(gts, "_monotonic", lambda: now[0])
    monkeypatch.setattr(gts, "_reddit_next_allowed_at", 0.0)

    client = _SeqClient([
        _FakeResponse(200, text=_reddit_atom("one"), headers={"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "34"}),
        _FakeResponse(200, text=_reddit_atom("two"), headers={"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "60"}),
    ])
    a = _run(gts.fetch_reddit(client, "memes"))
    b = _run(gts.fetch_reddit(client, "videos"))
    assert [it["title"] for it in a + b] == ["one", "two"]
    assert slept == [35.0]                       # paced exactly reset+1 before the second sub
    assert len(client.calls) == 2


def test_fetch_reddit_retries_a_429_after_the_reset_and_then_gives_up(monkeypatch):
    slept: list[float] = []
    now = [0.0]

    async def fake_sleep(sec):
        slept.append(round(sec, 1)); now[0] += sec
    monkeypatch.setattr(gts, "_sleep", fake_sleep)
    monkeypatch.setattr(gts, "_monotonic", lambda: now[0])
    monkeypatch.setattr(gts, "_reddit_next_allowed_at", 0.0)

    # 429 → wait reset+1 → 200
    client = _SeqClient([
        _FakeResponse(429, headers={"x-ratelimit-reset": "17"}),
        _FakeResponse(200, text=_reddit_atom("late"), headers={}),
    ])
    items = _run(gts.fetch_reddit(client, "memes"))
    assert [it["title"] for it in items] == ["late"] and slept == [18.0]

    # every attempt 429s → raises, marked so fetch_all does not retry it AGAIN
    client = _SeqClient([_FakeResponse(429, headers={"retry-after": "2"})] * gts.REDDIT_ATTEMPTS)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _run(gts.fetch_reddit(client, "memes"))
    assert getattr(ei.value, "_gt_no_retry", False) is True
    assert len(client.calls) == gts.REDDIT_ATTEMPTS

    # ...and fetch_all honours that marker (one dispatch, recorded as a failure)
    calls = 0
    async def reddit(c, sub, limit=25):
        nonlocal calls; calls += 1
        exc = _http_429(); setattr(exc, "_gt_no_retry", True); raise exc
    monkeypatch.setattr(gts, "fetch_reddit", reddit)
    plan = gts.build_fetch_plan(sources=["reddit"], geos=[], subreddits=["memes"], youtube_api_key="")
    _items, stats = _run(gts.fetch_all(plan, client=object()))
    assert calls == 1 and stats["reddit"]["ok"] is False

# =========================================================================== #
# 14. The 2026-08-20 sources — fixtures
#
# Every fixture below is a TRIMMED capture of a response fetched live from this
# machine on 2026-08-20 (status codes and item counts are in the agent report):
#   hacker_news  HTTP 200, 20 hits, nbHits 185
#   lemmy        HTTP 200, 50 posts
#   github       HTTP 200, 30 items (x-ratelimit-limit 10/min keyless)
#   google_news  HTTP 200, 100 items, 147 kB
#   tiktok       HTTP 200, 73.7 kB (US) / 74.9 kB (AE), 3 rows each
# =========================================================================== #

HN_JSON = {
    "hits": [
        {
            "title": "A joke domain purchase turned in geopolitical warfare",
            "url": "https://sprocketfox.io/xssfox/2026/08/19/sondehub-and-war/",
            "author": "kareiva", "points": 612, "num_comments": 80,
            "created_at": "2026-08-19T11:21:50Z", "created_at_i": 1787138510,
            "objectID": "49360015", "story_text": None,
            "_tags": ["story", "author_kareiva", "story_49360015", "front_page"],
        },
        {   # an Ask HN self-post: no url, story_text carries the body
            "title": "Ask HN: What are you working on this month?",
            "url": None, "author": "whoishiring", "points": 121, "num_comments": 348,
            "created_at": "2026-08-19T11:46:58Z", "objectID": "49360242",
            "story_text": "<p>Tell us what you built.</p>",
        },
        {"title": "", "objectID": "49360999", "points": 5},          # junk: no title
        {"title": "No id at all", "points": 5},                       # junk: no objectID
    ],
    "nbHits": 185,
}

LEMMY_JSON = {
    "posts": [
        {
            "post": {
                "id": 50832480, "name": "MySpace wasn't better because we were younger",
                "url": "https://sh.itjust.works/pictrs/image/2f0f17df.jpeg",
                "body": "And here we all are building back better on the Fediverse!",
                "published": "2026-08-18T22:21:13.943671Z",
                "thumbnail_url": "https://lemmy.world/pictrs/image/ae3fcbc0.jpeg",
                "ap_id": "https://sh.itjust.works/post/65309900",
                "nsfw": False, "deleted": False, "removed": False,
            },
            "creator": {"id": 30039, "name": "thal3s", "display_name": "Thales"},
            "community": {"id": 79185, "name": "microblogmemes", "title": "Microblog Memes",
                          "nsfw": False},
            "counts": {"post_id": 50832480, "comments": 75, "score": 1203,
                       "upvotes": 1207, "downvotes": 4,
                       "published": "2026-08-18T22:21:13.943671Z"},
        },
        {   # the source labels its own NSFW — free, before a single token is spent
            "post": {"id": 50899001, "name": "Definitely not safe for work",
                     "published": "2026-08-19T01:00:00Z", "nsfw": True},
            "community": {"id": 1, "name": "nsfwmemes", "title": "NSFW", "nsfw": True},
            "counts": {"score": 9000, "comments": 12},
        },
        {   # a post removed by a mod
            "post": {"id": 50899002, "name": "Removed by moderator",
                     "published": "2026-08-19T01:00:00Z", "removed": True},
            "community": {"id": 2, "name": "memes", "nsfw": False},
            "counts": {"score": 10, "comments": 0},
        },
        {"post": {"id": 3, "name": ""}, "counts": {}},                # junk: no title
        "not a dict",                                                  # junk
    ],
}

GITHUB_JSON = {
    "total_count": 4516298,
    "items": [
        {
            "full_name": "deepseek-ai/deepseek-harness",
            "html_url": "https://github.com/deepseek-ai/deepseek-harness",
            "description": "DeepSeek Harness: Everything is a Plugin.",
            "stargazers_count": 166835, "forks_count": 17796, "watchers_count": 166835,
            "open_issues_count": 412, "language": "TypeScript",
            "created_at": "2026-08-13T11:56:32Z",
            "topics": ["ai-agents", "cordis", "dsh", "dsh-plugin"],
            "owner": {"login": "deepseek-ai"},
        },
        {
            "full_name": "guillaumemeyer/watermarks-remover",
            "html_url": "https://github.com/guillaumemeyer/watermarks-remover",
            "description": None,
            "stargazers_count": 15241, "forks_count": 1702, "language": None,
            "created_at": "2026-08-11T16:32:38Z", "topics": [],
            "owner": {"login": "guillaumemeyer"},
        },
        {"html_url": "https://github.com/x/y"},          # junk: no full_name
    ],
}

GNEWS_RSS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<title>"(UAE OR "Abu Dhabi") (tech OR AI OR startup) when:7d" - Google News</title>
<lastBuildDate>Wed, 19 Aug 2026 21:03:19 GMT</lastBuildDate>
<item><title>UAE is leading the way in practical AI and robotics adoption: Gecko Robotics - CNBC</title>\
<link>https://news.google.com/rss/articles/CBMipAFBVV95cUxNWldDS3l0?oc=5</link>\
<guid isPermaLink="false">CBMipAFBVV95cUxNWldDS3l0</guid>\
<pubDate>Mon, 17 Aug 2026 07:28:00 GMT</pubDate>\
<description>&lt;a href="https://news.google.com/rss/articles/CBMipAFBVV95cUxNWldDS3l0?oc=5" target="_blank"&gt;\
UAE is leading the way in practical AI and robotics adoption&lt;/a&gt;&amp;nbsp;&amp;nbsp;\
&lt;font color="#6f6f6f"&gt;CNBC&lt;/font&gt;</description>\
<source url="https://www.cnbc.com">CNBC</source></item>
<item><title>Abu Dhabi Exchange Opens Live Market Data to AI Platforms - Markets Media</title>\
<link>https://news.google.com/rss/articles/CBMiywFBVV95cUxNM0pWWUlodk83?oc=5</link>\
<guid isPermaLink="false">CBMiywFBVV95cUxNM0pWWUlodk83</guid>\
<pubDate>Tue, 18 Aug 2026 11:55:29 GMT</pubDate>\
<description>&lt;a href="#"&gt;Abu Dhabi Exchange Opens Live Market Data to AI Platforms&lt;/a&gt;</description>\
<source url="https://www.marketsmedia.com">Markets Media</source></item>
<item><title></title><link>https://news.google.com/x</link></item>
</channel></rss>
"""

# Two real Creative Center rows (US, period=7), trimmed to 4 curve points and one
# creator each. The nesting is the real one:
# loaderData[<tab key>].dehydratedState.queries[].state.data.pages[].data[]
_TT_ROWS = [
    {
        "hashtagID": "63554219", "hashtagName": "unicornfrappuccino",
        "industryIDs": ["27000000000"], "publishCnt": "56051", "rankIndex": "1",
        "vv": "205162676",
        "popularityCurve": [
            {"timestamp": "1786752000", "value": 100},
            {"timestamp": "1786838400", "value": 95.58902349209004},
            {"timestamp": "1786924800", "value": 78.90165610597599},
            {"timestamp": "1787011200", "value": 39.45082805298799},
        ],
        "topCreators": [{
            "handleName": "sofia1089", "nickname": "sof", "followedCnt": "32786",
            "countryCode": "US", "creatorRank": "1",
            "avatarURL": "https://p16-common-sign.tiktokcdn.com/x.jpeg?x-expires=1787346000&x-signature=abc",
        }],
    },
    {
        "hashtagID": "113058", "hashtagName": "haydenpanettiere",
        "industryIDs": ["23000000000"], "publishCnt": "36890", "rankIndex": "2",
        "vv": "395637380",
        "popularityCurve": [
            {"timestamp": "1786924800", "value": 25.0},
            {"timestamp": "1787011200", "value": 50.0},
        ],
        "topCreators": [],
    },
    {"hashtagName": "", "hashtagID": "0"},          # junk row: no name
]

TIKTOK_BLOB = {
    "loaderData": {
        "creativeCenter/trends/(tab)/page": {
            "dehydratedState": {
                "queries": [
                    {"queryKey": ["trendsFilterConfig"], "state": {"data": {"regions": []}}},
                    {"queryKey": ["hashtagList"],
                     "state": {"data": {"pages": [{"data": _TT_ROWS}]}}},
                ],
            },
        },
    },
}

TIKTOK_HTML = (
    "<!DOCTYPE html><html><head><title>Trending hashtags</title></head><body>"
    '<div id="root"></div>'
    '<script type="application/json" id="__MODERN_ROUTER_DATA__">'
    + json.dumps(TIKTOK_BLOB)
    + "</script></body></html>"
)

# The exact same page fetched with httpx's default Accept: */* — HTTP 200, but
# the SSR payload is silently absent. This is the trap the Accept header exists
# to avoid and the reason the fetcher raises instead of returning [].
TIKTOK_HTML_NO_PAYLOAD = (
    "<!DOCTYPE html><html><head><title>Trending hashtags</title></head>"
    '<body><div id="root"></div></body></html>'
)


# =========================================================================== #
# 15. Hacker News
# =========================================================================== #

def test_parse_hacker_news():
    items = gts.parse_hacker_news(HN_JSON)
    assert [it["source_key"] for it in items] == ["49360015", "49360242"]
    for it in items:
        _assert_raw_item(it)
    a = items[0]
    assert a["source"] == "hacker_news"
    assert a["title"] == "A joke domain purchase turned in geopolitical warfare"
    assert a["url"] == "https://sprocketfox.io/xssfox/2026/08/19/sondehub-and-war/"
    assert a["metric_value"] == 612.0 and a["metric_label"] == "points"
    assert a["published_at"] == "2026-08-19T11:21:50+00:00"
    assert a["extra"]["comments"] == 80.0
    assert a["extra"]["author"] == "kareiva"
    assert a["extra"]["hn_url"] == "https://news.ycombinator.com/item?id=49360015"
    assert a["extra"]["domain"] == "sprocketfox.io"
    assert "612 points, 80 comments" in a["summary"]
    # an Ask HN with no url falls back to the HN discussion page and keeps the body
    b = items[1]
    assert b["url"] == "https://news.ycombinator.com/item?id=49360242"
    assert b["summary"].startswith("Tell us what you built.")
    assert b["extra"]["domain"] is None


def test_parse_hacker_news_junk():
    assert gts.parse_hacker_news(None) == []
    assert gts.parse_hacker_news({}) == []
    assert gts.parse_hacker_news({"hits": "nope"}) == []
    assert gts.parse_hacker_news({"hits": [None, 1, "x"]}) == []


# =========================================================================== #
# 16. Lemmy
# =========================================================================== #

def test_parse_lemmy_keeps_scores_and_drops_what_the_source_labels_nsfw():
    items = gts.parse_lemmy(LEMMY_JSON)
    assert [it["source_key"] for it in items] == ["50832480"]
    _assert_raw_item(items[0])
    a = items[0]
    assert a["source"] == "lemmy"
    assert a["title"] == "MySpace wasn't better because we were younger"
    assert a["url"] == "https://lemmy.world/post/50832480"
    # the metric Reddit's Atom refuses to give
    assert a["metric_value"] == 1203.0 and a["metric_label"] == "votes"
    assert a["extra"]["comments"] == 75.0
    assert a["extra"]["upvotes"] == 1207.0 and a["extra"]["downvotes"] == 4.0
    assert a["category"] == "microblogmemes"
    assert a["extra"]["community_title"] == "Microblog Memes"
    assert a["extra"]["creator"] == "thal3s"
    assert a["published_at"] == "2026-08-18T22:21:13.943671+00:00"
    assert a["image_url"] == "https://lemmy.world/pictrs/image/ae3fcbc0.jpeg"
    assert a["summary"].startswith("c/microblogmemes · 1,203 votes, 75 comments — ")


def test_parse_lemmy_junk():
    assert gts.parse_lemmy(None) == []
    assert gts.parse_lemmy({}) == []
    assert gts.parse_lemmy({"posts": "nope"}) == []


# =========================================================================== #
# 17. GitHub
# =========================================================================== #

def test_parse_github():
    items = gts.parse_github(GITHUB_JSON)
    assert [it["source_key"] for it in items] == [
        "deepseek-ai/deepseek-harness", "guillaumemeyer/watermarks-remover"]
    for it in items:
        _assert_raw_item(it)
    a = items[0]
    assert a["source"] == "github"
    assert a["title"] == "deepseek-ai/deepseek-harness"
    assert a["url"] == "https://github.com/deepseek-ai/deepseek-harness"
    assert a["metric_value"] == 166835.0 and a["metric_label"] == "stars"
    assert a["published_at"] == "2026-08-13T11:56:32+00:00"
    assert a["category"] == "typescript"
    assert a["extra"]["forks"] == 17796.0
    assert a["extra"]["owner"] == "deepseek-ai"
    assert a["extra"]["topics"] == ["ai-agents", "cordis", "dsh", "dsh-plugin"]
    assert a["summary"].startswith("DeepSeek Harness: Everything is a Plugin. — 166,835 stars")
    # no description, no language ⇒ still a valid item
    b = items[1]
    assert b["category"] is None and b["extra"]["description"] is None
    assert b["summary"] == "15,241 stars since it was created"


def test_parse_github_junk():
    assert gts.parse_github(None) == []
    assert gts.parse_github({}) == []
    assert gts.parse_github({"items": "nope"}) == []


# =========================================================================== #
# 18. Google News
# =========================================================================== #

def test_parse_google_news_rss_strips_the_publisher_suffix():
    items = gts.parse_google_news_rss(GNEWS_RSS, "ae", "(UAE) when:7d")
    assert len(items) == 2
    for it in items:
        _assert_raw_item(it)
    a = items[0]
    assert a["source"] == "google_news"
    # " - CNBC" is stripped: it would otherwise poison every clustering slug
    assert a["title"] == "UAE is leading the way in practical AI and robotics adoption: Gecko Robotics"
    assert a["summary"].startswith("CNBC: UAE is leading the way")
    assert a["source_key"] == "CBMipAFBVV95cUxNWldDS3l0"
    assert a["url"].startswith("https://news.google.com/rss/articles/")
    assert a["published_at"] == "2026-08-17T07:28:00+00:00"
    assert a["region"] == "AE"
    assert a["extra"]["news_source"] == "CNBC"
    assert a["extra"]["news_source_url"] == "https://www.cnbc.com"
    assert a["extra"]["query"] == "(UAE) when:7d"
    assert a["metric_value"] is None      # honest: Google News ships no counter
    assert items[1]["title"] == "Abu Dhabi Exchange Opens Live Market Data to AI Platforms"


def test_parse_google_news_rss_junk():
    assert gts.parse_google_news_rss("", "AE") == []
    assert gts.parse_google_news_rss("<not xml", "AE") == []
    assert gts.parse_google_news_rss("<rss><channel></channel></rss>", "AE") == []


# =========================================================================== #
# 19. TikTok Creative Center
# =========================================================================== #

def test_parse_tiktok_hashtags():
    items = gts.parse_tiktok_hashtags(TIKTOK_HTML, "us", 7)
    assert [it["title"] for it in items] == ["#unicornfrappuccino", "#haydenpanettiere"]
    for it in items:
        _assert_raw_item(it)
    a = items[0]
    assert a["source"] == "tiktok"
    assert a["source_key"] == "tiktok:63554219:US:7"
    assert a["url"] == "https://www.tiktok.com/tag/unicornfrappuccino"
    # POSTS is the headline metric, not views: a trend is a format people are
    # MAKING, so the number that matters is how many of them made one. Views
    # are still carried in `extra` and in the summary line.
    assert a["metric_value"] == 56051.0 and a["metric_label"] == "posts"
    assert a["extra"]["views"] == 205162676.0
    assert a["region"] == "US"
    assert a["category"] == "food"                      # 27000000000
    assert items[1]["category"] == "entertainment"      # 23000000000
    # published_at is the LAST curve point (D-1), a real measurement, not fetch time
    assert a["published_at"] == "2026-08-18T00:00:00+00:00"
    assert a["extra"]["posts"] == 56051.0 and a["extra"]["rank"] == 1.0
    assert a["extra"]["period_days"] == 7
    assert a["extra"]["top_creators"] == ["sofia1089"]
    assert "56,051 posts" in a["summary"] and "@sofia1089" in a["summary"]


def test_tiktok_image_url_is_never_the_signed_avatar():
    """avatarURL carries x-expires ~48h out: storing it gives cards that 403 the
    day after the run."""
    items = gts.parse_tiktok_hashtags(TIKTOK_HTML, "US", 7)
    assert all(it["image_url"] is None for it in items)
    assert all("x-expires" not in json.dumps(it) for it in items)


def test_tiktok_popularity_curve_survives_into_extra():
    """The curve is the single most valuable field in the whole source set: it
    is externally-measured DAILY growth, i.e. the only thing that gives the
    ranking module a real growth term on run #1."""
    a, b = gts.parse_tiktok_hashtags(TIKTOK_HTML, "US", 7)
    curve = a["extra"]["curve"]
    assert [p["value"] for p in curve] == [100, 95.58902349209004, 78.90165610597599, 39.45082805298799]
    assert curve[0]["at"] == "2026-08-15T00:00:00+00:00"
    assert a["extra"]["curve_start_ts"] == 1786752000
    # last point over the one before it — this hashtag is HALVING, not rising
    assert a["extra"]["growth_ratio"] == pytest.approx(0.5)
    assert b["extra"]["growth_ratio"] == pytest.approx(2.0)


def test_parse_tiktok_hashtags_junk():
    assert gts.parse_tiktok_hashtags(TIKTOK_HTML_NO_PAYLOAD, "US", 7) == []
    assert gts.parse_tiktok_hashtags("", "US", 7) == []
    assert gts.parse_tiktok_hashtags(
        '<script type="application/json" id="__MODERN_ROUTER_DATA__">not json</script>',
        "US", 7) == []
    # right blob, wrong loader key (the sibling Video tab's shape)
    other = json.dumps({"loaderData": {"creativeCenter/trends/video/page": {}}})
    html = f'<script type="application/json" id="__MODERN_ROUTER_DATA__">{other}</script>'
    assert gts.tiktok_loader_payload(html) is None
    assert gts.parse_tiktok_hashtags(html, "US", 7) == []


# =========================================================================== #
# 20. Fetchers for the new sources
# =========================================================================== #

def test_fetch_hacker_news_and_lemmy_hit_the_documented_endpoints():
    c1 = _FakeClient(lambda u, p, h: _FakeResponse(payload=HN_JSON))
    items = _run(gts.fetch_hacker_news(c1))
    assert c1.calls[0]["url"] == "https://hn.algolia.com/api/v1/search"
    assert c1.calls[0]["params"]["tags"] == "front_page"
    assert len(items) == 2

    c2 = _FakeClient(lambda u, p, h: _FakeResponse(payload=LEMMY_JSON))
    items = _run(gts.fetch_lemmy(c2))
    assert c2.calls[0]["url"] == "https://lemmy.world/api/v3/post/list"   # v4 is 404
    assert c2.calls[0]["params"] == {"sort": "TopDay", "limit": 50, "type_": "All"}
    assert len(items) == 1

    with pytest.raises(httpx.HTTPStatusError):
        _run(gts.fetch_hacker_news(_FakeClient(lambda u, p, h: _FakeResponse(503))))


def test_fetch_github_asks_the_server_for_recency():
    """created:> is a SERVER-SIDE recency filter — the owner's "nothing older
    than a month", enforced at the source instead of by a later gate."""
    client = _FakeClient(lambda u, p, h: _FakeResponse(payload=GITHUB_JSON))
    items = _run(gts.fetch_github(client, today=date(2026, 8, 20), days=30))
    call = client.calls[0]
    assert call["url"] == "https://api.github.com/search/repositories"
    assert call["params"]["q"] == "created:>2026-07-21"
    assert call["params"]["sort"] == "stars" and call["params"]["order"] == "desc"
    assert call["headers"]["Accept"] == "application/vnd.github+json"
    assert len(items) == 2
    assert gts.GITHUB_CREATED_WITHIN_DAYS == 30


def test_fetch_google_news_passes_the_edition_triple():
    client = _FakeClient(lambda u, p, h: _FakeResponse(text=GNEWS_RSS))
    items = _run(gts.fetch_google_news(
        client, query='(UAE) when:7d', geo="AE", hl="en-AE", gl="AE", ceid="AE:en"))
    call = client.calls[0]
    assert call["url"] == "https://news.google.com/rss/search"
    assert call["params"] == {"q": "(UAE) when:7d", "hl": "en-AE", "gl": "AE", "ceid": "AE:en"}
    assert "when:7d" in call["params"]["q"]        # server-side recency
    assert [it["region"] for it in items] == ["AE", "AE"]
    with pytest.raises(ValueError):
        _run(gts.fetch_google_news(client, query="  ", geo="AE"))


def test_fetch_tiktok_sends_the_load_bearing_accept_header():
    """httpx defaults to Accept: */*, which returns HTTP 200 with the payload
    SILENTLY ABSENT. Measured same-IP, same-second: 40 kB vs 74 kB."""
    client = _FakeClient(lambda u, p, h: _FakeResponse(text=TIKTOK_HTML))
    items = _run(gts.fetch_tiktok_hashtags(client, region="ae", period=7))
    call = client.calls[0]
    assert call["url"] == "https://ads.tiktok.com/creative/creativeCenter/trends/hashtag"
    assert call["headers"]["Accept"] == "text/html"
    assert "Chrome" in call["headers"]["User-Agent"]
    assert call["params"] == {"region": "AE", "period": 7}
    assert [it["region"] for it in items] == ["AE", "AE"]


def test_fetch_tiktok_validates_the_region_against_the_allow_list():
    """An unsupported code does not error server-side — it silently returns US
    data, which would mislabel the region on every card it produced."""
    client = _FakeClient(lambda u, p, h: _FakeResponse(text=TIKTOK_HTML))
    for good in ("AE", "SA", "US", "GB", "EG"):
        assert good in gts.TIKTOK_REGIONS
    assert "IN" not in gts.TIKTOK_REGIONS and len(gts.TIKTOK_REGIONS) == 27
    with pytest.raises(ValueError):
        _run(gts.fetch_tiktok_hashtags(client, region="IN", period=7))
    with pytest.raises(ValueError):
        _run(gts.fetch_tiktok_hashtags(client, region="", period=7))
    assert client.calls == []          # never even asked
    # an unsupported period degrades to the shortest supported one, not to junk
    _run(gts.fetch_tiktok_hashtags(client, region="US", period=120))
    assert client.calls[0]["params"]["period"] == 7


def test_fetch_tiktok_raises_when_the_ssr_payload_is_gone():
    """The single most important line of this integration: a scrape that quietly
    returns zero items is invisible for weeks (the sibling Video tab's fate).
    A raise becomes a visible source failure in the run card."""
    client = _FakeClient(lambda u, p, h: _FakeResponse(text=TIKTOK_HTML_NO_PAYLOAD))
    with pytest.raises(gts.TikTokLayoutChanged):
        _run(gts.fetch_tiktok_hashtags(client, region="US", period=7))
    assert issubclass(gts.TikTokLayoutChanged, RuntimeError)
    # ...and fetch_all turns it into a recorded source failure, not a crash
    plan = gts.build_fetch_plan(sources=["tiktok"], geos=[], subreddits=[],
                                youtube_api_key="", tiktok_regions=["US"])
    items, stats = _run(gts.fetch_all(plan, client=client))
    assert items == []
    assert stats["tiktok"]["ok"] is False and "TikTokLayoutChanged" in stats["tiktok"]["error"]


# =========================================================================== #
# 21. Wikipedia rise gate
# =========================================================================== #

@pytest.mark.parametrize("name,views,prev,expected", [
    # measured on the live top-40 — the exact rows the owner complained about
    ("ChatGPT",                        56_541, 33_065, False),   # 1.71x, evergreen
    ("Erection",                       27_000, 25_000, False),   # 1.08x
    ("Ejaculation",                    56_541, 33_065, False),   # 1.71x
    ("Deaths in 2026",                200_000, 146_000, False),  # 1.37x
    ("List of highest-grossing films", 60_000, 60_000, False),   # 1.00x, flat
    ("Google",                        425_000, 100_000, True),   # 4.25x
    ("Nancy Mace",                     29_950,   5_000, True),   # 5.99x, tiny baseline
    ("Hayden Panettiere",          10_392_637,       0, True),   # new to the top-1000
])
def test_wiki_is_rising_matches_the_measured_corpus(name, views, prev, expected):
    assert gts.wiki_is_rising(views, prev) is expected, name


def test_wiki_is_rising_edges():
    assert gts.wiki_is_rising(100, None) is True        # no comparison day
    assert gts.wiki_is_rising(None, 100) is False
    assert gts.wiki_is_rising(100, -5) is True
    # a 2x rise that is only +1,000 views is a rounding error, not a trend
    assert gts.wiki_is_rising(2_000, 1_000) is False
    assert gts.wiki_is_rising(41_000, 20_000) is True   # 2.05x, +21k, below baseline_high
    # exactly on both bars
    assert gts.wiki_is_rising(60_000, 40_000) is False  # 1.5x but prev >= 25k ⇒ needs 3x
    assert gts.wiki_is_rising(120_000, 40_000) is True


def test_parse_wikipedia_top_drops_the_evergreen_page_that_ranked_number_two():
    """"ChatGPT" rose 1.71x day over day on a huge baseline and became the #2
    card in the live feed. A rise gate scaled to the page's own baseline is what
    removes it — and the sexual pages fall out with it, no word list involved."""
    today = {"items": [{"year": "2026", "month": "08", "day": "18", "articles": [
        {"article": "ChatGPT", "views": 56_541, "rank": 3},
        {"article": "Ejaculation", "views": 56_541, "rank": 7},
        {"article": "Nancy_Mace", "views": 29_950, "rank": 20},
    ]}]}
    yesterday = {"items": [{"year": "2026", "month": "08", "day": "17", "articles": [
        {"article": "ChatGPT", "views": 33_065},
        {"article": "Ejaculation", "views": 33_065},
        {"article": "Nancy_Mace", "views": 5_000},
    ]}]}
    keys = [it["source_key"] for it in gts.parse_wikipedia_top(today, yesterday)]
    assert keys == ["Nancy_Mace"]


def test_parse_wikipedia_top_without_a_comparison_day_falls_back_to_rank():
    """Nothing can be measured, so emit only the very top rather than the whole
    1000-row list."""
    today = {"items": [{"year": "2026", "month": "08", "day": "18", "articles": [
        {"article": "Top_Story", "views": 900_000, "rank": 2},
        {"article": "Rank_25", "views": 100_000, "rank": 25},
        {"article": "Rank_26", "views": 99_000, "rank": 26},
        {"article": "No_Rank", "views": 98_000},
    ]}]}
    keys = [it["source_key"] for it in gts.parse_wikipedia_top(today, None)]
    assert keys == ["Top_Story", "Rank_25", "No_Rank"]


# =========================================================================== #
# 22. fetch_all — per-source caps and the screen hook
# =========================================================================== #

def _many(source: str, n: int, *, region="global", title="t"):
    return [gts.make_item(source=source, source_key=f"{source}:{i}",
                          title=f"{title}{i}", region=region) for i in range(n)]


def test_fetch_all_honours_per_source_caps(monkeypatch):
    """Today Reddit's budget is 7x40=280 while KYM — the best signal-to-noise
    source in the system — is capped at 8. caps inverts that."""
    async def fake_reddit(client, sub, limit=25):
        return _many("reddit", 40)

    async def fake_kym(client):
        return _many("kym", 40)

    async def fake_bsky(client, limit=25):
        return _many("bluesky", 40)

    monkeypatch.setattr(gts, "fetch_reddit", fake_reddit)
    monkeypatch.setattr(gts, "fetch_kym", fake_kym)
    monkeypatch.setattr(gts, "fetch_bluesky", fake_bsky)
    plan = gts.build_fetch_plan(sources=["reddit", "kym", "bluesky"], geos=[],
                                subreddits=["memes", "technology"], youtube_api_key="")
    items, stats = _run(gts.fetch_all(
        plan, client=object(), max_items_per_unit=8, caps={"reddit": 3, "kym": 40}))
    assert stats["reddit"]["count"] == 6          # 3 per unit x 2 subs
    assert stats["kym"]["count"] == 40            # its own cap beats the default 8
    assert stats["bluesky"]["count"] == 8         # no entry ⇒ the default
    assert len(items) == 54


def test_fetch_all_screens_before_the_cap(monkeypatch):
    """The point of the hook: the cap must yield GOOD items, not whatever
    happened to come back first."""
    async def fake_kym(client):
        # the first six are junk, the good ones are behind them
        return (_many("kym", 6, title="junk") + _many("kym", 10, title="keep"))

    monkeypatch.setattr(gts, "fetch_kym", fake_kym)

    def screen(item):
        if item["title"].startswith("junk"):
            return ("drop", "explicit")
        return ("keep", "")

    plan = gts.build_fetch_plan(sources=["kym"], geos=[], subreddits=[], youtube_api_key="")
    items, stats = _run(gts.fetch_all(
        plan, client=object(), max_items_per_unit=4, screen=screen))
    assert [it["title"] for it in items] == ["keep0", "keep1", "keep2", "keep3"]
    assert stats["kym"]["count"] == 4
    assert stats["kym"]["dropped"] == 6
    assert stats["kym"]["dropped_by"] == {"explicit": 6}


def test_fetch_all_screen_flag_keeps_the_item_and_stamps_it(monkeypatch):
    async def fake_kym(client):
        return _many("kym", 3, title="x")

    monkeypatch.setattr(gts, "fetch_kym", fake_kym)

    def screen(item):
        if item["title"] == "x1":
            return ("flag", "bait")
        if item["title"] == "x2":
            return ("drop", "harm")
        return ("keep", "")

    plan = gts.build_fetch_plan(sources=["kym"], geos=[], subreddits=[], youtube_api_key="")
    items, stats = _run(gts.fetch_all(plan, client=object(), screen=screen))
    assert [it["title"] for it in items] == ["x0", "x1"]
    assert "safety" not in items[0]                       # a keep is untouched
    assert items[1]["safety"] == "flagged"
    assert items[1]["safety_reason"] == "bait"
    # a flag is KEPT, so it is not counted as dropped
    assert stats["kym"]["dropped"] == 1
    assert stats["kym"]["dropped_by"] == {"harm": 1}
    assert stats["kym"]["flagged"] == 1


def test_fetch_all_without_a_screen_is_unchanged(monkeypatch):
    """Additive by construction: no screen ⇒ the same items AND the same stats
    key set as before the hook existed."""
    async def fake_kym(client):
        return _many("kym", 5, title="x")

    monkeypatch.setattr(gts, "fetch_kym", fake_kym)
    plan = gts.build_fetch_plan(sources=["kym"], geos=[], subreddits=[], youtube_api_key="")
    items, stats = _run(gts.fetch_all(plan, client=object()))
    assert [it["title"] for it in items] == ["x0", "x1", "x2", "x3", "x4"]
    assert set(stats["kym"]) == {"ok", "count", "error", "ms"}
    assert all("safety" not in it for it in items)


def test_fetch_all_survives_a_screen_that_raises(monkeypatch):
    """Degraded modes must survive: a broken screen must not cost us a source."""
    async def fake_kym(client):
        return _many("kym", 3, title="x")

    monkeypatch.setattr(gts, "fetch_kym", fake_kym)

    def screen(item):
        raise RuntimeError("the safety module exploded")

    plan = gts.build_fetch_plan(sources=["kym"], geos=[], subreddits=[], youtube_api_key="")
    items, stats = _run(gts.fetch_all(plan, client=object(), screen=screen))
    assert len(items) == 3 and stats["kym"]["ok"] is True
    assert stats["kym"]["dropped"] == 0


# =========================================================================== #
# 23. build_fetch_plan — the new units
# =========================================================================== #

def test_build_fetch_plan_includes_the_new_sources():
    plan = gts.build_fetch_plan(
        sources=["hacker_news", "lemmy", "github", "google_news", "tiktok",
                 "instagram", "instagram_watch"],
        geos=["US"], subreddits=[], youtube_api_key="",
        tiktok_regions=["AE", "us", "zz"],
        ig_hashtags=["#coding", "abudhabi"],
        ig_watch_accounts=["@42abudhabi", "kym"])
    units = [u["unit"] for u in plan]
    assert "hacker_news" in units and "lemmy" in units and "github" in units
    # google news is editorial, not geo-expanded: one Gulf unit, one tech unit
    assert "google_news:gulf" in units and "google_news:tech" in units
    gulf = next(u for u in plan if u["unit"] == "google_news:gulf")
    assert gulf["region"] == "AE" and gulf["params"]["ceid"] == "AE:en"
    assert "when:7d" in gulf["params"]["query"]
    # tiktok: region x period, unsupported codes skipped
    assert "tiktok:AE:7d" in units and "tiktok:AE:30d" in units
    assert "tiktok:US:7d" in units
    assert not any(u.startswith("tiktok:ZZ") for u in units)
    tt = next(u for u in plan if u["unit"] == "tiktok:AE:7d")
    assert tt["params"] == {"region": "AE", "period": 7} and tt["region"] == "AE"
    # instagram: '#' and '@' are stripped, hashtag and watch units are distinct ids
    assert "instagram:coding" in units and "instagram:abudhabi" in units
    assert "instagram_watch:42abudhabi" in units and "instagram_watch:kym" in units
    ig = next(u for u in plan if u["unit"] == "instagram:coding")
    assert ig["source"] == "instagram" and ig["params"] == {"tag": "coding"}
    watch = next(u for u in plan if u["unit"] == "instagram_watch:kym")
    assert watch["source"] == "instagram_watch" and watch["params"] == {"username": "kym"}
    for u in plan:
        assert u["label"] == gts.SOURCE_LABELS[u["source"]]


def test_instagram_and_reddit_share_one_serial_lane_policy():
    """Instagram's Graph rate limit is SHARED with the publish path — a doomed
    run must never burn it in parallel."""
    assert gts.SERIAL_SOURCES == frozenset({"reddit", "instagram", "instagram_watch"})


def test_fetch_all_dispatches_every_new_source(monkeypatch):
    seen: list[tuple] = []

    async def fake_hn(client, limit=30):
        seen.append(("hn",)); return gts.parse_hacker_news(HN_JSON)

    async def fake_lemmy(client, limit=50):
        seen.append(("lemmy",)); return gts.parse_lemmy(LEMMY_JSON)

    async def fake_gh(client, *, days=30, limit=30, today=None):
        seen.append(("gh", days)); return gts.parse_github(GITHUB_JSON)

    async def fake_news(client, *, query, geo="US", hl="en-US", gl="US", ceid="US:en"):
        seen.append(("news", geo, ceid)); return gts.parse_google_news_rss(GNEWS_RSS, geo, query)

    async def fake_tt(client, *, region, period=7):
        seen.append(("tt", region, period)); return gts.parse_tiktok_hashtags(TIKTOK_HTML, region, period)

    async def fake_ig(client, *, tag, **kw):
        seen.append(("ig", tag)); return _many("instagram", 2)

    async def fake_ig_watch(client, *, username, **kw):
        seen.append(("igw", username)); return _many("instagram_watch", 1)

    monkeypatch.setattr(gts, "fetch_hacker_news", fake_hn)
    monkeypatch.setattr(gts, "fetch_lemmy", fake_lemmy)
    monkeypatch.setattr(gts, "fetch_github", fake_gh)
    monkeypatch.setattr(gts, "fetch_google_news", fake_news)
    monkeypatch.setattr(gts, "fetch_tiktok_hashtags", fake_tt)
    monkeypatch.setattr(gts, "fetch_instagram_hashtag", fake_ig)
    monkeypatch.setattr(gts, "fetch_instagram_watch", fake_ig_watch)

    plan = gts.build_fetch_plan(
        sources=["hacker_news", "lemmy", "github", "google_news", "tiktok",
                 "instagram", "instagram_watch"],
        geos=[], subreddits=[], youtube_api_key="",
        tiktok_regions=["AE"], ig_hashtags=["coding"], ig_watch_accounts=["nasa"])
    items, stats = _run(gts.fetch_all(plan, client=object()))
    assert ("hn",) in seen and ("lemmy",) in seen
    assert ("news", "AE", "AE:en") in seen and ("news", "US", "US:en") in seen
    assert ("tt", "AE", 7) in seen and ("tt", "AE", 30) in seen
    assert ("ig", "coding") in seen and ("igw", "nasa") in seen
    assert stats["hacker_news"]["count"] == 2
    assert stats["google_news"]["count"] == 4        # two units x two items
    assert stats["tiktok"]["count"] == 4             # two periods x two rows
    assert stats["instagram"]["count"] == 2 and stats["instagram_watch"]["count"] == 1
    assert all(st["ok"] for st in stats.values())
    for it in items:
        _assert_raw_item(it)


def test_instagram_is_imported_lazily_and_a_missing_module_is_only_a_source_failure(monkeypatch):
    """general_trends_instagram imports THIS module, so a top-level import here
    would be circular — whichever loaded second would get a half-built partner,
    silently and import-order dependent. The wrappers import inside the call, so
    a missing module degrades to "that source failed", never to "the whole
    trends tab is down"."""
    assert callable(gts.fetch_instagram_hashtag)
    assert callable(gts.fetch_instagram_watch)
    # the cycle is broken: loading this module does not pull instagram in
    src = Path(gts.__file__).read_text(encoding="utf-8")
    assert "from .general_trends_instagram import" in src      # only inside the wrappers
    for line in src.splitlines():
        if "general_trends_instagram" in line and "import" in line:
            assert line.startswith("    "), line               # indented ⇒ function-local

    monkeypatch.setitem(sys.modules, "app.services.general_trends_instagram", None)
    plan = gts.build_fetch_plan(sources=["instagram"], geos=[], subreddits=[],
                                youtube_api_key="", ig_hashtags=["coding"])
    assert [u["unit"] for u in plan] == ["instagram:coding"]
    items, stats = _run(gts.fetch_all(plan, client=object()))
    assert items == []
    assert stats["instagram"]["ok"] is False and stats["instagram"]["error"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


# =========================================================================== #
# Low-value item shapes — sports fixtures and bare tokens
# =========================================================================== #

def test_sports_fixtures_are_recognised_in_both_scripts():
    for title in ("al-najma vs al-ittihad", "giants vs guardians", "sparks v. sun",
                  "Diriyah VS Al-Nassr", "النصر ضد الدرعية"):
        assert gts.is_sports_fixture(title) is True, title


def test_a_hyphen_or_an_embedded_vs_is_not_a_fixture():
    """The separator must be a whole word: dropping "Spider-Man" or "Versace"
    to a fixture rule would cost real trends."""
    for title in ("Spider-Man: Brand New Day", "Mercedes-Benz", "Versace",
                  "Vsauce", "GTA 6 release date", "Marlins - Phillies", ""):
        assert gts.is_sports_fixture(title) is False, title


def test_bare_token_needs_no_summary_no_metric_and_few_words():
    bare = {"title": "Territorial Waters", "summary": "", "metric_value": None}
    assert gts.is_bare_token(bare) is True
    # any one of the three escape hatches saves it
    assert gts.is_bare_token({**bare, "summary": "a real description"}) is False
    assert gts.is_bare_token({**bare, "metric_value": 2000.0}) is False
    assert gts.is_bare_token({**bare, "title": "Islamic New Year"}) is False
    assert gts.is_bare_token({"title": "", "summary": "", "metric_value": None}) is False


def test_screen_shape_returns_the_reason_and_never_raises():
    assert gts.screen_shape({"title": "giants vs guardians"}) == ("drop", "fixture")
    assert gts.screen_shape(
        {"title": "Official Sources", "summary": "", "metric_value": None}
    ) == ("drop", "bare_token")
    assert gts.screen_shape({"title": "Nihilist Penguin", "summary": "A penguin walks off"})         == ("keep", "")
    # a malformed item is KEPT: losing a source to a filter bug is worse
    assert gts.screen_shape({}) == ("keep", "")
    assert gts.screen_shape({"title": None, "summary": None}) == ("keep", "")


# =========================================================================== #
# Feed position — the only magnitude signal the metric-less sources have
# =========================================================================== #

def test_kym_records_its_feed_position():
    items = gts.parse_kym_rss(KYM_RSS)
    assert items, "fixture produced no items"
    assert [i["extra"]["rank"] for i in items] == list(range(1, len(items) + 1))


def test_reddit_records_its_feed_position():
    items = gts.parse_reddit_atom(REDDIT_ATOM, "memes")
    assert items, "fixture produced no items"
    assert [i["extra"]["rank"] for i in items] == list(range(1, len(items) + 1))
    # and the sub is still there — rank is additive
    assert all(i["extra"]["sub"] == "memes" for i in items)


def test_tiktok_carries_views_per_post_the_participation_test():
    """A trend is a format people are MAKING. Views-per-post separates that
    from a story people merely WATCH better than any other number this feed
    gives us, and it costs nothing — both operands are already in the row.

    Measured live (2026-08-20):
        #marvelrivalssummerfest2  180k posts /  35M views =    196 v/post
        #haydenpanettiere          37k posts / 396M views = 10,725 v/post
    A 55x separation between "everyone is making one" and "a celebrity died",
    on two rows that look identical if you rank on views.

    It is a CLUE, not a verdict — #tiktokcookbook measured 6,937 and is a real
    format — so it is surfaced to the scorer in the summary and never used as
    a hard gate."""
    items = gts.parse_tiktok_hashtags(TIKTOK_HTML, "US", 7)
    a = items[0]
    expected = a["extra"]["views"] / a["extra"]["posts"]
    assert a["extra"]["views_per_post"] == pytest.approx(expected)
    # the scorer reads the summary, so the number has to be IN it
    assert "views per post" in a["summary"]
    assert f"{expected:,.0f}" in a["summary"]


def test_tiktok_views_per_post_is_none_when_it_cannot_be_computed():
    """No posts, no ratio — never a divide-by-zero and never a fabricated 0."""
    html = TIKTOK_HTML.replace('publishCnt": "56051"', 'publishCnt": "0"')
    items = gts.parse_tiktok_hashtags(html, "US", 7)
    assert items, "fixture stopped parsing"
    assert items[0]["extra"]["views_per_post"] is None
    # and the summary must not claim a ratio it does not have
    assert "views per post" not in items[0]["summary"]


def test_tiktok_keeps_top_creator_follower_counts():
    """Three mega-accounts carrying a tag is a campaign; small accounts are a
    grassroots format. Measured: AE's #happyindependenceday is led by a
    2,235,588-follower account."""
    items = gts.parse_tiktok_hashtags(TIKTOK_HTML, "US", 7)
    followers = items[0]["extra"]["creator_followers"]
    assert isinstance(followers, list)
    assert all(isinstance(f, float) for f in followers)

# =========================================================================== #
# 25. The 2026-08-20 Know Your Meme family — fixtures
#
# Real markup, trimmed, captured from the live endpoints on 2026-08-20:
#   /memes/all.rss        16 items  (2 kept here + two deliberate rejects)
#   /editorials.rss       10 items  (1 kept here)
#   /sitemap.xml          62 <url>, 44 of them /memes/ (5 kept here)
#   entry pages           4 kept: the penguin (Year 2008 — THE trap case),
#                         Mrs. Puff (Year 2020, 135 views — the counter-case),
#                         a Deadpool reject, and one with no format Type.
# =========================================================================== #

KYM_TODAY = date(2026, 8, 20)

# all.rss — note the <a><img/></a> wrapper (the newsfeed has a bare <img>), the
# "Read More" tail on every description, and the wave stated in prose.
KYM_ALL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Know Your Meme Entries - All</title>
    <link>https://knowyourmeme.com</link>
    <item>
      <guid isPermaLink="false">Entry-57320</guid>
      <link>https://knowyourmeme.com/memes/out-of-respect-for-jayden-daniels-number-5-jersey-controversy</link>
      <title>Out Of Respect For Jayden Daniels / Number 5 Jersey Controversy</title>
      <pubDate>Wed, 19 Aug 2026 15:35:46 -0400</pubDate>
      <description>&lt;a href="https://knowyourmeme.com/memes/out-of-respect-for-jayden-daniels-number-5-jersey-controversy"&gt;&lt;img alt="Jdaniels" src="https://i.kym-cdn.com/entries/icons/mobile/000/057/320/jdaniels.jpg" /&gt;&lt;/a&gt;&lt;p&gt;Out Of Respect For Jayden Daniels is a viral controversy and series of memes that make fun of NFL player Jayden Daniels.&lt;/p&gt;
&lt;p&gt;The story was reported in mid-August 2026 and inspired memes that month. The memes typically include the catchphrase, "Out of respect for Jayden Daniels," as a punchline.&lt;/p&gt;&lt;a href="https://knowyourmeme.com/memes/out-of-respect-for-jayden-daniels-number-5-jersey-controversy"&gt;Read More&lt;/a&gt;</description>
    </item>
    <item>
      <guid isPermaLink="false">Entry-57319</guid>
      <link>https://knowyourmeme.com/memes/udin-din-din-dun</link>
      <title>Udin Din Din Dun</title>
      <pubDate>Wed, 19 Aug 2026 14:35:48 -0400</pubDate>
      <description>&lt;p&gt;Udin Din Din Dun is an AI-generated Indonesian Brainrot character depicted as an anthropomorphic orange.&lt;/p&gt;
&lt;p&gt;The character emerged online sometime in early 2025, spreading through funk and phonk remixes.&lt;/p&gt;&lt;a href="https://knowyourmeme.com/memes/udin-din-din-dun"&gt;Read More&lt;/a&gt;</description>
    </item>
    <item>
      <guid isPermaLink="false">Entry-57318</guid>
      <link>https://knowyourmeme.com/editorials/guides/some-explainer</link>
      <title>An editorial that leaked into the entry feed</title>
      <pubDate>Wed, 19 Aug 2026 13:00:00 -0400</pubDate>
      <description>&lt;p&gt;Not an entry.&lt;/p&gt;</description>
    </item>
    <item>
      <guid isPermaLink="false">Entry-57317</guid>
      <link>https://knowyourmeme.com/memes/no-title-here</link>
      <title></title>
      <pubDate>Wed, 19 Aug 2026 12:00:00 -0400</pubDate>
    </item>
  </channel>
</rss>
"""

KYM_EDITORIALS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Know Your Meme Editorials</title>
    <link>https://knowyourmeme.com</link>
    <item>
      <guid isPermaLink="false">Editorial-17432</guid>
      <link>https://knowyourmeme.com/editorials/guides/what-is-the-flobby-samuel-insect-meme-the-viral-comedy-heaven-reddit-post-and-big-jack-horner-meme-explained</link>
      <title>What Is The 'Flobby Samuel Insect' Meme? The Viral 'Comedy Heaven' Reddit Post And 'Big Jack Horner' Meme Explained</title>
      <pubDate>Wed, 19 Aug 2026 12:21:49 -0400</pubDate>
      <description>&lt;p&gt;The Comedy Heaven &lt;a href="https://knowyourmeme.com/memes/sites/reddit" class="internal-link"&gt;subreddit&lt;/a&gt; is trying to figure out if a meme format known as "Flobby Samuel Insect" is real or not, as a recent post in the community goes viral.&lt;/p&gt;
&lt;p&gt;&lt;img src="https://i.kym-cdn.com/photos/images/newsfeed/003/308/857/a75.jpg" alt="" /&gt;&lt;/p&gt;
&lt;p&gt;The so-called "Flobby Samuel Insect" meme was first shared on the /r/ComedyHeaven subreddit on August 19th, 2026.&lt;/p&gt;</description>
    </item>
    <item>
      <guid isPermaLink="false">Editorial-17431</guid>
      <link>https://knowyourmeme.com/editorials/collections/best-memes-of-the-week</link>
      <title>The Best Memes Of The Week</title>
      <pubDate>Tue, 18 Aug 2026 17:02:00 -0400</pubDate>
      <description>&lt;p&gt;A weekly round-up.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>
"""

# KYM writes every tag on its own line with the value indented — the reason the
# parser strips instead of reading the text as-is. Includes an editorial URL, a
# /videos/ URL and a duplicate, all of which must be filtered out; and one
# entry filed under events/ (a sub-path, not a bare slug).
KYM_SITEMAP_XML = """<?xml version='1.0' encoding='utf-8' ?>
<urlset xmlns='https://www.sitemaps.org/schemas/sitemap/0.9'>
<url>
<loc>
https://knowyourmeme.com/editorials/guides/what-is-the-flobby-samuel-insect-meme
</loc>
<lastmod>
2026-08-19T12:50:00-04:00
</lastmod>
<priority>
0.6
</priority>
</url>

<url>
<loc>
https://knowyourmeme.com/memes/mrs-puff-in-a-padded-room
</loc>
<lastmod>
2026-08-19T15:43:43-04:00
</lastmod>
<priority>
1.0
</priority>
</url>

<url>
<loc>
https://knowyourmeme.com/memes/events/black-community-reacts-to-hayden-panettieres-death
</loc>
<lastmod>
2026-08-19T11:22:06-04:00
</lastmod>
</url>

<url>
<loc>
https://knowyourmeme.com/videos/440075-reddit
</loc>
<lastmod>
2026-08-19T10:21:09-04:00
</lastmod>
</url>

<url>
<loc>
https://knowyourmeme.com/memes/kinda-chic-trend
</loc>
<lastmod>
2026-08-14T11:54:26-04:00
</lastmod>
</url>

<url>
<loc>
https://knowyourmeme.com/memes/mrs-puff-in-a-padded-room
</loc>
<lastmod>
2026-08-19T15:43:43-04:00
</lastmod>
</url>
</urlset>
"""


def _kym_entry_html(
    *, slug: str, title: str, badge: str, status: str, types_html: str,
    year: str, origin: str, region_html: str, views: str, videos: str,
    photos: str, comments: str, published: str, body: str, image: str,
) -> str:
    """The real entry-page skeleton, verbatim down to the single quotes.

    Every attribute below is copied from a live page: the ``<dl>`` that opens
    with an ``entry-category-badge`` anchor and then runs ``<dt>``/``<dd>``
    pairs, the separate stats ``<dl>`` whose counts live in ``title=``, the
    ``<section class='bodycopy'>`` with a mobile-only navigation table in front
    of the prose, and the ``article:published_time`` meta.
    """
    return f"""<!DOCTYPE html><html><head>
<meta property='og:title' content='{title} | Know Your Meme' />
<meta property='og:image' content='{image}' />
<meta property='og:url' content='https://knowyourmeme.com/memes/{slug}' />
<meta property='article:published_time' content='{published}' />
</head><body>
<h1 class='content-title entry-title'>
{title}
</h1>
<dl>
<dd class='views' title='{views} Views'>
<a href='/memes/{slug}' rel='nofollow'>{views}</a>
</dd>
<dd class='videos' title='{videos} Videos'>
<a href='/memes/{slug}#videos' rel='nofollow'>{videos}</a>
</dd>
<dd class='photos' title='{photos} Images'>
<a href='/memes/{slug}#photos' rel='nofollow'>{photos}</a>
</dd>
<dd class='comments' title='{comments} Comments'>
<a href='/memes/{slug}#comments' rel='nofollow'>{comments}</a>
</dd>
</dl>
<dl>
<a href="/categories/{badge.lower()}" class="entry-category-badge" style="background: #220a51; color: white;">{badge}</a>
<dt>
Status
</dt>
<dd>
{status}
</dd>
<dt>
Type:
</dt>
<dd>
{types_html}
</dd>
<dt>
Year
</dt>
<dd>
<a href="/search?query=year%3A{year}" rel="nofollow">{year}</a>
</dd>
<dt>
Origin
</dt>
<dd class='entry_origin_link'>
{origin}
</dd>
{region_html}
</dl>
<section class='bodycopy'>
<table class="navigation mobile-only">
<thead><tr><td><b>Navigation</b></td></tr></thead>
<tbody><tr><td><a href="#about">About</a> &bull; <a href="#origin">Origin</a></td></tr></tbody>
</table>
<h2 id='about'>About</h2>
<p>{body}</p>
</section>
</body></html>"""


def _kym_types(*slugs: str) -> str:
    return ", ".join(
        f'<a href="/types/{s}?status=all" class="entry-type-link">'
        f'{s.replace("-", " ").title()}</a>' for s in slugs)


_KYM_REGION_US = """<dt>
Region
</dt>
<dd>
<a href="/search?query=region%3A%22United+States%22" rel="nofollow">United States</a>
</dd>"""

# THE TRAP. Year 2008 — the owner's own headline example of a trend, and the
# entry any "Year must be recent" filter deletes first.
KYM_ENTRY_PENGUIN = _kym_entry_html(
    slug="penguin-walking-toward-mountain-nihilist-penguin",
    title="Penguin Walking Toward Mountain / Nihilist Penguin",
    badge="Meme", status="Confirmed",
    types_html=_kym_types("animal", "character", "exploitable",
                          "participatory-media", "pop-culture-reference", "viral-video"),
    year="2008", origin='<a href="/memes/sites/youtube">YouTube</a>',
    region_html=_KYM_REGION_US,
    views="71,598", videos="5", photos="17", comments="7",
    published="2026-01-21T13:17:27-05:00",
    image="https://i.kym-cdn.com/entries/icons/original/000/056/159/Lonely_Penguin_Mountain_meme_banner_image.jpg",
    body=("Then, in early 2026, the clip of the bird became a prevalent meme and "
          "source for edits on TikTok and Instagram, typically paired with a pipe "
          "organ cover of Gigi D'Agostino's Eurodance song &quot;L'Amour Toujours.&quot;"),
)

# The counter-case: published to KYM the SAME WEEK as entries with real waves,
# also participatory-media, also an old Year — and 135 views, 0 videos.
KYM_ENTRY_MRS_PUFF = _kym_entry_html(
    slug="mrs-puff-in-a-padded-room", title="Mrs. Puff In a Padded Room",
    badge="Meme", status="Submission",
    types_html=_kym_types("participatory-media", "pop-culture-reference"),
    year="2020", origin="X / Twitter", region_html=_KYM_REGION_US,
    views="135", videos="0", photos="12", comments="0",
    published="2026-08-19T14:16:15-04:00",
    image="https://i.kym-cdn.com/entries/icons/original/000/057/316/puff.jpg",
    body=("Mrs. Puff In a Padded Room is an exploitable reaction image meme format "
          "based on a scene from the 2002 SpongeBob SquarePants episode."),
)

# Status: Deadpool — KYM's own editors judged it not a real meme. Note it also
# has NO Region row, which the parser must survive.
KYM_ENTRY_DEADPOOL = _kym_entry_html(
    slug="the-vogue-cat-cheshire", title="The Vogue Cat - Cheshire",
    badge="Meme", status="Deadpool",
    types_html=_kym_types("animal", "viral-video"),
    year="2026",
    origin="https://www.tiktok.com/@kenxie_j02/video/7668323193001757983",
    region_html="",
    views="264", videos="0", photos="0", comments="0",
    published="2026-08-02T06:25:22-04:00",
    image="https://i.kym-cdn.com/entries/icons/original/000/057/229/vogue.png",
    body="The Vogue Cat. Became famous on TikTok with 35M views in two days.",
)

# Typed only `character`: an entry about a THING, with nothing to make.
KYM_ENTRY_NO_FORMAT_TYPE = _kym_entry_html(
    slug="udin-din-din-dun", title="Udin Din Din Dun",
    badge="Meme", status="Submission", types_html=_kym_types("character"),
    year="2025", origin="TikTok",
    region_html="""<dt>
Region
</dt>
<dd>
<a href="/search?query=region%3A%22Indonesia%22" rel="nofollow">Indonesia</a>
</dd>""",
    views="64", videos="0", photos="6", comments="1",
    published="2026-08-19T14:35:48-04:00",
    image="https://i.kym-cdn.com/entries/icons/original/000/057/319/udin.jpg",
    body=("The character emerged online sometime in early 2025, spreading through "
          "funk and phonk remixes and AI-generated videos."),
)


# =========================================================================== #
# 26. Know Your Meme — the entry feeds
# =========================================================================== #

def test_parse_kym_entry_feed():
    items = gts.parse_kym_entry_feed(KYM_ALL_RSS, feed="all", today=KYM_TODAY)
    # the editorial link and the empty title are both dropped
    assert [i["source_key"] for i in items] == [
        "out-of-respect-for-jayden-daniels-number-5-jersey-controversy",
        "udin-din-din-dun"]
    for it in items:
        _assert_raw_item(it)
    a, b = items
    assert a["source"] == "kym_entries"
    assert a["url"].startswith("https://knowyourmeme.com/memes/")
    assert a["image_url"] == (
        "https://i.kym-cdn.com/entries/icons/mobile/000/057/320/jdaniels.jpg")
    assert a["published_at"] == "2026-08-19T19:35:46+00:00"
    assert a["region"] == "global"
    assert a["extra"]["guid"] == "Entry-57320"
    assert a["extra"]["feed"] == "all"
    assert (a["extra"]["rank"], a["extra"]["rank_total"]) == (1, 2)
    # the "Read More" anchor KYM appends to every description is not content
    assert "Read More" not in a["summary"]
    assert a["summary"].startswith("Out Of Respect For Jayden Daniels")
    # the wave is read out of the prose BEFORE any entry page is fetched
    assert a["extra"]["wave_year"] == 2026
    assert b["extra"]["wave_year"] == 2025
    assert b["image_url"] is None


def test_parse_kym_entry_feed_junk():
    assert gts.parse_kym_entry_feed("<rss><channel/></rss>") == []
    assert gts.parse_kym_entry_feed("") == []
    assert gts.parse_kym_entry_feed("<not xml") == []


def test_parse_kym_editorials_rss():
    items = gts.parse_kym_editorials_rss(KYM_EDITORIALS_RSS, today=KYM_TODAY)
    assert len(items) == 2
    for it in items:
        _assert_raw_item(it)
        assert it["source"] == "kym_editorials"
    a = items[0]
    assert a["source_key"] == (
        "editorials/guides/what-is-the-flobby-samuel-insect-meme-the-viral-"
        "comedy-heaven-reddit-post-and-big-jack-horner-meme-explained")
    assert a["title"].startswith("What Is The 'Flobby Samuel Insect' Meme?")
    assert a["summary"].startswith("The Comedy Heaven subreddit")
    assert "<a" not in a["summary"] and "<p>" not in a["summary"]
    assert a["image_url"] == "https://i.kym-cdn.com/photos/images/newsfeed/003/308/857/a75.jpg"
    assert a["published_at"] == "2026-08-19T16:21:49+00:00"
    assert (a["extra"]["rank"], a["extra"]["rank_total"]) == (1, 2)
    # NEVER a source that can mint: an explainer ABOUT a format is not the format
    assert "kym_editorials" in gts.NON_MINTING_SOURCES
    assert gts.is_minting("kym_editorials") is False


def test_parse_kym_sitemap_keeps_only_entry_urls_newest_first():
    """29 of the live sitemap's 44 /memes/ URLs are in NO feed — that is the
    only reason this unit exists. Everything else on the sitemap (editorials,
    /videos/, duplicates) has to go."""
    urls = gts.parse_kym_sitemap(KYM_SITEMAP_XML)
    assert [u["slug"] for u in urls] == [
        "mrs-puff-in-a-padded-room",
        "events/black-community-reacts-to-hayden-panettieres-death",
        "kinda-chic-trend",
    ]
    assert all(u["url"].startswith("https://knowyourmeme.com/memes/") for u in urls)
    # newest lastmod first, and KYM's newline-wrapped tags are stripped
    assert urls[0]["lastmod"] == "2026-08-19T19:43:43+00:00"
    assert urls[-1]["lastmod"] == "2026-08-14T15:54:26+00:00"
    # a sub-path entry keeps its whole path, so events/foo can never collide
    # with a top-level foo
    assert "/" in urls[1]["slug"]
    assert gts.parse_kym_sitemap("") == []
    assert gts.parse_kym_sitemap("<urlset/>") == []


# =========================================================================== #
# 27. Know Your Meme — the entry page and its Type: taxonomy
# =========================================================================== #

def test_parse_kym_entry_page_reads_the_type_taxonomy():
    facts = gts.parse_kym_entry_page(
        KYM_ENTRY_PENGUIN, url="https://knowyourmeme.com/memes/penguin", today=KYM_TODAY)
    assert facts["title"] == "Penguin Walking Toward Mountain / Nihilist Penguin"
    # the BADGE is the category. A format type is a different axis and must
    # never be written into `category`.
    assert facts["category"] == "Meme"
    assert facts["category"] not in gts.KYM_FORMAT_TYPES
    assert facts["status"] == "Confirmed"
    # type slugs come from the /types/<slug> hrefs, so re-wording a label on the
    # site cannot silently change the verdict
    assert facts["types"] == ["animal", "character", "exploitable",
                              "participatory-media", "pop-culture-reference",
                              "viral-video"]
    assert facts["year"] == 2008
    assert facts["origin"] == "YouTube"
    assert facts["region"] == "United States"
    assert (facts["views"], facts["videos"], facts["photos"], facts["comments"]) == (
        71598.0, 5.0, 17.0, 7.0)
    assert facts["published_at"] == "2026-01-21T18:17:27+00:00"
    assert facts["image_url"].endswith("Lonely_Penguin_Mountain_meme_banner_image.jpg")
    # the table-of-contents furniture is not the article
    assert "Navigation" not in facts["summary"]
    assert facts["summary"].startswith("Then, in early 2026")


def test_the_penguin_case_an_old_year_in_a_fresh_feed_is_kept_as_a_revival():
    """THE regression this whole taxonomy exists to prevent.

    `Year` on a KYM entry is the format's ORIGIN, not its current wave. The
    penguin walking toward the mountains — thousands of people restaged it, a
    UAE shop walked it to their store, it is the owner's own headline example
    of what a trend IS — carries **Year 2008**. A naive "Year must be recent"
    filter deletes the brief's flagship trend on the first run.

    So an old Year is a FLAG, never a verdict: the item is kept and stamped
    `revival`, and the entry's own numbers are exposed so downstream can
    require corroboration. The parser invents no threshold; it only makes the
    two cases distinguishable, and they are, by a factor of 500:

        penguin     71,598 views, 5 videos, 17 photos  (Year 2008)
        Mrs. Puff      135 views, 0 videos, 12 photos  (Year 2020)

    Both were published to Know Your Meme in the same week.
    """
    penguin = gts.parse_kym_entry_page(
        KYM_ENTRY_PENGUIN, url="https://knowyourmeme.com/memes/penguin", today=KYM_TODAY)
    puff = gts.parse_kym_entry_page(
        KYM_ENTRY_MRS_PUFF, url="https://knowyourmeme.com/memes/puff", today=KYM_TODAY)

    # KEPT. Not dropped, not empty, not degraded.
    assert penguin, "the penguin entry must parse"
    assert penguin["revival"] is True
    assert penguin["is_format"] is True          # exploitable + participatory-media
    assert penguin["rejected"] is False
    # and the prose says the WAVE is 2026 even though the ORIGIN is 2008 —
    # the honest fix for Year's ambiguity
    assert penguin["year"] == 2008
    assert penguin["wave_year"] == 2026

    # the stale encyclopedia entry is flagged exactly the same way...
    assert puff["revival"] is True and puff["is_format"] is True
    # ...and is told apart by NUMBERS the parser merely exposes
    assert penguin["views"] > puff["views"] * 500
    assert penguin["videos"] == 5.0 and puff["videos"] == 0.0

    # the whole item survives the round trip, not just the facts dict
    item = gts.kym_item_from_entry_page(
        penguin, url="https://knowyourmeme.com/memes/penguin", slug="penguin")
    assert item is not None
    _assert_raw_item(item)
    assert item["extra"]["revival"] is True
    assert item["extra"]["year"] == 2008 and item["extra"]["wave_year"] == 2026
    assert item["metric_value"] == 71598.0


def test_parse_kym_entry_page_deadpool_is_marked_rejected():
    """`Status: Deadpool` is KYM's editors saying "this is not a real meme".
    Every sampled deadpool entry had 191-833 views. It is recorded, not
    silently dropped, so downstream can see WHY something was rejected."""
    facts = gts.parse_kym_entry_page(
        KYM_ENTRY_DEADPOOL, url="https://knowyourmeme.com/memes/vogue", today=KYM_TODAY)
    assert facts["status"] == "Deadpool"
    assert facts["rejected"] is True
    assert facts["types"] == ["animal", "viral-video"]
    assert facts["is_format"] is False
    assert facts["views"] == 264.0
    # this entry has NO Region row at all — the parser must not invent one
    assert facts["region"] is None
    assert facts["origin"].startswith("https://www.tiktok.com/")


def test_parse_kym_entry_page_with_no_format_type_is_not_a_format():
    """Typed only `character`: an entry about a THING that exists, with nothing
    for anyone to make their own version of."""
    facts = gts.parse_kym_entry_page(
        KYM_ENTRY_NO_FORMAT_TYPE, url="https://knowyourmeme.com/memes/udin",
        today=KYM_TODAY)
    assert facts["types"] == ["character"]
    assert facts["is_format"] is False
    assert facts["rejected"] is False
    assert facts["revival"] is False            # Year 2025 is last year, not old
    assert facts["region"] == "Indonesia"


@pytest.mark.parametrize("types,expected", [
    (["participatory-media"], True),
    (["exploitable", "character"], True),       # one format type is enough
    (["dance"], True),
    (["snowclone", "image-macro"], True),
    (["character"], False),
    (["viral-video", "death"], False),
    (["animal"], False),                        # KYM ships types in NEITHER list
    ([], False),
    (None, False),
    ("participatory-media", False),             # a bare string is not a type list
])
def test_kym_is_format_matches_kyms_own_taxonomy(types, expected):
    assert gts.kym_is_format(types) is expected
    assert gts.KYM_FORMAT_TYPES & gts.KYM_NON_FORMAT_TYPES == frozenset()


@pytest.mark.parametrize("year,expected", [
    (2026, False),
    (2025, False),      # last year is still a new format
    (2024, True),
    (2020, True),       # Mrs. Puff
    (2008, True),       # the penguin
    (None, False),
    ("", False),
    ("not a year", False),
])
def test_kym_is_revival_flags_an_old_origin_year(year, expected):
    assert gts.kym_is_revival(year, today=KYM_TODAY) is expected


@pytest.mark.parametrize("text,expected", [
    ("The story was reported in mid-August 2026 and inspired memes that month.", 2026),
    ("In early 2026, the clip became a prevalent meme.", 2026),
    ("The character emerged online sometime in early 2025, spreading through remixes.", 2025),
    # a plain date with no wave word near it is not a wave
    ("Released on September 1st, 2007, and directed by Werner Herzog.", None),
    # the most recent wave wins when the prose names several
    ("First shared in 2019, it went viral again in 2026.", 2026),
    # a year past next year is a typo, not a wave
    ("The meme went viral in 2099.", None),
    ("", None),
    (None, None),
])
def test_kym_wave_year_reads_the_wave_out_of_the_prose(text, expected):
    """KYM's `Year` is the ORIGIN; the description states the WAVE in prose.
    Extracting it is the honest fix for that ambiguity — and it must never
    raise, because a bonus field may not sink an item."""
    assert gts.kym_wave_year(text, today=KYM_TODAY) == expected


def test_kym_apply_entry_facts_folds_the_taxonomy_into_extra_and_the_summary():
    items = gts.parse_kym_entry_feed(KYM_ALL_RSS, feed="all", today=KYM_TODAY)
    item = items[0]
    facts = gts.parse_kym_entry_page(
        KYM_ENTRY_PENGUIN, url="https://knowyourmeme.com/memes/penguin", today=KYM_TODAY)
    gts.kym_apply_entry_facts(item, facts)
    _assert_raw_item(item)
    extra = item["extra"]
    # everything the pipeline needs to set trend_kind FROM THE SOURCE rather
    # than pay a model to guess it
    for key in ("types", "year", "wave_year", "origin", "region", "status",
                "views", "videos", "photos", "revival", "rejected", "is_format"):
        assert key in extra, key
    assert extra["enriched"] is True
    # the badge, never a format type
    assert item["category"] == "Meme"
    # KYM's first ever real magnitude — labelled as KYM page traffic, because
    # that is what it is
    assert item["metric_value"] == 71598.0 and item["metric_label"] == "KYM views"
    # the scorer reads the summary, so the taxonomy has to be IN it
    assert "participatory media" in item["summary"]
    assert "71,598 views" in item["summary"]
    assert "status Confirmed" in item["summary"]
    assert len(item["summary"]) <= 600
    # the feed prose is still there in front of it
    assert item["summary"].startswith("Out Of Respect For Jayden Daniels")


def test_kym_enrichment_failure_degrades_to_the_feed_item():
    """One GET per entry is a cost, so it must also be optional: a 404, a
    timeout or a re-skinned page costs us the taxonomy for that entry and
    nothing else."""
    items = gts.parse_kym_entry_feed(KYM_ALL_RSS, feed="all", today=KYM_TODAY)
    before = json.loads(json.dumps(items[0]))
    for junk in ({}, None, "not a dict", {"title": None}):
        gts.kym_apply_entry_facts(items[0], junk)
    assert items[0] == before
    _assert_raw_item(items[0])
    # a half-parsed page still gives what it has and never raises
    partial = gts.parse_kym_entry_page("<html><body><p>nothing</p></body></html>",
                                       url="https://knowyourmeme.com/memes/x")
    gts.kym_apply_entry_facts(items[1], partial)
    _assert_raw_item(items[1])
    assert gts.parse_kym_entry_page("") == {}
    assert gts.parse_kym_entry_page(None) == {}


def test_kym_item_from_entry_page_needs_a_title():
    """A sitemap-only entry becomes an item only once its page names it."""
    assert gts.kym_item_from_entry_page({}, url="https://knowyourmeme.com/memes/x") is None
    assert gts.kym_item_from_entry_page({"views": 10.0},
                                        url="https://knowyourmeme.com/memes/x") is None
    # a page that gave a title and NOTHING else still yields a contract-clean
    # item: the RawItem summary cap does not depend on enrichment running
    bare = gts.kym_item_from_entry_page(
        {"title": "Nameless But Titled", "summary": "word " * 400},
        url="https://knowyourmeme.com/memes/bare")
    _assert_raw_item(bare)
    assert bare["extra"].get("enriched") is None
    facts = gts.parse_kym_entry_page(
        KYM_ENTRY_MRS_PUFF, url="https://knowyourmeme.com/memes/mrs-puff-in-a-padded-room",
        today=KYM_TODAY)
    item = gts.kym_item_from_entry_page(
        facts, url="https://knowyourmeme.com/memes/mrs-puff-in-a-padded-room",
        slug="mrs-puff-in-a-padded-room", lastmod="2026-08-19T19:43:43+00:00",
        rank=33, rank_total=40)
    _assert_raw_item(item)
    assert item["source"] == "kym_entries"
    assert item["source_key"] == "mrs-puff-in-a-padded-room"
    assert item["title"] == "Mrs. Puff In a Padded Room"
    assert item["extra"]["feed"] == "sitemap"
    assert (item["extra"]["rank"], item["extra"]["rank_total"]) == (33, 40)
    assert item["extra"]["revival"] is True
    assert item["metric_value"] == 135.0


# =========================================================================== #
# 28. YouTube Shorts — the songs creators USED most
#
# Real trimmed InnerTube reply (AE, WEEKLY, captured 2026-08-20). Three rows:
# the #1 with a previousPosition, a row with NO previousPosition (which is
# "not on the previous chart", not "new"), and a row with no encryptedVideoId
# that has to be skipped rather than emitted.
# =========================================================================== #

SHORTS_JSON = {
    "responseContext": {"visitorData": "Cgt4eHh4eHh4eHh4eCiZ"},
    "contents": {"sectionListRenderer": {"contents": [{
        "musicAnalyticsSectionRenderer": {"content": {
            "perspectiveMetadata": {"requestParams": {
                "perspective": "CHART_DETAILS",
                "chartParams": {"countryCode": "ae",
                                "chartType": "CHART_TYPE_SHORTS_TRACKS_BY_USAGE",
                                "chartPeriodType": "CHART_PERIOD_TYPE_WEEKLY"}}},
            "trackTypes": [{
                "listType": "TOP_SHORTS_BY_USAGE",
                "chartPeriodType": "CHART_PERIOD_TYPE_WEEKLY",
                "endDate": "2026-08-13",
                "trackViews": [
                    {
                        "id": "G:i2a8efuDatw",
                        "name": "Headlights (feat. KIDDO)",
                        "thumbnail": {"thumbnails": [{
                            "url": "https://yt3.googleusercontent.com/qziwPDf687hi=w180-h180-l90-rj",
                            "width": 180, "height": 180}]},
                        "encryptedVideoId": "zDy8K0o_ksA",
                        "chartEntryMetadata": {"currentPosition": 1, "previousPosition": 3,
                                               "percentViewsChange": 0.035680305,
                                               "periodsOnChart": 31},
                        "artists": [{"kgMid": "/g/11c5bsbjd9", "name": "Alok"},
                                    {"kgMid": "/g/11bxf4crlf", "name": "Alan Walker"}],
                        "isVisible": True,
                        "releaseDate": {"year": 2022, "month": 2, "day": 17},
                    },
                    {
                        "id": "G:OI5DVvnOIh4",
                        "name": "Main Yahaan Hoon",
                        "thumbnail": {"thumbnails": [{
                            "url": "https://yt3.googleusercontent.com/BZBfTByEyZo6=w180-h180-l90-rj"}]},
                        "encryptedVideoId": "m6Y8xEfyXTs",
                        "chartEntryMetadata": {"currentPosition": 15,
                                               "percentViewsChange": 0.29257643,
                                               "periodsOnChart": 17},
                        "artists": [{"kgMid": "/m/031104", "name": "Udit Narayan"}],
                        "isVisible": True,
                    },
                    {
                        "id": "G:broken",
                        "name": "A track with no video id",
                        "chartEntryMetadata": {"currentPosition": 50},
                        "artists": [],
                    },
                ],
            }],
        }},
    }]}},
}


def test_parse_youtube_shorts_chart_row():
    items = gts.parse_youtube_shorts(SHORTS_JSON, "ae", "WEEKLY")
    assert len(items) == 2                       # the id-less row is skipped
    for it in items:
        _assert_raw_item(it)
    a, b = items
    assert a["source"] == "youtube_shorts"
    assert a["source_key"] == "ytshorts:zDy8K0o_ksA:AE"
    assert a["title"] == "Headlights (feat. KIDDO) — Alok, Alan Walker"
    # the SOUND page: literally the list of people who already made their own
    assert a["url"] == "https://www.youtube.com/source/zDy8K0o_ksA/shorts"
    assert a["image_url"].startswith("https://yt3.googleusercontent.com/")
    assert a["region"] == "AE"
    assert a["category"] == "music"
    # published_at is the CHART PERIOD's end date, not a release date
    assert a["published_at"] == "2026-08-13T00:00:00+00:00"
    assert a["extra"]["chart_end_date"] == "2026-08-13"
    assert a["extra"]["list_type"] == "TOP_SHORTS_BY_USAGE"
    assert a["extra"]["country"] == "AE" and a["extra"]["period"] == "WEEKLY"
    assert a["extra"]["artists"] == ["Alok", "Alan Walker"]
    assert a["extra"]["periods_on_chart"] == 31.0
    assert a["extra"]["previous_position"] == 3.0
    assert a["extra"]["percent_change"] == pytest.approx(0.035680305)
    # the summary has to say what the chart MEANS — using a sound is making one
    assert "creators used most" in a["summary"]
    # rank_total is the CHART's length (3 raw rows), not the number we could
    # emit — the ranking curve's fanout must be the real list length.
    assert a["summary"].startswith("#1 of 3")
    assert "Up from #3." in a["summary"]
    # no previousPosition is "not on the previous chart", NOT "new to the chart"
    assert b["extra"]["prev_rank"] is None
    assert "Not on the previous chart." in b["summary"]
    assert b["title"] == "Main Yahaan Hoon — Udit Narayan"


def test_youtube_shorts_reports_a_rank_not_an_invented_headcount():
    """An ordinal is not an audience. Turning "#3 of 50" into a metric_value
    would invent a magnitude and the ranking module would then score that
    invention against the source's own median as though it were measured. So
    metric_value stays None and the real numbers go where the ranking module
    actually reads them: extra["rank"]/["rank_total"] for the rank curve,
    extra["prev_rank"]/["growth_ratio"] for the growth term."""
    items = gts.parse_youtube_shorts(SHORTS_JSON, "AE")
    a, b = items
    assert a["metric_value"] is None and a["metric_label"] is None
    assert (a["extra"]["rank"], a["extra"]["rank_total"]) == (1.0, 3.0)
    assert a["extra"]["prev_rank"] == 3.0
    # growth_ratio is YouTube's OWN week-over-week usage change, not ours
    assert a["extra"]["growth_ratio"] == pytest.approx(1.035680305)
    assert b["extra"]["growth_ratio"] == pytest.approx(1.29257643)
    # ...and the ranking module's rank curve can actually consume it
    from app.services import general_trends_ranking as gtr
    assert gtr.ratio_from_rank(a["extra"]["rank"], a["extra"]["rank_total"]) is not None


def test_parse_youtube_shorts_junk():
    assert gts.parse_youtube_shorts({}, "AE") == []
    assert gts.parse_youtube_shorts(None, "AE") == []
    assert gts.parse_youtube_shorts({"contents": {"sectionListRenderer": {}}}, "AE") == []
    assert gts.youtube_shorts_chart({"contents": "nope"}) is None
    assert gts.youtube_shorts_chart(None) is None


def test_youtube_shorts_body_carries_the_load_bearing_flag_and_country():
    """The `flags=` is what unlocks the Shorts charts at all, and gl and
    chart_params_country_code must agree or the reply is for a country other
    than the one on the card."""
    body = gts.youtube_shorts_body("ae")
    assert body["browseId"] == gts.YOUTUBE_CHARTS_BROWSE_ID
    assert body["context"]["client"]["clientName"] == "WEB_MUSIC_ANALYTICS"
    assert body["context"]["client"]["gl"] == "AE"
    q = body["query"]
    assert "flags=MusicCharts__enable_apac_and_shorts_charts_expansion" in q
    assert "chart_params_country_code=ae" in q
    assert "chart_params_chart_type=SHORTS_TRACKS_BY_USAGE" in q
    assert "chart_params_period_type=WEEKLY" in q
    assert "chart_params_period_type=DAILY" in gts.youtube_shorts_body("us", "daily")["query"]


# =========================================================================== #
# 29. Fetchers for the KYM family and the Shorts chart
# =========================================================================== #

class _FakeRpcClient(_FakeClient):
    """_FakeClient plus a POST (the Shorts chart is an InnerTube RPC)."""

    async def post(self, url, params=None, json=None, headers=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {}), "json": json, "method": "POST"})
        return self.route(url, params or {}, headers or {})


def _kym_route(**overrides):
    """Route the whole KYM family; `overrides` replaces one response by key."""
    pages = {
        "https://knowyourmeme.com/memes/out-of-respect-for-jayden-daniels-number-5-jersey-controversy":
            KYM_ENTRY_DEADPOOL,
        "https://knowyourmeme.com/memes/udin-din-din-dun": KYM_ENTRY_NO_FORMAT_TYPE,
        "https://knowyourmeme.com/memes/mrs-puff-in-a-padded-room": KYM_ENTRY_MRS_PUFF,
        "https://knowyourmeme.com/memes/kinda-chic-trend": KYM_ENTRY_PENGUIN,
    }

    def route(url, p, h):
        if url in overrides:
            return overrides[url]
        if url == gts.KYM_ENTRY_FEEDS["all"]:
            return _FakeResponse(text=KYM_ALL_RSS)
        if url == gts.KYM_ENTRY_FEEDS["confirmed"]:
            return _FakeResponse(text=KYM_ALL_RSS.replace(
                "udin-din-din-dun", "kinda-chic-trend"))
        if url == gts.KYM_SITEMAP_URL:
            return _FakeResponse(text=KYM_SITEMAP_XML)
        if url == gts.KYM_EDITORIALS_URL:
            return _FakeResponse(text=KYM_EDITORIALS_RSS)
        if url in pages:
            return _FakeResponse(text=pages[url])
        return _FakeResponse(404)
    return route


def test_fetch_kym_entries_merges_the_three_feeds_and_dedupes():
    client = _FakeClient(_kym_route())
    items = _run(gts.fetch_kym_entries(client, today=KYM_TODAY))
    feeds = [it["extra"]["feed"] for it in items]
    keys = [it["source_key"] for it in items]
    assert feeds.count("all") == 2
    assert feeds.count("confirmed") == 1          # the shared slug is not duplicated
    assert feeds.count("sitemap") == 1            # only the one whose page resolves
    assert keys.count("out-of-respect-for-jayden-daniels-number-5-jersey-controversy") == 1
    assert "mrs-puff-in-a-padded-room" in keys
    for it in items:
        _assert_raw_item(it)
        assert it["source"] == "kym_entries"
    # every one of the three feeds was actually asked for
    asked = [c["url"] for c in client.calls]
    assert gts.KYM_ENTRY_FEEDS["all"] in asked
    assert gts.KYM_ENTRY_FEEDS["confirmed"] in asked
    assert gts.KYM_SITEMAP_URL in asked
    # and the entries came back enriched with KYM's own taxonomy
    enriched = [it for it in items if it["extra"].get("enriched")]
    assert enriched and all("types" in it["extra"] for it in enriched)


def test_fetch_kym_entries_bounds_the_entry_page_fetches():
    """One GET per entry page, so the budget is a MODULE CONSTANT and it is a
    per-RUN number — which is why all.rss, confirmed.rss and the sitemap are
    one unit sharing it rather than three units each spending it."""
    client = _FakeClient(_kym_route())
    _run(gts.fetch_kym_entries(client, enrich_max=1, today=KYM_TODAY))
    entry_gets = [c["url"] for c in client.calls
                  if c["url"].startswith(gts.KYM_ENTRY_URL_PREFIX)
                  and not c["url"].endswith(".rss")]
    assert len(entry_gets) == 1
    # zero budget is honoured too: feeds only, no page fetches at all
    client = _FakeClient(_kym_route())
    items = _run(gts.fetch_kym_entries(client, enrich_max=0, today=KYM_TODAY))
    assert not any(c["url"].startswith(gts.KYM_ENTRY_URL_PREFIX)
                       and not c["url"].endswith(".rss") for c in client.calls)
    assert items and not any(it["extra"].get("enriched") for it in items)
    assert gts.KYM_ENTRY_FETCH_MAX == 40


def test_fetch_kym_entries_survives_a_dead_branch_and_a_dead_entry_page():
    """all.rss is the trunk; confirmed.rss, the sitemap and every entry page
    are branches. Losing a branch must cost exactly that branch."""
    client = _FakeClient(_kym_route(**{
        gts.KYM_ENTRY_FEEDS["confirmed"]: _FakeResponse(503),
        gts.KYM_SITEMAP_URL: _FakeResponse(500),
        "https://knowyourmeme.com/memes/udin-din-din-dun": _FakeResponse(404),
    }))
    items = _run(gts.fetch_kym_entries(client, today=KYM_TODAY))
    assert [it["extra"]["feed"] for it in items] == ["all", "all"]
    # the entry whose page 404'd is still here, just without the taxonomy
    plain = [it for it in items if it["source_key"] == "udin-din-din-dun"][0]
    _assert_raw_item(plain)
    assert plain["extra"].get("enriched") is None
    assert plain["title"] == "Udin Din Din Dun"
    assert plain["summary"]


def test_fetch_kym_entries_raises_when_the_trunk_feed_goes_quiet():
    """KYM has already retired four sibling endpoints on us. The fifth will not
    404 — it will answer 200 with nothing in it, and that is invisible for
    weeks unless the fetcher raises (the TikTokLayoutChanged precedent)."""
    client = _FakeClient(_kym_route(**{
        gts.KYM_ENTRY_FEEDS["all"]: _FakeResponse(text="<rss><channel/></rss>")}))
    with pytest.raises(gts.KymEntryLayoutChanged):
        _run(gts.fetch_kym_entries(client, today=KYM_TODAY))
    # a real HTTP error still surfaces as an HTTP error
    client = _FakeClient(_kym_route(**{gts.KYM_ENTRY_FEEDS["all"]: _FakeResponse(503)}))
    with pytest.raises(httpx.HTTPStatusError):
        _run(gts.fetch_kym_entries(client, today=KYM_TODAY))


def test_fetch_kym_editorials():
    client = _FakeClient(_kym_route())
    items = _run(gts.fetch_kym_editorials(client, today=KYM_TODAY))
    assert client.calls[0]["url"] == gts.KYM_EDITORIALS_URL
    assert len(items) == 2 and items[0]["source"] == "kym_editorials"
    client = _FakeClient(_kym_route(**{
        gts.KYM_EDITORIALS_URL: _FakeResponse(text="<rss><channel/></rss>")}))
    with pytest.raises(gts.KymEntryLayoutChanged):
        _run(gts.fetch_kym_editorials(client))


def test_fetch_youtube_shorts_posts_the_innertube_rpc():
    client = _FakeRpcClient(lambda u, p, h: _FakeResponse(payload=SHORTS_JSON))
    items = _run(gts.fetch_youtube_shorts(client, region="ae"))
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == gts.YOUTUBE_CHARTS_URL
    assert call["params"] == {"alt": "json"}
    assert call["json"]["context"]["client"]["gl"] == "AE"
    assert "chart_params_country_code=ae" in call["json"]["query"]
    assert len(items) == 2 and items[0]["region"] == "AE"
    # keyless, cookieless: nothing that looks like a credential goes out
    assert not any(k.lower() in ("authorization", "cookie", "x-goog-api-key")
                   for k in call["headers"])


def test_fetch_youtube_shorts_rejects_a_region_that_is_not_a_country_code():
    """No allow-list is kept, and none is needed: an unknown country is HTTP
    400 here, NOT a silent fallback to US the way TikTok's Creative Center is.
    Only the obviously-not-a-country case is caught before the request."""
    client = _FakeRpcClient(lambda u, p, h: _FakeResponse(payload=SHORTS_JSON))
    for bad in ("", "united-arab-emirates", "A", "AEX"):
        with pytest.raises(ValueError):
            _run(gts.fetch_youtube_shorts(client, region=bad))
    assert client.calls == []


@pytest.mark.parametrize("mutate,why", [
    (lambda d: d.pop("contents"), "renderer gone"),
    (lambda d: d["contents"]["sectionListRenderer"]["contents"][0]
     ["musicAnalyticsSectionRenderer"]["content"]["trackTypes"][0]
     .__setitem__("trackViews", []), "zero rows"),
    (lambda d: d["contents"]["sectionListRenderer"]["contents"][0]
     ["musicAnalyticsSectionRenderer"]["content"]["trackTypes"][0]
     .__setitem__("listType", "TOP_TRACKS"), "a listening chart, not a usage chart"),
    (lambda d: d["contents"]["sectionListRenderer"]["contents"][0]
     ["musicAnalyticsSectionRenderer"]["content"].__setitem__("trackTypes", []),
     "trackTypes emptied"),
])
def test_fetch_youtube_shorts_alarms_on_schema_drift_not_just_non_200(mutate, why):
    """THE guard. This is an undocumented InnerTube RPC: it can keep answering
    200 while renaming a field or swapping the chart, and a source that quietly
    returns nothing goes unnoticed for weeks. Alarm on the SHAPE."""
    payload = json.loads(json.dumps(SHORTS_JSON))
    mutate(payload)
    client = _FakeRpcClient(lambda u, p, h: _FakeResponse(payload=payload))
    with pytest.raises(gts.ShortsChartChanged):
        _run(gts.fetch_youtube_shorts(client, region="AE"))


def test_fetch_youtube_shorts_still_raises_on_a_real_http_error():
    client = _FakeRpcClient(lambda u, p, h: _FakeResponse(400))
    with pytest.raises(httpx.HTTPStatusError):
        _run(gts.fetch_youtube_shorts(client, region="ZZ"))


# =========================================================================== #
# 30. build_fetch_plan + dispatch for the new sources
# =========================================================================== #

def test_build_fetch_plan_includes_the_kym_family_and_the_shorts_regions():
    plan = gts.build_fetch_plan(
        sources=["kym", "kym_entries", "kym_editorials", "youtube_shorts"],
        geos=["US"], subreddits=[], youtube_api_key="",
        shorts_regions=["ae", "SA", "us", "ae"])
    units = [u["unit"] for u in plan]
    assert units == ["kym", "kym_entries", "kym_editorials",
                     "youtube_shorts:AE", "youtube_shorts:SA", "youtube_shorts:US"]
    # kym_entries is ONE unit: all.rss + confirmed.rss + the sitemap share a
    # single per-run entry-page budget, which three units could not do
    assert len([u for u in plan if u["source"] == "kym_entries"]) == 1
    ae = next(u for u in plan if u["unit"] == "youtube_shorts:AE")
    assert ae["region"] == "AE"
    assert ae["params"] == {"region": "AE", "period": "WEEKLY"}
    for u in plan:
        assert u["label"] == gts.SOURCE_LABELS[u["source"]]
    # the shorts chart is not geo-expanded off GENERAL_TRENDS_GEOS: it has its
    # own region list, because the whole point of it is that AE differs from US
    assert not any(u["unit"] == "youtube_shorts:US" and u["region"] == "US"
                   for u in gts.build_fetch_plan(
                       sources=["youtube_shorts"], geos=["US"], subreddits=[],
                       youtube_api_key="", shorts_regions=[]))


def test_shorts_regions_default_to_the_gulf_but_an_empty_list_turns_it_off():
    """The one source that does NOT follow "empty config ⇒ no units": it is
    keyless and geo-correct, so there is nothing to configure before it works
    and a caller that has not learned the new setting should still get AE."""
    plan = gts.build_fetch_plan(sources=["youtube_shorts"], geos=[], subreddits=[],
                                youtube_api_key="")
    assert [u["region"] for u in plan] == list(gts.SHORTS_DEFAULT_REGIONS)
    assert gts.SHORTS_DEFAULT_REGIONS == ("AE", "SA", "US")
    assert gts.build_fetch_plan(sources=["youtube_shorts"], geos=[], subreddits=[],
                                youtube_api_key="", shorts_regions=[]) == []


def test_fetch_all_dispatches_the_kym_family_and_the_shorts_chart(monkeypatch):
    seen: list[tuple] = []

    async def fake_entries(client, **kw):
        seen.append(("entries",))
        return gts.parse_kym_entry_feed(KYM_ALL_RSS, feed="all", today=KYM_TODAY)

    async def fake_editorials(client, **kw):
        seen.append(("editorials",))
        return gts.parse_kym_editorials_rss(KYM_EDITORIALS_RSS, today=KYM_TODAY)

    async def fake_shorts(client, *, region, period="WEEKLY"):
        seen.append(("shorts", region, period))
        return gts.parse_youtube_shorts(SHORTS_JSON, region, period)

    monkeypatch.setattr(gts, "fetch_kym_entries", fake_entries)
    monkeypatch.setattr(gts, "fetch_kym_editorials", fake_editorials)
    monkeypatch.setattr(gts, "fetch_youtube_shorts", fake_shorts)

    plan = gts.build_fetch_plan(
        sources=["kym_entries", "kym_editorials", "youtube_shorts"],
        geos=[], subreddits=[], youtube_api_key="", shorts_regions=["AE", "US"])
    items, stats = _run(gts.fetch_all(plan, client=object()))
    assert ("entries",) in seen and ("editorials",) in seen
    assert ("shorts", "AE", "WEEKLY") in seen and ("shorts", "US", "WEEKLY") in seen
    assert stats["kym_entries"]["count"] == 2
    assert stats["kym_editorials"]["count"] == 2
    assert stats["youtube_shorts"]["count"] == 4      # two regions x two rows
    assert all(st["ok"] for st in stats.values())
    for it in items:
        _assert_raw_item(it)
    # the two regions produce DISTINCT keys for the same track — an AE trend
    # and a US trend are not the same sighting
    keys = {it["source_key"] for it in items if it["source"] == "youtube_shorts"}
    assert "ytshorts:zDy8K0o_ksA:AE" in keys and "ytshorts:zDy8K0o_ksA:US" in keys


def test_the_new_sources_are_not_serial_and_are_global_or_regional_correctly():
    assert "kym_entries" in gts.GLOBAL_SOURCES
    assert "kym_editorials" in gts.GLOBAL_SOURCES
    assert "youtube_shorts" not in gts.GLOBAL_SOURCES     # it is per-country
    # none of them throttles per client, so none belongs in the serial lane
    assert gts.SERIAL_SOURCES.isdisjoint(
        {"kym_entries", "kym_editorials", "youtube_shorts"})
