"""Tests for the General Trends pipeline, ideation, poller helper and router glue.

Modules under test:

  * ``app/services/general_trends_pipeline.py``  — normalise_title, clustering
    (exact key → exact slug → in-run → KNN → new), embedding degradation, the
    scoring prompt/parse/pass, ``run_refresh`` end to end
  * ``app/services/general_trend_ideation.py``   — the "Make this" prompt + envelope
  * ``app/workers/general_trends_poller.py``     — ``auto_refresh_due``
  * ``app/routers/general_trends.py``            — the pure ``_source_items`` merge

Everything is mocked: no network, no database, no Vertex. The store is swapped
for ``_FakeStore`` (async methods only, records what the pipeline persisted),
the sources' ``fetch_all`` for a canned result, ``gemini_embed`` /
``gemini_text`` / ``_brand_note`` for fakes — every one a module attribute the
pipeline looks up BY NAME at call time.

There is no pytest-asyncio in this venv, so coroutines are driven with
``asyncio.run`` via ``_run``, matching tests/test_trend_finder.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.routers import general_trends as router_mod  # noqa: E402
from app.services import general_trend_ideation as gti  # noqa: E402
from app.services import general_trends_pipeline as gtp  # noqa: E402
from app.services import general_trends_sources as gts  # noqa: E402
from app.services import trend_ideation as ti  # noqa: E402
from app.workers import general_trends_poller as poller  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


# =========================================================================== #
# 1. normalise_title — the exact-match key
# =========================================================================== #

@pytest.mark.parametrize("raw, slug", [
    ("The Nihilist Penguin", "nihilist penguin"),
    ("#TheNihilistPenguin", "thenihilistpenguin"),
    ("@elonmusk", "elonmusk"),
    ("Spider-Man: Brand New Day", "spider man brand new day"),
    ("Don't Panic!!", "dont panic"),
    ("  A   Minecraft   Movie ", "minecraft movie"),
    ("An apple a day", "apple a day"),                # only the LEADING article goes
    ("Hayden_Panettiere", "hayden panettiere"),
    ("ｆｕｌｌｗｉｄｔｈ", "fullwidth"),                  # NFKC folds full-width forms
    ("PETE HEGSETH", "pete hegseth"),
    ("r/memes", "r memes"),
])
def test_normalise_title_cases(raw, slug):
    assert gtp.normalise_title(raw) == slug


@pytest.mark.parametrize("raw", ["", None, "   ", "!!!", "#", "The", "a", 42 and ""])
def test_normalise_title_empty_or_junk_gives_empty(raw):
    assert gtp.normalise_title(raw) == ""


def test_normalise_title_is_idempotent():
    once = gtp.normalise_title("The Nihilist-Penguin!")
    assert gtp.normalise_title(once) == once == "nihilist penguin"


def test_embed_text_for_uses_title_and_a_slice_of_summary():
    item = {"title": " Pete  Hegseth ", "summary": "x" * 1000}
    txt = gtp.embed_text_for(item)
    assert txt.startswith("Pete Hegseth. ")
    assert len(txt) == len("Pete Hegseth. ") + gtp.EMBED_SUMMARY_CHARS
    assert gtp.embed_text_for({"title": "solo"}) == "solo"
    # Wikipedia's summary is a stats line, identical for every article — it must
    # not enter the vector or all forty articles collapse into one trend.
    assert gtp.embed_text_for({"source": "wikipedia", "title": "Hayden Panettiere",
                               "summary": "#1 most-viewed on English Wikipedia · 10,392,637 views"})         == "Hayden Panettiere"


def test_there_is_no_cosine_merge_threshold_any_more():
    """REWRITTEN (this test used to pin ``KNN_DISTANCE_MAX <= 0.13``).

    Over all 65,703 pairs of the 363 vectors of the first live run,
    count(distance <= 0.12) = 0 and the closest pair in the whole corpus
    (0.1230) is FALSE — "juju smith" ~ "najee harris" — so no threshold exists
    at any usable recall. The vectors are a candidate-HINT generator now, and
    the constant that remains must be a hint width, not a verdict."""
    assert not hasattr(gtp, "KNN_DISTANCE_MAX")
    assert gtp.KNN_HINT_MAX_DISTANCE >= 0.2      # generous on purpose: it decides nothing


# =========================================================================== #
# 2. The fake store (async methods only — records what was persisted)
# =========================================================================== #

class _FakeStore:
    def __init__(self, *, trends: list[dict] | None = None,
                 source_keys: dict | None = None, nearest=None):
        self.trends: dict[int, dict] = {}
        for t in trends or []:
            self.trends[int(t["id"])] = dict(t)
        # (source, source_key) -> trend id, as the sightings join would find
        self.source_keys: dict[tuple, int] = dict(source_keys or {})
        self.nearest = nearest                  # list | callable(embedding) -> list
        self.nearest_calls: list = []
        self.canonical_lookups: list = []       # keys asked for (rung 3)
        self.canonical_writes: list = []        # (trend_id, key) stamped
        self.sightings: list[tuple] = []        # (trend_id, run_id, item, seen_at)
        self.touches: list[tuple] = []          # (trend_id, item, seen_at)
        self.created: list[dict] = []
        self.thumbs: list[tuple] = []
        self.velocities: dict[int, float] = {}
        self.scores: dict[int, dict] = {}
        self.cleared: list = []
        self.run_updates: list[tuple] = []      # (run_id, fields)
        self.runs: dict[int, dict] = {}
        self.evidence: dict[int, list] = {}
        self._next_id = 100

    # -- runs -------------------------------------------------------------
    async def ensure_tables(self):
        return None

    async def create_run(self, metadata=None):
        self._next_id += 1
        run = {"id": self._next_id, "status": "queued", "metadata": dict(metadata or {}),
               "progress": None, "active": True, "finished_at": None}
        self.runs[run["id"]] = run
        return dict(run)

    async def update_run(self, run_id, **fields):
        self.run_updates.append((run_id, dict(fields)))
        run = self.runs.setdefault(run_id, {"id": run_id, "status": "queued", "metadata": {}})
        run.update(fields)
        if fields.get("status") in ("done", "failed"):
            # Real wall-clock, NOT the frozen _NOW: the poller compares
            # finished_at against datetime.now(), so a fixed stamp makes this
            # fake look "6 hours stale" the moment the calendar rolls past
            # _NOW and the auto-refresh test becomes a time bomb.
            run["finished_at"] = datetime.now(timezone.utc)
        run["active"] = run.get("status") not in ("done", "failed")

    async def latest_run(self):
        if not self.runs:
            return None
        return dict(self.runs[max(self.runs)])

    async def run_is_active(self):
        run = await self.latest_run()
        return bool(run and run.get("active"))

    # -- lookups ----------------------------------------------------------
    async def get_trend(self, trend_id):
        row = self.trends.get(int(trend_id))
        return dict(row) if row else None

    async def find_trend_by_slug(self, slug, *, since_days=30):
        for t in self.trends.values():
            if t.get("slug") == slug:
                return dict(t)
        return None

    async def find_trend_by_canonical_key(self, key, *, since_days=30):
        self.canonical_lookups.append(key)
        for t in self.trends.values():
            if t.get("canonical_key") and t["canonical_key"] == key:
                return dict(t)
        return None

    async def set_canonical_key(self, trend_id, key):
        self.canonical_writes.append((int(trend_id), key))
        row = self.trends.get(int(trend_id))
        if row is not None and key:
            row["canonical_key"] = key

    async def find_trend_by_source_key(self, source, source_key, since_days=30):
        tid = self.source_keys.get((source, source_key))
        return dict(self.trends[tid]) if tid in self.trends else None

    async def heartbeat_run(self, run_id):
        self.heartbeats = getattr(self, "heartbeats", 0) + 1

    async def recent_trend_slugs(self, since_days=14):
        return [{"id": tid, "slug": t.get("slug") or "", "title": t.get("title")}
                for tid, t in sorted(self.trends.items())]

    async def nearest_trends(self, embedding, *, limit=5, since_days=14,
                             max_distance=0.20):
        self.nearest_calls.append(list(embedding))
        if callable(self.nearest):
            return list(self.nearest(embedding))
        return list(self.nearest or [])

    # -- writes -----------------------------------------------------------
    async def create_trend(self, fields):
        self._next_id += 1
        row = {"id": self._next_id, "local_thumb_path": None, "scored_at": None,
               "velocity": None, "fit_score": None, "uae_relevance": None,
               **{k: v for k, v in fields.items() if k != "embedding"}}
        row["_embedding"] = fields.get("embedding")
        row["source_count"] = len(row.get("sources") or [])
        self.trends[row["id"]] = row
        self.created.append(dict(row))
        return dict(row)

    async def touch_trend(self, trend_id, *, item, seen_at):
        self.touches.append((int(trend_id), item, seen_at))
        row = self.trends.get(int(trend_id))
        if row is not None:
            row["sighting_count"] = int(row.get("sighting_count") or 0) + 1
            for key, val in (("sources", item.get("source")), ("regions", item.get("region"))):
                lst = list(row.get(key) or [])
                if val and val not in lst:
                    lst.append(val)
                row[key] = lst
            row["source_count"] = len(row.get("sources") or [])
            if not row.get("image_url") and item.get("image_url"):
                row["image_url"] = item["image_url"]
            row["last_seen_at"] = seen_at

    async def insert_sighting(self, trend_id, run_id, item, seen_at=None):
        self.sightings.append((int(trend_id), run_id, item, seen_at))
        self.source_keys[(item.get("source"), item.get("source_key"))] = int(trend_id)

    async def set_local_thumb(self, trend_id, rel_path):
        self.thumbs.append((int(trend_id), rel_path))
        self.trends[int(trend_id)]["local_thumb_path"] = rel_path

    # -- velocity + scoring -----------------------------------------------
    async def recent_sightings_for_velocity(self, since_hours=72):
        out: dict[int, list] = {}
        for tid, _run, item, seen_at in self.sightings:
            out.setdefault(tid, []).append({"seen_at": seen_at or _NOW,
                                            "source": item.get("source")})
        return out

    async def query_trends(self, **kw):
        status = kw.get("status")
        rows = [dict(t) for t in self.trends.values()]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        else:
            rows = [r for r in rows if r.get("status") != "dismissed"]
        return rows

    async def save_velocity(self, trend_id, velocity):
        self.velocities[int(trend_id)] = float(velocity)
        self.trends[int(trend_id)]["velocity"] = float(velocity)

    async def unscored_trends(self, limit=400):
        return [dict(t) for t in self.trends.values() if not t.get("scored_at")][:limit]

    async def sightings_for(self, trend_ids, *, per_trend=6):
        return {int(i): list(self.evidence.get(int(i), []))[:per_trend] for i in trend_ids}

    async def save_score(self, trend_id, score):
        self.scores[int(trend_id)] = dict(score)
        row = self.trends[int(trend_id)]
        row.update({k: v for k, v in score.items()})
        row["scored_at"] = _NOW

    async def clear_scores(self, ids=None):
        self.cleared.append(ids)
        n = 0
        for tid, t in self.trends.items():
            if ids is None or tid in ids:
                t["scored_at"] = None
                n += 1
        return n

    async def theme_rollup(self, *a, **k):
        return []

    async def stats(self):
        return {}


def _item(source, key, title, *, region="global", summary="", image_url=None,
          metric=None, label=None, url=None):
    return gts.make_item(source=source, source_key=key, title=title, url=url,
                         summary=summary, image_url=image_url, metric_value=metric,
                         metric_label=label, region=region)


def _vec(*coords):
    """A short controlled vector (the matcher only cares about direction)."""
    return [float(c) for c in coords]


def _fake_embed(monkeypatch, mapping: dict | None = None, *, calls: list | None = None,
                boom: Exception | None = None):
    """gemini_embed stand-in: maps each text's TITLE (before '. ') to a vector."""
    async def _embed(texts, **kw):
        if calls is not None:
            calls.append(list(texts))
        if boom is not None:
            raise boom
        out = []
        for t in texts:
            title = t.split(". ", 1)[0]
            out.append(list((mapping or {}).get(title) or _vec(0, 0, 1)))
        return out
    monkeypatch.setattr(gtp, "gemini_embed", _embed)


# =========================================================================== #
# 3. cluster_items — the trust ladder
# =========================================================================== #

def test_cluster_exact_slug_merge_touches_the_existing_trend(monkeypatch):
    store = _FakeStore(trends=[{"id": 7, "slug": "nihilist penguin", "title": "nihilist penguin",
                                "sources": ["reddit"], "regions": ["global"],
                                "sighting_count": 1, "image_url": None,
                                "local_thumb_path": None}])
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("tiktok", "bsky-1", "The Nihilist Penguin", image_url="https://i/x.png")]

    res = _run(gtp.cluster_items(items, run_id=1, embed=None, seen_at=_NOW))

    assert res["new"] == 0 and res["touched"] == 1 and res["skipped"] == 0
    assert res["attached"] == [(7, items[0])]
    assert res["trend_ids"] == [7]
    assert res["embeddings"] == "off"
    assert store.created == []
    assert [(s[0], s[1], s[2]["source_key"], s[3]) for s in store.sightings] == \
        [(7, 1, "bsky-1", _NOW)]
    assert [(t[0], t[1]["source"]) for t in store.touches] == [(7, "tiktok")]
    assert store.trends[7]["sources"] == ["reddit", "tiktok"]
    assert res["image_urls"] == {7: "https://i/x.png"}


# ---- lexical matcher (name containment / token overlap) -------------------

@pytest.mark.parametrize("a, b", [
    # measured on the first live runs — one source names the thing, another the story
    ("hayden panettiere", "hayden panettiere dies at 36"),
    ("natalie harp", "trump aide natalie harp controversy"),
    ("nancy mace", "nancy mace tattoo reveal sparks discourse on x"),
    ("al ittihad vs al nejmeh football match", "al najma vs al ittihad football match"),
    ("lanterns hbo series premiere", "lanterns tv series"),
    ("nihilist penguin", "nihilist penguin"),
])
def test_slugs_match_lexically_positive(a, b):
    assert gtp.slugs_match_lexically(a, b) is True
    assert gtp.slugs_match_lexically(b, a) is True


@pytest.mark.parametrize("a, b", [
    # embedding distance put these UNDER 0.16 — the name rule must not merge them
    ("juju smith schuster nfl player", "najee harris nfl trend"),
    ("nba 2k series discussion", "nba 2k mycareer mode"),      # "nba 2k" too short/generic
    ("gta 6 hype", "nba 2k series discussion"),
    ("emirates", "uae ministry of defence"),                    # one-word names never contain
    ("today is tuesday", "international day trending in uae"),
    ("uae intercepts missiles", "reports of missile attack on uae"),
    ("", "anything"), ("x", ""),
])
def test_slugs_match_lexically_negative(a, b):
    assert gtp.slugs_match_lexically(a, b) is False


@pytest.mark.parametrize("a, b", [
    # REGRESSION, and the reason the stopword list was rewritten. It stripped
    # the CONTENT words and kept the TEMPLATE words, so these two shared
    # {what, going} out of a 4-token union and hit Jaccard EXACTLY 0.5. There
    # are 12+ "What's going on with X" posts in the live corpus, and greedy
    # first-match plus transitive chaining collapsed them into one mega-card.
    ("whats going on with tom segura", "whats going on with github"),
    ("whats going on with the nba", "whats going on with my landlord"),
    ("why does everyone hate javascript", "why does everyone hate pineapple pizza"),
    ("anyone else seeing this update", "anyone else getting this error"),
    # Mention is not topic: containment fired on a name buried at the END of a
    # long sentence about something else entirely.
    ("natalie harp",
     "jon ossoff posted a long thread last night and somewhere in it he "
     "mentions natalie harp"),
])
def test_lexical_rung_does_not_merge_the_template_families(a, b):
    assert gtp.slugs_match_lexically(a, b) is False
    assert gtp.slugs_match_lexically(b, a) is False


def test_lexical_match_returns_the_best_candidate_not_the_first():
    """REWRITTEN (was ``…returns_the_first_matching_candidate``). First-in-dict
    -order is exactly what chained unrelated posts together; the rung now
    scores every candidate and takes the best."""
    cands = [(1, "nba 2k mycareer mode"), (3, "hayden panettiere dies at 36"),
             (2, "hayden panettiere")]
    assert gtp.lexical_match("hayden panettiere death hoax", cands) == 2
    assert gtp.lexical_match("something else entirely", cands) is None
    assert gtp.lexical_match("", cands) is None


def test_containment_beats_a_weak_token_overlap():
    # Both candidates score above zero; the contained NAME must win.
    cands = [(1, "al najma vs al ittihad football match"),
             (2, "al ittihad vs al nejmeh football")]
    assert gtp.lexical_score("al ittihad vs al nejmeh football", cands[1][1]) == 1.0
    assert gtp.lexical_match("al ittihad vs al nejmeh football", cands) == 2


def test_cluster_lexical_merge_attaches_to_a_recent_trend_by_name(monkeypatch):
    """REGRESSION: after two live runs 0 of 259 trends had 2+ sources — Wikipedia
    'Hayden Panettiere' and Bluesky 'Hayden Panettiere dies at 36' were filed
    apart (cosine 0.158 > KNN threshold). The name rule merges them."""
    store = _FakeStore(trends=[{"id": 77, "slug": "hayden panettiere", "title": "Hayden Panettiere",
                                "sources": ["wikipedia"], "regions": ["global"],
                                "sighting_count": 1, "image_url": None, "local_thumb_path": None}])
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("tiktok", "bsky-hp", "Hayden Panettiere dies at 36")]

    res = _run(gtp.cluster_items(items, run_id=2, embed=None, seen_at=_NOW))

    assert res["new"] == 0 and res["touched"] == 1
    assert res["attached"] == [(77, items[0])]
    assert store.created == []
    assert store.trends[77]["sources"] == ["wikipedia", "tiktok"]


def test_cluster_lexical_merge_within_one_run_without_embeddings(monkeypatch):
    """REWRITTEN for minting rights: wikipedia and x_trends can no longer mint,
    so the Wikipedia row attaches to the Bluesky story by name and the lone
    x_trends token creates nothing at all."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [
        _item("wikipedia", "Natalie_Harp", "Natalie Harp"),
        _item("tiktok", "bsky-nh", "Trump aide Natalie Harp controversy"),
        _item("x_trends", "US:Najee Harris", "Najee Harris"),   # unrelated, stays apart
    ]
    res = _run(gtp.cluster_items(items, run_id=3, embed=None, seen_at=_NOW))
    assert res["new"] == 1 and res["touched"] == 1 and res["not_minted"] == 1
    assert [c["sources"] for c in store.created] == [["tiktok"]]
    ids = [tid for tid, _ in res["attached"]]
    assert len(ids) == 2 and ids[0] == ids[1]


# ---- P3 rung 3: the canonical key ----------------------------------------

def _label(name, **over):
    lab = {"identified": "known", "canonical_name": name, "is_trend": "movement",
           "freshness": "current", "sensitivity": "ok", "blocked_reason": "none",
           "what_it_is": "a thing"}
    lab.update(over)
    return lab


def test_canonical_key_merges_two_languages_in_one_run(monkeypatch):
    """The rung the embeddings could not be: an EXACT key, so the Arabic and
    the English name of the same match land on one card with no similarity
    maths at all."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("tiktok", "b1", "الدرعية ضد النصر"),
             _item("tiktok", "AE:al nassr", "Al-Nassr vs Al-Diriyah", region="AE")]
    labels = [_label("Al-Nassr vs Al-Diriyah"), _label("Al Nassr vs Al Diriyah")]

    res = _run(gtp.cluster_items(items, run_id=1, embed=None, seen_at=_NOW,
                                 labels=labels))

    assert res["new"] == 1 and res["touched"] == 1
    assert res["attached"][0][0] == res["attached"][1][0]
    assert store.created[0]["canonical_key"] == "al nassr vs al diriyah"


def test_canonical_key_matches_across_runs_with_no_model_call(monkeypatch):
    store = _FakeStore(trends=[{
        "id": 55, "slug": "al nassr vs al diriyah football",
        "canonical_key": "al nassr vs al diriyah", "title": "Al-Nassr vs Al-Diriyah",
        "sources": ["tiktok"], "regions": ["AE"], "sighting_count": 1,
        "image_url": None, "local_thumb_path": None}])
    monkeypatch.setattr(gtp, "store", store)

    res = _run(gtp.cluster_items([_item("kym", "k1", "النصر ضد الدرعية")],
                                 run_id=2, embed=None, seen_at=_NOW,
                                 labels=[_label("Al-Nassr vs Al-Diriyah")]))

    assert res["new"] == 0 and res["attached"][0][0] == 55
    assert store.canonical_lookups == ["al nassr vs al diriyah"]
    assert store.created == []


def test_a_row_matched_by_another_rung_is_stamped_with_the_label(monkeypatch):
    store = _FakeStore(trends=[{"id": 8, "slug": "nihilist penguin",
                                "title": "Nihilist Penguin", "sources": ["kym"],
                                "regions": ["global"], "sighting_count": 1,
                                "canonical_key": None, "image_url": None,
                                "local_thumb_path": None}])
    monkeypatch.setattr(gtp, "store", store)
    _run(gtp.cluster_items([_item("tiktok", "b1", "The Nihilist Penguin")],
                           run_id=2, embed=None, seen_at=_NOW,
                           labels=[_label("Nihilist Penguin meme")]))
    assert store.canonical_writes == [(8, "nihilist penguin meme")]


def test_an_unknown_label_never_becomes_a_key(monkeypatch):
    """Every unidentified item in a run would otherwise collapse into one card
    keyed "unknown" — a false merge, which is far worse than a false split."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("tiktok", "b1", "#شيء"), _item("kym", "k1", "#غامض")]
    # The second label is the dangerous shape: the model says it cannot
    # identify the thing and STILL offers a plausible-sounding name. Trusting
    # it would merge two unrelated hashtags into one card.
    labels = [_label("unknown", identified="unknown"),
              _label("Financial Transactions / Fintech", identified="unknown")]
    res = _run(gtp.cluster_items(items, run_id=1, embed=None, seen_at=_NOW,
                                 labels=labels))
    assert res["new"] == 2
    assert store.canonical_lookups == []
    assert all(c["canonical_key"] is None for c in store.created)


def test_clustering_runs_unchanged_when_the_labelling_pass_is_unavailable(monkeypatch):
    """MANDATORY DEGRADATION: no labels ⇒ rung 3 is skipped and the ladder
    still works. ``labels=None``, a short list and empty dicts are all the same
    thing."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("kym", "k1", "Nihilist Penguin"),
             _item("tiktok", "b1", "the nihilist penguin")]
    for labels in (None, [], [{}], [{}, {}]):
        store.trends.clear()
        store.created.clear()
        store.canonical_lookups.clear()
        res = _run(gtp.cluster_items(items, run_id=1, embed=None, seen_at=_NOW,
                                     labels=labels))
        assert res["new"] == 1 and res["touched"] == 1
        assert store.canonical_lookups == []


def test_cluster_survives_a_lexicon_load_failure(monkeypatch):
    store = _FakeStore()
    async def boom(since_days=14):
        raise RuntimeError("db hiccup")
    store.recent_trend_slugs = boom
    monkeypatch.setattr(gtp, "store", store)
    res = _run(gtp.cluster_items([_item("kym", "k1", "Some Meme")], run_id=4, embed=None, seen_at=_NOW))
    assert res["new"] == 1 and res["skipped"] == 0


def test_heartbeat_loop_stamps_and_survives_a_failing_store(monkeypatch):
    calls = {"n": 0}

    class _S:
        async def heartbeat_run(self, run_id):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db hiccup")

    monkeypatch.setattr(gtp, "store", _S())

    # WAIT FOR THE TICKS, DO NOT SLEEP A FIXED WALL-CLOCK WINDOW. The old
    # version slept 0.05s at interval=0.005 and asserted >= 3 ticks, which
    # measured out at EXACTLY 3 on this machine — dead on the boundary, so any
    # jitter (a slow first tick, GC, a loaded CI box) made it 2 and the test
    # failed ~1 run in 8. The property under test is "it kept ticking after the
    # stamp that raised", not "it ticked fast enough".
    async def scenario():
        task = asyncio.create_task(gtp._heartbeat_loop(9, interval=0.001))
        try:
            async with asyncio.timeout(5):
                while calls["n"] < 3:
                    await asyncio.sleep(0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    _run(scenario())
    assert calls["n"] >= 3          # kept ticking after the failing stamp


def test_run_refresh_stops_the_heartbeat_task_when_done(monkeypatch):
    """The heartbeat is a background task: it must be cancelled on both the
    happy path and the failure path (a leaked task keeps stamping a dead run)."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)

    async def fake_fetch_all(plan, **kw):
        raise RuntimeError("boom")   # unexpected → run fails
    monkeypatch.setattr(gtp.gts, "fetch_all", fake_fetch_all)

    async def scenario():
        before = {t for t in asyncio.all_tasks()}
        await gtp.run_refresh(5, metadata={"trigger": "manual"})
        await asyncio.sleep(0)
        leaked = [t for t in asyncio.all_tasks() - before if "heartbeat" in (t.get_name() or "")]
        return leaked

    leaked = _run(scenario())
    assert leaked == []
    assert any(f.get("status") == "failed" for _, f in store.run_updates)


def test_cluster_source_key_merge_wins_over_a_different_title(monkeypatch):
    # Google's US:pete hegseth was filed under trend 9 last time; today the same
    # source key comes back with a differently-cased/spaced title. Same card.
    store = _FakeStore(
        trends=[{"id": 9, "slug": "hegseth", "title": "Hegseth", "sources": ["tiktok"],
                 "regions": ["US"], "sighting_count": 3}],
        source_keys={("tiktok", "US:pete hegseth"): 9},
    )
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("tiktok", "US:pete hegseth", "Pete  Hegseth", region="US")]

    res = _run(gtp.cluster_items(items, run_id=2, embed=None, seen_at=_NOW))

    assert res["touched"] == 1 and res["new"] == 0
    assert res["attached"][0][0] == 9
    assert store.created == []


def test_cluster_creates_a_new_trend_when_nothing_matches(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    item = _item("kym", "nihilist-penguin", "Nihilist Penguin", summary="a sad penguin",
                 url="https://kym/x", image_url="https://kym/x.jpg", metric=None)

    res = _run(gtp.cluster_items([item], run_id=3, embed=None, seen_at=_NOW))

    assert res["new"] == 1 and res["touched"] == 0
    row = store.created[0]
    assert row["slug"] == "nihilist penguin" and row["title"] == "Nihilist Penguin"
    assert row["sources"] == ["kym"] and row["regions"] == ["global"]
    assert row["first_seen_at"] == _NOW and row["last_seen_at"] == _NOW
    assert row["_embedding"] is None                     # exact-only run: no vector
    # the created trend is ALSO sighted + touched, so sighting_count lands on 1
    tid = row["id"]
    assert store.trends[tid]["sighting_count"] == 1
    assert [s[0] for s in store.sightings] == [tid]
    assert res["image_urls"] == {tid: "https://kym/x.jpg"}


def test_embedding_hint_only_merges_when_the_names_agree(monkeypatch):
    """REWRITTEN (was ``…knn_merge_attaches_under_the_threshold``, which pinned
    ``distance <= KNN_DISTANCE_MAX`` as a merge DECISION).

    The vectors now only PROPOSE candidates; the lexical name rung decides. A
    near neighbour whose name does not agree must not merge, however close the
    cosine says it is — the closest pair in the entire live corpus was false."""
    close = {"id": 21, "slug": "penguin meme", "title": "penguin meme", "distance": 0.01}
    store = _FakeStore(
        trends=[{"id": 21, "slug": "penguin meme", "title": "penguin meme",
                 "sources": ["kym"], "regions": ["global"], "sighting_count": 1}],
        nearest=[close],
    )
    monkeypatch.setattr(gtp, "store", store)
    _fake_embed(monkeypatch, {"Nihilist Penguin": _vec(1, 0, 0)})

    res = _run(gtp.cluster_items([_item("tiktok", "b1", "Nihilist Penguin")],
                                 run_id=4, embed=gtp.embed_texts, seen_at=_NOW))

    assert res["embeddings"] == "ok"
    assert store.nearest_calls == [_vec(1, 0, 0)]      # the hint WAS asked for…
    assert res["new"] == 1 and res["touched"] == 0     # …and it did not decide


def test_embedding_hint_feeds_the_lexical_rung(monkeypatch):
    """The other half: a hint the 14-day slug lexicon never offered still
    merges, because the NAMES agree. This is what the vectors are for now."""
    hinted = {"id": 21, "slug": "nihilist penguin", "title": "Nihilist Penguin",
              "distance": 0.19}
    # No rows in the fake store at all: the lexicon is empty, so the ONLY way
    # this can merge is through the hint.
    store = _FakeStore(nearest=[hinted])
    monkeypatch.setattr(gtp, "store", store)
    _fake_embed(monkeypatch, {"Nihilist Penguin meme explained": _vec(1, 0, 0)})

    res = _run(gtp.cluster_items(
        [_item("tiktok", "b1", "Nihilist Penguin meme explained")],
        run_id=4, embed=gtp.embed_texts, seen_at=_NOW))

    assert res["new"] == 0 and res["touched"] == 1
    assert res["attached"][0][0] == 21


def test_cluster_knn_too_far_creates_a_trend_and_stores_the_vector(monkeypatch):
    store = _FakeStore(nearest=[{"id": 21, "slug": "x", "title": "x", "distance": 0.55}])
    monkeypatch.setattr(gtp, "store", store)
    _fake_embed(monkeypatch, {"Nihilist Penguin": _vec(1, 0, 0)})

    res = _run(gtp.cluster_items([_item("tiktok", "b1", "Nihilist Penguin")],
                                 run_id=4, embed=gtp.embed_texts, seen_at=_NOW))

    assert res["new"] == 1 and res["touched"] == 0
    assert store.created[0]["_embedding"] == _vec(1, 0, 0)


def test_cluster_in_run_merge_of_two_sources_same_story(monkeypatch):
    """REWRITTEN: the in-run merge used to be a cosine decision. It is now the
    name rung over candidates the in-run vectors proposed — and the filing
    order is minting-first, so KYM (which may mint) is filed before Reddit
    (which may not)."""
    store = _FakeStore(nearest=[])
    monkeypatch.setattr(gtp, "store", store)
    _fake_embed(monkeypatch, {
        "Nihilist Penguin": _vec(1.0, 0.0, 0.0),
        "the nihilist penguin meme explained": _vec(0.98, 0.05, 0.0),   # cos ≈ 0.9987
        "Pete Hegseth": _vec(0.0, 1.0, 0.0),
    })
    items = [
        _item("reddit", "t3_a", "Nihilist Penguin", region="global"),
        _item("kym", "nihilist-penguin", "the nihilist penguin meme explained"),
        _item("tiktok", "US:pete hegseth", "Pete Hegseth", region="US"),
    ]

    res = _run(gtp.cluster_items(items, run_id=5, embed=gtp.embed_texts, seen_at=_NOW))

    assert res["new"] == 2 and res["touched"] == 1 and res["not_minted"] == 0
    by_source = {it["source"]: tid for tid, it in res["attached"]}
    assert by_source["kym"] == by_source["reddit"] != by_source["tiktok"]
    merged = store.trends[by_source["kym"]]
    assert merged["sources"] == ["kym", "reddit"] and merged["sighting_count"] == 2


def test_cluster_in_run_slug_map_merges_even_without_embeddings(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("kym", "k1", "Nihilist Penguin"),
             _item("tiktok", "b1", "the nihilist  penguin")]
    res = _run(gtp.cluster_items(items, run_id=5, embed=None, seen_at=_NOW))
    assert res["new"] == 1 and res["touched"] == 1
    assert res["attached"][0][0] == res["attached"][1][0]


def test_cluster_degrades_to_exact_match_when_embed_raises(monkeypatch):
    store = _FakeStore(nearest=[{"id": 21, "distance": 0.0}])   # would match if asked
    monkeypatch.setattr(gtp, "store", store)
    _fake_embed(monkeypatch, boom=RuntimeError("403 billing disabled"))
    items = [_item("tiktok", "b1", "Nihilist Penguin"),
             _item("tiktok", "b2", "Nihilist Penguin"),
             _item("kym", "k1", "Something Else")]

    res = _run(gtp.cluster_items(items, run_id=6, embed=gtp.embed_texts, seen_at=_NOW))

    assert res["embeddings"] == "unavailable"
    assert res["new"] == 2 and res["touched"] == 1        # exact slug still merges
    assert store.nearest_calls == []                       # KNN never consulted
    assert all(c["_embedding"] is None for c in store.created)


def test_cluster_degrades_when_embed_returns_the_wrong_count(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)

    async def _short(texts):
        return [_vec(1, 0, 0)]                             # one vector for two texts
    res = _run(gtp.cluster_items([_item("tiktok", "a", "One"), _item("kym", "b", "Two")],
                                 run_id=6, embed=_short, seen_at=_NOW))
    assert res["embeddings"] == "unavailable" and res["new"] == 2


def test_cluster_skips_empty_titles_and_non_dicts(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("kym", "a", "!!!"), _item("kym", "b", ""), "junk",
             _item("kym", "c", "Real One")]
    res = _run(gtp.cluster_items(items, run_id=7, embed=None, seen_at=_NOW))
    assert res["skipped"] == 3 and res["new"] == 1


def test_cluster_files_a_repeated_source_item_once_per_run(monkeypatch):
    # The same post arriving twice with the same (source, source_key). The
    # sightings table would drop the second INSERT (UNIQUE run/source/key) but
    # touch_trend would still bump sighting_count — so the duplicate must be
    # dropped before filing. (Source changed reddit -> bluesky: reddit is
    # non-minting now and would create nothing to count.)
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [
        _item("tiktok", "b-abc", "Nihilist Penguin", region="global"),
        _item("tiktok", "b-abc", "Nihilist Penguin", region="global"),   # duplicate
        _item("tiktok", "b-xyz", "Nihilist Penguin", region="global"),   # DIFFERENT post
    ]
    res = _run(gtp.cluster_items(items, run_id=7, embed=None, seen_at=_NOW))

    assert res["new"] == 1 and res["touched"] == 1 and res["skipped"] == 1
    assert [it["source_key"] for _, it in res["attached"]] == ["b-abc", "b-xyz"]
    trend_id = res["trend_ids"][0]
    assert store.trends[trend_id]["sighting_count"] == 2
    assert len(store.sightings) == 2 and len(store.touches) == 2


# ---- P0: minting rights ---------------------------------------------------

def test_non_minting_source_never_creates_a_trend(monkeypatch):
    """The single most important rule in the rewrite: 358 of 363 rows in the
    first live run were singletons because ANY item minted a card, and 47% of
    them were one Reddit post."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    res = _run(gtp.cluster_items([_item("reddit", "t3_a", "Some lone reddit post")],
                                 run_id=1, embed=None, seen_at=_NOW))
    assert res["new"] == 0 and res["touched"] == 0
    assert res["not_minted"] == 1 and res["skipped"] == 0
    assert store.created == [] and store.sightings == []


@pytest.mark.parametrize("source", sorted(gts.NON_MINTING_SOURCES))
def test_every_non_minting_source_is_enforced(monkeypatch, source):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    res = _run(gtp.cluster_items([_item(source, "k1", "A Lonely Signal")],
                                 run_id=1, embed=None, seen_at=_NOW))
    assert res["not_minted"] == 1 and store.created == []


def test_non_minting_source_still_attaches_as_a_sighting(monkeypatch):
    """Corroboration is the whole point of keeping these sources."""
    store = _FakeStore(trends=[{"id": 7, "slug": "nihilist penguin",
                                "title": "Nihilist Penguin", "sources": ["kym"],
                                "regions": ["global"], "sighting_count": 1,
                                "image_url": None, "local_thumb_path": None}])
    monkeypatch.setattr(gtp, "store", store)
    res = _run(gtp.cluster_items([_item("reddit", "t3_a", "The Nihilist Penguin")],
                                 run_id=1, embed=None, seen_at=_NOW))
    assert res["touched"] == 1 and res["not_minted"] == 0
    assert res["attached"][0][0] == 7
    assert store.trends[7]["sources"] == ["kym", "reddit"]


def test_minting_items_are_filed_before_non_minting_ones(monkeypatch):
    """Filing is sequential, so ORDER decides whether a Reddit sighting finds
    the trend that a Bluesky item would have minted. The stable sort puts every
    minting item first while leaving the fetch order intact inside each group."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [
        _item("reddit", "t3_a", "Nihilist Penguin"),      # arrives FIRST, cannot mint
        _item("tiktok", "b1", "Nihilist Penguin"),       # arrives second, mints
        _item("wikipedia", "Nihilist_Penguin", "Nihilist Penguin"),
    ]
    res = _run(gtp.cluster_items(items, run_id=1, embed=None, seen_at=_NOW))

    assert res["new"] == 1 and res["touched"] == 2 and res["not_minted"] == 0
    # the bluesky item was filed first even though it was fetched second
    assert [it["source"] for _, it in res["attached"]] == ["tiktok", "reddit", "wikipedia"]
    assert len(res["trend_ids"]) == 1
    assert store.created[0]["sources"] == ["tiktok"]


def test_minting_sort_is_stable_within_each_group(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    items = [_item("reddit", "r1", "Alpha"), _item("kym", "k1", "Beta"),
             _item("reddit", "r2", "Gamma"), _item("tiktok", "b1", "Delta")]
    res = _run(gtp.cluster_items(items, run_id=1, embed=None, seen_at=_NOW))
    assert [it["source_key"] for _, it in res["attached"]] == ["k1", "b1"]
    assert res["not_minted"] == 2       # both reddit posts matched nothing


def test_cluster_of_nothing_is_a_no_op(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    res = _run(gtp.cluster_items([], run_id=7, embed=None, seen_at=_NOW))
    assert res["new"] == res["touched"] == 0 and res["attached"] == []


def test_cluster_one_bad_item_does_not_end_the_run(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    real_create = store.create_trend

    async def _flaky(fields):
        if fields["slug"] == "boom":
            raise RuntimeError("simulated write failure")
        return await real_create(fields)
    store.create_trend = _flaky
    res = _run(gtp.cluster_items([_item("tiktok", "a", "Boom"), _item("kym", "b", "Fine")],
                                 run_id=8, embed=None, seen_at=_NOW))
    assert res["skipped"] == 1 and res["new"] == 1


def test_cluster_slug_race_falls_back_to_the_existing_row(monkeypatch):
    # create_trend fails on UNIQUE(slug) because another writer got there first;
    # the item is filed under that row instead of being dropped.
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    real_create = store.create_trend

    async def _race(fields):
        store.trends[999] = {"id": 999, "slug": fields["slug"], "title": "x",
                             "sources": [], "regions": [], "sighting_count": 0}
        raise RuntimeError("duplicate key value violates unique constraint")
    store.create_trend = _race
    res = _run(gtp.cluster_items([_item("tiktok", "a", "Raced")], run_id=8,
                                 embed=None, seen_at=_NOW))
    assert res["touched"] == 1 and res["attached"][0][0] == 999
    store.create_trend = real_create


def test_embed_texts_batches_by_32(monkeypatch):
    calls: list = []
    _fake_embed(monkeypatch, calls=calls)
    vecs = _run(gtp.embed_texts([f"t{i}" for i in range(70)]))
    assert [len(c) for c in calls] == [32, 32, 6]
    assert len(vecs) == 70


def test_embed_texts_of_nothing_never_calls_the_model(monkeypatch):
    calls: list = []
    _fake_embed(monkeypatch, calls=calls)
    assert _run(gtp.embed_texts([])) == [] and calls == []


def test_run_matcher_cosine_and_distance():
    m = gtp._RunMatcher()
    assert m.nearest(_vec(1, 0)) is None
    m.remember(1, "a", _vec(1, 0, 0))
    m.remember(2, "b", _vec(0, 1, 0))
    tid, dist = m.nearest(_vec(0.9, 0.1, 0))
    assert tid == 1 and dist == pytest.approx(1 - 0.9 / (0.9 ** 2 + 0.1 ** 2) ** 0.5, abs=1e-6)
    assert m.nearest(_vec(0, 0, 0)) is None            # degenerate query
    assert m.nearest(_vec(1, 0)) is None               # dimension mismatch
    assert gtp.cosine_similarity(_vec(1, 0), _vec(1, 0)) == pytest.approx(1.0)
    assert gtp.cosine_similarity([], _vec(1, 0)) == 0.0


# =========================================================================== #
# 4. Thumbnails + velocity
# =========================================================================== #

def test_download_thumbs_only_for_trends_without_one(monkeypatch):
    store = _FakeStore(trends=[
        {"id": 1, "image_url": "https://a/1.jpg", "local_thumb_path": None},
        {"id": 2, "image_url": "https://a/2.jpg", "local_thumb_path": "have.jpg"},
        {"id": 3, "image_url": None, "local_thumb_path": None},
    ])
    monkeypatch.setattr(gtp, "store", store)
    asked: list = []

    async def _dl(url):
        asked.append(url)
        return f"thumbs/{url.rsplit('/', 1)[-1]}"
    monkeypatch.setattr(gtp, "_download_thumb", _dl)

    n = _run(gtp.download_thumbs({1: "https://item/1.jpg", 2: "x", 3: "https://item/3.jpg",
                                  4: "https://gone/4.jpg"}))
    assert n == 2
    # the trend row's own image wins; the item's is the fallback
    assert asked == ["https://a/1.jpg", "https://item/3.jpg"]
    assert store.thumbs == [(1, "thumbs/1.jpg"), (3, "thumbs/3.jpg")]


def test_download_thumb_rejects_non_http_urls():
    assert _run(gtp._download_thumb("")) is None
    assert _run(gtp._download_thumb("self")) is None          # reddit's non-URL thumbnails
    assert _run(gtp._download_thumb("data:image/png;base64,AAAA")) is None


def _mock_http(monkeypatch, handler):
    """Route every httpx.AsyncClient the pipeline builds through `handler`
    (a plain `request -> httpx.Response` function). No network."""
    import httpx

    real_client = httpx.AsyncClient

    class _Client(real_client):
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    monkeypatch.setattr(gtp.httpx, "AsyncClient", _Client)


def _thumb_sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(gtp, "asset_local_path",
                        lambda kind, h, ext: tmp_path / f"{h}.{ext}")
    monkeypatch.setattr(gtp, "relativise", lambda p: str(p))


def test_download_thumb_saves_a_small_image(monkeypatch, tmp_path):
    import httpx
    _thumb_sandbox(monkeypatch, tmp_path)
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, content=b"JFIF-jpegbytes", headers={"content-type": "image/jpeg"}))

    rel = _run(gtp._download_thumb("https://img.example/penguin.jpg"))

    assert rel and Path(rel).read_bytes() == b"JFIF-jpegbytes"
    # second call: cached on disk, no fetch needed
    _mock_http(monkeypatch, lambda req: (_ for _ in ()).throw(AssertionError("refetched")))
    assert _run(gtp._download_thumb("https://img.example/penguin.jpg")) == rel


def test_download_thumb_rejects_non_images_and_oversized_bodies(monkeypatch, tmp_path):
    import httpx
    _thumb_sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(gtp, "_THUMB_MAX_BYTES", 64)

    # wrong content-type
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, content=b"<html>", headers={"content-type": "text/html"}))
    assert _run(gtp._download_thumb("https://img.example/a")) is None

    # honest Content-Length over the cap: refused before the body is read
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, content=b"x" * 10,
        headers={"content-type": "image/png", "content-length": "999999"}))
    assert _run(gtp._download_thumb("https://img.example/b")) is None

    # no Content-Length, body keeps coming past the cap: aborted mid-stream
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, content=b"x" * 200, headers={"content-type": "image/png"}))
    assert _run(gtp._download_thumb("https://img.example/c")) is None

    # 4xx
    _mock_http(monkeypatch, lambda req: httpx.Response(404, headers={"content-type": "image/png"}))
    assert _run(gtp._download_thumb("https://img.example/d")) is None
    assert list(tmp_path.iterdir()) == []


def test_fetch_image_bytes_streams_and_stops_at_the_cap(monkeypatch):
    """The cap bounds memory: the stream is abandoned once the buffer passes
    it, so a 100 MB "image" never lands in RAM."""
    import httpx
    monkeypatch.setattr(gtp, "_THUMB_MAX_BYTES", 100)
    served: list[int] = []

    async def _body():
        for _ in range(1000):
            served.append(50)
            yield b"y" * 50

    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, stream=_AsyncStream(_body()), headers={"content-type": "image/gif"}))
    assert _run(gtp._fetch_image_bytes("https://img.example/huge.gif")) is None
    # a handful of chunks at most — not the whole 50 KB
    assert sum(served) <= 300


class _AsyncStream(__import__("httpx").AsyncByteStream):
    """Minimal httpx async byte stream over an async generator."""
    def __init__(self, gen):
        self._gen = gen

    def __aiter__(self):
        return self._gen

    async def aclose(self):
        await self._gen.aclose()


def test_refresh_velocities_covers_dismissed_and_stays_in_range(monkeypatch):
    store = _FakeStore(trends=[
        {"id": 1, "status": "new", "sources": ["a", "b", "c"], "source_count": 3,
         "first_seen_at": _NOW - timedelta(hours=1), "last_seen_at": _NOW},
        {"id": 2, "status": "dismissed", "sources": ["a"], "source_count": 1,
         "first_seen_at": _NOW - timedelta(days=10), "last_seen_at": _NOW - timedelta(days=5)},
    ])
    store.sightings = [(1, 1, {"source": "a"}, _NOW), (1, 1, {"source": "b"}, _NOW)]
    monkeypatch.setattr(gtp, "store", store)
    n = _run(gtp.refresh_velocities(_NOW))
    assert n == 2 and set(store.velocities) == {1, 2}
    assert store.velocities[1] > store.velocities[2]
    assert all(0.0 <= v <= 1.0 for v in store.velocities.values())


# =========================================================================== #
# 5. Scoring — prompt, schema, parse, batch, pass
# =========================================================================== #

def _trend(i, **over):
    t = {"id": i, "title": f"Trend {i}", "slug": f"trend {i}", "sources": ["reddit", "kym"],
         "regions": ["global"], "sighting_count": 3, "summary": f"summary {i}",
         "first_seen_at": _NOW - timedelta(days=1), "last_seen_at": _NOW,
         "velocity": 0.6, "fit_score": None, "uae_relevance": None, "scored_at": None,
         "evidence": [
             {"source": "reddit", "region": "global", "title": f"r/memes post {i}",
              "metric_value": 2000, "metric_label": "posts"},
             {"source": "google_trends", "region": "US", "title": f"google {i}",
              "metric_value": 20000, "metric_label": "searches"},
         ]}
    t.update(over)
    return t


def test_schema_requires_every_item_field():
    item = gtp._SCHEMA["properties"]["scores"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert {"index", "clean_title", "topic", "topic_summary", "theme", "trend_kind",
            "sensitivity", "fit_score", "fit_reason", "uae_relevance", "confidence",
            "suggested_angle", "suggested_format", "effort", "tags"} <= set(item["required"])
    assert set(item["properties"]["trend_kind"]["enum"]) == gtp.TREND_KINDS


def test_fit_reason_is_generated_before_fit_score():
    """Structured output is autoregressive: whatever is declared first is
    generated first. The old schema emitted the number from nothing and the
    sentence was a post-hoc rationalisation of it."""
    item = gtp._SCHEMA["properties"]["scores"]["items"]
    order = list(item["properties"])
    assert order.index("fit_reason") < order.index("fit_score")
    # ...and stated explicitly, in case the SDK's schema conversion rebuilds
    # the map and loses Python's insertion order.
    assert item["propertyOrdering"] == order


def test_the_fit_prompt_is_anchored_and_states_the_caps():
    """REGRESSION for the bimodal collapse (164 live rows at 0-9, 91 at
    70-100): the old _SYSTEM gave membership language and no scale."""
    for anchor in ("- 100 —", "- 80 —", "- 60 —", "- 40 —", "- 20 —", "- 0 —"):
        assert anchor in gtp._SYSTEM
    assert "at most 25" in gtp._SYSTEM and "at most 55" in gtp._SYSTEM
    assert "at most 60" in gtp._SYSTEM
    assert "write this BEFORE you pick the number" in gtp._SYSTEM


def test_the_fit_prompt_carries_the_plain_language_rule():
    """The owner was shown a post that opened "I just saw the announcement that
    the Norminette..." — 42's internal code linter, a word no one outside the
    school has heard. It did not start at the brief: the SCORER wrote it into
    `suggested_angle` and the brief inherited it, so the rule belongs here too.

    Identity, not resemblance — it is the same constant the brief prompts use
    (trend_ideation.PLAIN_LANGUAGE_RULE), because one rule that reads four
    different ways is four rules."""
    assert ti.PLAIN_LANGUAGE_RULE in gtp._SYSTEM
    assert "WRITE FOR SOMEONE WHO HAS NEVER HEARD OF 42." in gtp._SYSTEM
    assert "NEVER use an insider word as if everyone knows it." in gtp._SYSTEM
    assert "The joke has to land for someone who cannot code." in gtp._SYSTEM
    # The concrete anchor survives: the exact sentence that was complained
    # about, plus the ALLOWED form of the same word.
    assert "saw the announcement that the Norminette..." in gtp._SYSTEM
    assert "the robot that fails your code over one" in gtp._SYSTEM
    # It governs the writing section, so it sits directly above it.
    assert gtp._SYSTEM.index("WRITE FOR SOMEONE") < gtp._SYSTEM.index("WHAT TO WRITE")


def test_the_suggested_angle_guidance_is_audience_ready():
    """`suggested_angle` is not an internal note: a person films it AND the
    ideation prompt seeds the finished caption from it, so it is the last place
    an insider word can be tolerated.

    The old guidance demonstrated the failure — its one example of a GOOD angle
    was 'Three Piscine students in the cluster at 2am', two insider words used
    as if everyone knows them."""
    assert "Three Piscine students in the cluster at 2am" not in gtp._SYSTEM
    assert "Three students at 2am in a lab that never closes" in gtp._SYSTEM
    assert "its words become the words of the finished post" in gtp._SYSTEM
    assert "Describe the room, not its 42 nickname." in gtp._SYSTEM
    # ...and the phrase is still shown, as the counter-example it now is.
    assert "not 'Piscine students in the cluster'" in gtp._SYSTEM
    # The schema description is part of the prompt the model actually reads.
    props = gtp._SCHEMA["properties"]["scores"]["items"]["properties"]
    assert "has never heard of 42" in props["suggested_angle"]["description"]


def test_the_prompt_no_longer_hands_the_model_the_insider_word_for_the_room():
    """The "What 42 HAS" line is where the model picks up its vocabulary for
    the whole answer, and it used to offer "a computer cluster" with nothing
    attached. The nickname is kept — labelled as a nickname, which is exactly
    what the rule asks for."""
    assert "a computer lab open around the clock (students call it the cluster" in gtp._SYSTEM
    assert "a campus, a computer cluster open around the clock" not in gtp._SYSTEM


def test_the_fit_anchors_do_not_teach_insider_vocabulary():
    """The 80 anchor holds up ONE example of a good angle, so its wording is
    the wording the model imitates in every suggested_angle. It used to end
    "at 3am in the cluster" — the last place in the prompt where an insider
    word was demonstrated as good prose, which is how "day 20 of the Piscine"
    ended up on real scored rows (trends 562 and 922, scored 2026-08-19)."""
    assert "at 3am in a lab that never closes" in gtp._SYSTEM
    assert "at 3am in the cluster" not in gtp._SYSTEM
    # The anchor itself is unchanged: only its example vocabulary moved.
    assert "- 80 — a general internet format with an obvious campus reskin" in gtp._SYSTEM


def test_the_rest_of_the_scoring_prompt_is_untouched():
    """The rule is ADDITIVE. The participation test, the trend-kind taxonomy,
    the views-per-post measurement and the corroboration reading were tuned by
    a separate change and are correct; this guards them against being reworded
    by accident while the rule is edited."""
    for line in (
        "A trend is a FORMAT PEOPLE ARE COPYING.",
        "The trend is the reusable joke, not the penguin.",
        "VIEWS PER POST — the strongest clue you will get",
        "A tag for a celebrity's death ran at 10,725 views per post",
        "`format` — a reusable structure people fill with their own content",
        "'1 source · 1 sighting' means one post, on one platform, once.",
        "what would OUR version of this look like?",
        "A huge, beautiful trend 42 cannot make anything for is a LOW fit",
    ):
        assert line in gtp._SYSTEM, line


def test_batch_prompt_shows_the_raw_text_and_what_the_number_is_worth():
    """REWRITTEN: the prompt used to print `name:` and `sightings: 3` and leave
    the model to decide what three sightings were worth. It now shows the raw
    text verbatim and says, in words, what the corroboration means."""
    prompt = gtp.build_batch_prompt([_trend(1), _trend(2)])
    assert "Assess the 2 trends" in prompt
    assert "TREND 1" in prompt and "TREND 2" in prompt
    assert 'raw text (verbatim): "Trend 1"' in prompt
    assert "corroboration: 2 independent source(s) (Reddit, Know Your Meme)" in prompt
    assert "corroborated, this is spreading" in prompt
    assert "what we saw:" in prompt
    assert "- Reddit · global · r/memes post 1 · 2,000 posts" in prompt
    assert "- Google Trends · US · google 2 · 20K searches" in prompt
    assert "seen: 2026-08-18 → 2026-08-19" in prompt
    assert "summary: summary 1" in prompt
    assert "sightings: 3" not in prompt       # the bare counter is gone


def test_corroboration_line_says_one_post_is_one_post():
    thin = _trend(1, sources=["reddit"], sighting_count=1, evidence=[],
                  summary="A long enough description of an actual reddit post body "
                          "that says what happened and why people are laughing.")
    assert "ONE post" in gtp.corroboration_line(thin)
    bare = _trend(2, sources=["x_trends"], sighting_count=1, evidence=[], summary="")
    line = gtp.corroboration_line(bare)
    assert "evidence: bare" in line and "nothing else" in line


def test_batch_prompt_caps_evidence_at_six_and_handles_missing_bits():
    ev = [{"source": "x_trends", "region": "AE", "title": f"t{i}"} for i in range(9)]
    prompt = gtp.build_batch_prompt([_trend(1, evidence=ev, sources=[], regions=[],
                                            summary="", first_seen_at=None,
                                            last_seen_at=None, sighting_count=0)])
    assert prompt.count("- X (via trends24) · AE · t") == 6
    assert "summary: (none)" in prompt
    assert "regions:" not in prompt and "seen:" not in prompt


def test_batch_prompt_brand_note_leads():
    prompt = gtp.build_batch_prompt([_trend(1)], "voice: warm")
    assert prompt.startswith("WHAT 42 SOUNDS AND LOOKS LIKE:\nvoice: warm")


def test_evidence_line_metric_formatting():
    assert gtp.evidence_line({"source": "wikipedia", "region": None, "title": "Hayden",
                              "metric_value": 10_392_637, "metric_label": "views"}) == \
        "Wikipedia · global · Hayden · 10.4M views"
    assert gtp.evidence_line({"source": "kym", "title": "", "metric_value": None}) == \
        "Know Your Meme · global · (untitled)"
    assert gtp.evidence_line({"source": "??", "title": "x", "metric_value": "n/a"}) == \
        "?? · global · x"


def _entry(i, **over):
    e = {"index": i, "clean_title": f"Clean {i}", "topic": f"angle {i}",
         "topic_summary": "what it is", "theme": "Meme Format", "trend_kind": "meme",
         "sensitivity": "ok", "fit_score": 70, "fit_reason": "students can shoot it",
         "uae_relevance": 40, "confidence": 65, "suggested_angle": "do it in the cluster",
         "suggested_format": "reel", "effort": "low", "tags": ["a", "b"]}
    e.update(over)
    return e


def test_parse_maps_by_the_echoed_index_not_by_position():
    trends = [_trend(11), _trend(22), _trend(33)]
    out = gtp.parse_batch_response({"scores": [_entry(3), _entry(1)]}, trends)
    assert set(out) == {33, 11}
    assert out[33]["clean_title"] == "Clean 3" and out[11]["clean_title"] == "Clean 1"
    assert out[11]["theme"] == "meme format"          # lowercased for the rollup
    assert 0 <= out[11]["opportunity_score"] <= 100


def test_parse_clamps_and_drops_invented_enums():
    trends = [_trend(1)]
    out = gtp.parse_batch_response({"scores": [_entry(
        1, fit_score=150, uae_relevance=-5, trend_kind="vibe", sensitivity="spicy",
        suggested_format="tiktok", effort="huge", clean_title="x" * 200,
        tags=[f"t{i}" for i in range(20)], topic_summary="s" * 1000,
    )]}, trends)
    s = out[1]
    assert s["fit_score"] == 100 and s["uae_relevance"] == 0
    assert s["trend_kind"] is None and s["sensitivity"] is None
    assert s["suggested_format"] is None and s["effort"] is None
    assert len(s["clean_title"]) == 60
    assert len(s["tags"]) == 8 and len(s["topic_summary"]) == 600


def test_parse_skips_bad_indexes_and_non_dicts():
    trends = [_trend(1)]
    raw = {"scores": [_entry(0), _entry(2), _entry("x"), "junk", _entry(1)]}
    assert set(gtp.parse_batch_response(raw, trends)) == {1}
    assert gtp.parse_batch_response("nope", trends) == {}
    assert gtp.parse_batch_response({"scores": None}, trends) == {}


def test_parse_blank_strings_become_none():
    out = gtp.parse_batch_response({"scores": [_entry(1, clean_title="  ", topic="",
                                                       fit_reason=None)]}, [_trend(1)])
    assert out[1]["clean_title"] is None and out[1]["topic"] is None
    assert out[1]["fit_reason"] is None


# ---- P2: the caps the prompt asks for, guaranteed in Python ---------------

def _bare(i=1, **over):
    """One Wikipedia row: a name and a counter, nothing else."""
    t = _trend(i, sources=["wikipedia"], sighting_count=1,
               summary="#4 most-viewed on English Wikipedia · 210,000 views",
               evidence=[{"source": "wikipedia", "region": "global", "title": "ChatGPT",
                          "metric_value": 210000, "metric_label": "views"}])
    t.update(over)
    t["_profile"] = gtp.evidence_profile(t)
    return t


def test_evidence_profile_grades_what_we_actually_know():
    assert gtp.evidence_profile(_trend(1))["quality"] == "corroborated"
    assert _bare()["_profile"]["quality"] == "bare"
    thin = _trend(2, sources=["reddit"], sighting_count=1, evidence=[],
                  summary="A long enough description of an actual reddit post body "
                          "that says what happened and why people are laughing.")
    assert gtp.evidence_profile(thin)["quality"] == "thin"


def test_evidence_profile_counts_evidence_not_the_poll_counter():
    """sighting_count is a POLL counter: one Wikipedia page re-read nine times
    is nine sightings and ONE piece of evidence."""
    polled = _trend(3, sources=["wikipedia"], sighting_count=9, evidence_count=1,
                    evidence=[], summary="")
    prof = gtp.evidence_profile(polled)
    assert prof["sighting_count"] == 1 and prof["quality"] == "bare"


def test_fit_caps_bite_and_explain_themselves():
    t = _bare(1, _triage={"identified": "known", "is_trend": "evergreen",
                          "freshness": "old", "what_it_is": "the ChatGPT page"})
    out = gtp.parse_batch_response({"scores": [_entry(1, fit_score=95)]}, [t])
    assert out[1]["fit_score"] == 25
    assert "[capped at 25:" in out[1]["fit_reason"]


def test_fading_caps_at_sixty():
    t = _trend(1, _triage={"identified": "known", "is_trend": "movement",
                           "freshness": "fading"})
    t["_profile"] = gtp.evidence_profile(t)
    out = gtp.parse_batch_response({"scores": [_entry(1, fit_score=90)]}, [t])
    assert out[1]["fit_score"] == 60


def test_single_post_caps_at_55_but_a_meme_template_is_forgiven():
    """A meme template posted once IS the trend — that is the one case where
    'one post' is not a reason to distrust the score."""
    t = _trend(1, sources=["reddit"], sighting_count=1, evidence=[],
               summary="A long enough description of an actual reddit post body "
                       "that says what happened and why people are laughing.",
               _triage={"identified": "inferred", "is_trend": "single_post",
                        "freshness": "current"})
    t["_profile"] = gtp.evidence_profile(t)
    capped = gtp.parse_batch_response({"scores": [_entry(1, fit_score=95,
                                                         trend_kind="news")]}, [t])
    assert capped[1]["fit_score"] == 55
    for kind in gtp.FORMAT_KINDS:
        free = gtp.parse_batch_response({"scores": [_entry(1, fit_score=95,
                                                            trend_kind=kind)]}, [t])
        assert free[1]["fit_score"] == 95, kind


def test_an_uncapped_score_is_left_exactly_as_the_model_wrote_it():
    t = _trend(1, _triage={"identified": "known", "is_trend": "movement",
                           "freshness": "breaking"})
    t["_profile"] = gtp.evidence_profile(t)
    s = gtp.parse_batch_response({"scores": [_entry(1, fit_score=73)]}, [t])[1]
    assert s["fit_score"] == 73 and "[capped" not in (s["fit_reason"] or "")
    assert s["clean_title"] == "Clean 1" and s["confidence"] == 65
    assert s["evidence_quality"] == "corroborated" and s["freshness"] == "breaking"


def test_a_title_we_cannot_vouch_for_never_overwrites_the_raw_evidence():
    """THE direct fix for "Financial Transactions / Fintech": save_score does
    `title = COALESCE(clean_title, title)`, so an invented English name
    REPLACES the raw hashtag on the row and the guess becomes the record."""
    unknown = _trend(1, _triage={"identified": "unknown", "is_trend": "movement",
                                 "freshness": "current"})
    unknown["_profile"] = gtp.evidence_profile(unknown)
    out = gtp.parse_batch_response(
        {"scores": [_entry(1, clean_title="Financial Transactions / Fintech")]}, [unknown])
    assert out[1]["clean_title"] is None
    # ...and the same for bare evidence, however confident the model sounded
    assert gtp.parse_batch_response({"scores": [_entry(1)]}, [_bare()])[1]["clean_title"] is None


def test_drop_counters_are_ported_and_extended():
    for key in ("batches_ok", "batches_unparseable", "batches_failed",
                "entries_bad_index", "items_not_returned", "fit_missing",
                "uae_missing", "fit_capped", "prefilter_skipped", "triage_skipped",
                "triage_batches_failed", "triage_batches_ok", "scored"):
        assert key in gtp.drop_stats(), key
    before = gtp.drop_stats()["fit_capped"]
    gtp.parse_batch_response({"scores": [_entry(1, fit_score=99)]}, [_bare()])
    assert gtp.drop_stats()["fit_capped"] == before + 1


# ---- P2 stage 1: the labelling / triage pass ------------------------------

def test_triage_prompt_forbids_guessing_and_licenses_old():
    """The two load-bearing instructions. The OLD _SYSTEM actively asserted
    everything was from "the last few days", which is why the model never once
    flagged an evergreen."""
    assert ("A hashtag you can TRANSLATE is still `unknown`" in gtp._TRIAGE_SYSTEM)
    assert "Never guess a plausible-sounding meaning" in gtp._TRIAGE_SYSTEM
    assert "Say `old` even though we just saw it — especially then."in gtp._TRIAGE_SYSTEM
    assert "the last few days" not in gtp._SYSTEM


def test_triage_schema_asks_for_the_name_after_the_judgement():
    item = gtp._TRIAGE_SCHEMA["properties"]["items"]["items"]
    assert set(item["required"]) == set(item["properties"])
    order = list(item["properties"])
    assert order.index("identified") < order.index("canonical_name")
    assert item["propertyOrdering"] == order


def _tri_entry(i, **over):
    e = {"index": i, "identified": "known", "canonical_name": f"Thing {i}",
         "is_trend": "movement", "freshness": "current", "sensitivity": "ok",
         "blocked_reason": "none", "what_it_is": "a thing people do"}
    e.update(over)
    return e


def test_triage_parses_by_position_because_raw_items_have_no_id():
    items = [_item("kym", "a", "One"), _item("kym", "b", "Two")]
    out = gtp.parse_triage_response({"items": [_tri_entry(2), _tri_entry(1)]}, items)
    assert set(out) == {0, 1}
    assert out[0]["canonical_name"] == "Thing 1" and out[1]["canonical_name"] == "Thing 2"
    assert gtp.parse_triage_response({"items": [_tri_entry(0), _tri_entry(9), "junk"]},
                                     items) == {}
    assert gtp.parse_triage_response("nope", items) == {}


def test_triage_clamps_invented_enums_to_the_cautious_answer():
    out = gtp.parse_triage_response({"items": [_tri_entry(
        1, identified="obviously", is_trend="vibes", freshness="soon",
        sensitivity="spicy")]}, [_item("kym", "a", "One")])
    assert out[0]["identified"] == "unknown" and out[0]["is_trend"] == "not_a_trend"
    assert out[0]["freshness"] == "current" and out[0]["sensitivity"] == "caution"


@pytest.mark.parametrize("over, reason", [
    ({"sensitivity": "avoid", "blocked_reason": "war_or_conflict"}, "sensitive"),
    ({"identified": "unknown"}, "unidentified"),
    ({"is_trend": "not_a_trend"}, "not_a_trend"),
    ({"freshness": "old"}, "old"),
])
def test_triage_verdicts(over, reason):
    tri = dict(_tri_entry(1), **over)
    assert gtp.triage_verdict(tri) == ("skip", reason)


def test_an_empty_triage_always_scores():
    """A clerical failure in the cheap pass must never delete a trend."""
    assert gtp.triage_verdict({}) == ("score", None)


def test_label_items_batches_by_twenty(monkeypatch):
    sizes: list = []

    async def _text(prompt, **kw):
        sizes.append(prompt.count("ITEM "))
        n = prompt.count("ITEM ")
        return {"items": [_tri_entry(i) for i in range(1, n + 1)]}
    monkeypatch.setattr(gtp, "gemini_text", _text)
    items = [_item("kym", f"k{i}", f"Thing {i}") for i in range(25)]
    labels, status = _run(gtp.label_items(items))
    assert sizes == [20, 5] and status == "ok"
    assert len(labels) == 25 and labels[0]["canonical_name"] == "Thing 1"


def test_label_items_unavailable_never_raises_and_says_so(monkeypatch):
    """MANDATORY DEGRADATION: stage 1 down ⇒ empty labels, status
    'unavailable', and the caller carries on."""
    _fake_text(monkeypatch, error=RuntimeError("PERMISSION_DENIED: 403"))
    labels, status = _run(gtp.label_items([_item("kym", "a", "One")]))
    assert status == "unavailable" and labels == [{}]


def test_a_failed_triage_batch_does_not_delete_its_items(monkeypatch):
    calls = {"n": 0}

    async def _text(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("504 gateway timeout")       # transient, not fatal
        n = prompt.count("ITEM ")
        return {"items": [_tri_entry(i) for i in range(1, n + 1)]}
    monkeypatch.setattr(gtp, "gemini_text", _text)
    items = [_item("kym", f"k{i}", f"Thing {i}") for i in range(25)]
    labels, status = _run(gtp.label_items(items))
    assert status == "partial"
    assert labels[:20] == [{}] * 20            # batch 1 lost, items intact
    assert labels[20]["canonical_name"] == "Thing 1"


def test_triage_raw_sentinel_is_a_lost_batch_not_a_lost_run(monkeypatch):
    _fake_text(monkeypatch, {"_raw": "```json {truncated"})
    labels, status = _run(gtp.label_items([_item("kym", "a", "One")]))
    assert labels == [{}] and status == "partial"


def _fake_text(monkeypatch, result=None, error=None, *, calls: list | None = None):
    async def _call(prompt, **kwargs):
        if calls is not None:
            calls.append((prompt, kwargs))
        if error is not None:
            raise error
        return result
    monkeypatch.setattr(gtp, "gemini_text", _call)


def test_score_batch_calls_the_model_with_the_house_settings(monkeypatch):
    calls: list = []
    _fake_text(monkeypatch, {"scores": [_entry(1)]}, calls=calls)
    out = _run(gtp.score_batch([_trend(1)], "brand"))
    assert set(out) == {1}
    prompt, kw = calls[0]
    assert kw["system"] is gtp._SYSTEM and kw["json_schema"] is gtp._SCHEMA
    assert kw["max_tokens"] >= 6000 and kw["temperature"] == 0.3
    assert "model" in kw
    assert prompt.startswith("WHAT 42 SOUNDS AND LOOKS LIKE:\nbrand")


def test_score_batch_raw_sentinel_skips_the_batch(monkeypatch):
    _fake_text(monkeypatch, {"_raw": "```json {truncated"})
    assert _run(gtp.score_batch([_trend(1)])) == {}


def test_score_batch_billing_error_raises_scoring_unavailable(monkeypatch):
    _fake_text(monkeypatch, error=RuntimeError("billing is disabled for project x"))
    with pytest.raises(gtp.ScoringUnavailable) as ei:
        _run(gtp.score_batch([_trend(1)]))
    assert "billing" in str(ei.value).lower()


def test_score_batch_transient_error_returns_empty(monkeypatch):
    _fake_text(monkeypatch, error=RuntimeError("504 gateway timeout"))
    assert _run(gtp.score_batch([_trend(1)])) == {}


def test_score_batch_of_nothing_never_calls_the_model(monkeypatch):
    async def _explode(*a, **k):
        raise AssertionError("must not be called")
    monkeypatch.setattr(gtp, "gemini_text", _explode)
    assert _run(gtp.score_batch([])) == {}


def _brand(monkeypatch, note=""):
    async def _note():
        return note
    monkeypatch.setattr(gtp, "_brand_note", _note)


def test_score_pending_attaches_evidence_saves_and_reports_progress(monkeypatch):
    store = _FakeStore(trends=[_trend(1, evidence=None), _trend(2, evidence=None),
                               _trend(3, evidence=None, scored_at=_NOW)])
    store.evidence = {1: [{"source": "reddit", "region": "global", "title": "seen on reddit"}]}
    monkeypatch.setattr(gtp, "store", store)
    _brand(monkeypatch, "voice: warm")
    prompts: list = []

    async def _text(prompt, **kw):
        prompts.append(prompt)
        return {"scores": [_entry(1), _entry(2)]}
    monkeypatch.setattr(gtp, "gemini_text", _text)
    progress: list = []

    async def _on(done, total):
        progress.append((done, total))

    counts = _run(gtp.score_pending(on_progress=_on))
    assert counts == {"scored": 2, "prefiltered": 0, "triaged_out": 0, "total": 2}
    assert set(store.scores) == {1, 2}
    assert progress[-1] == (2, 2)
    assert "seen on reddit" in prompts[0]                       # evidence made it in
    assert store.scores[1]["clean_title"] == "Clean 1"


def test_score_pending_batches_twenty_then_six(monkeypatch):
    """REWRITTEN (was ``…batches_of_eight``). A live batch of 8 truncated at
    max_tokens=6000 and the {"_raw": …} sentinel then dropped all eight
    SILENTLY, so the bigger schema is paid for with a smaller batch. The
    labelling pass — the twenty — runs over RAW ITEMS during the refresh, not
    here."""
    store = _FakeStore(trends=[_trend(i, evidence=None) for i in range(1, 20)])
    monkeypatch.setattr(gtp, "store", store)
    _brand(monkeypatch)
    sizes: list = []

    async def _text(prompt, **kw):
        sizes.append(prompt.count("TREND "))
        return {"scores": []}
    monkeypatch.setattr(gtp, "gemini_text", _text)
    assert _run(gtp.score_pending())["scored"] == 0
    assert sizes == [6, 6, 6, 1]
    assert gtp.TRIAGE_BATCH_SIZE == 20 and gtp.SCORE_BATCH_SIZE == 6


def test_score_pending_nothing_pending_never_calls_the_model(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)

    async def _explode(*a, **k):
        raise AssertionError("must not be called")
    monkeypatch.setattr(gtp, "gemini_text", _explode)
    monkeypatch.setattr(gtp, "_brand_note", _explode)
    assert _run(gtp.score_pending())["total"] == 0


def test_score_pending_writes_a_row_for_everything_it_skips(monkeypatch):
    """Nothing disappears: a skipped trend is stored at fit_score 0 with an
    honest reason, so "Min fit: any" still shows it and the next refresh can
    rescue it once a second source corroborates it."""
    bare_x = _trend(9, sources=["x_trends"], sighting_count=1, summary="", evidence=None)
    store = _FakeStore(trends=[_trend(1, evidence=None), _trend(2, evidence=None), bare_x])
    monkeypatch.setattr(gtp, "store", store)
    _brand(monkeypatch)
    _fake_text(monkeypatch, {"scores": [_entry(1)]})

    counts = _run(gtp.score_pending(labels={
        1: _label("Thing One"),
        2: _label("Thing Two", sensitivity="avoid", blocked_reason="war_or_conflict"),
    }))

    assert counts == {"scored": 1, "prefiltered": 1, "triaged_out": 1, "total": 3}
    assert set(store.scores) == {1, 2, 9}
    assert store.scores[2]["fit_score"] == 0 and store.scores[2]["sensitivity"] == "avoid"
    assert "war or conflict" in store.scores[2]["fit_reason"]
    assert store.scores[9]["fit_score"] == 0 and store.scores[9]["clean_title"] is None
    assert "second source" in store.scores[9]["fit_reason"]


def test_a_trend_with_no_label_is_still_scored_with_the_caps_applied(monkeypatch):
    """The failed-triage-batch path, at the pass level."""
    store = _FakeStore(trends=[_bare(1, evidence=None, scored_at=None)])
    monkeypatch.setattr(gtp, "store", store)
    _brand(monkeypatch)
    _fake_text(monkeypatch, {"scores": [_entry(1, fit_score=95)]})
    counts = _run(gtp.score_pending(labels=None))
    assert counts["scored"] == 1 and counts["triaged_out"] == 0
    assert store.scores[1]["fit_score"] == 25          # bare evidence, capped anyway


def test_score_pending_propagates_scoring_unavailable(monkeypatch):
    store = _FakeStore(trends=[_trend(1, evidence=None)])
    monkeypatch.setattr(gtp, "store", store)
    _brand(monkeypatch)
    _fake_text(monkeypatch, error=RuntimeError("PERMISSION_DENIED: 403"))
    with pytest.raises(gtp.ScoringUnavailable):
        _run(gtp.score_pending())


def test_rescore_clears_then_scores(monkeypatch):
    store = _FakeStore(trends=[_trend(1, evidence=None, scored_at=_NOW),
                               _trend(2, evidence=None, scored_at=_NOW)])
    monkeypatch.setattr(gtp, "store", store)
    _brand(monkeypatch)
    _fake_text(monkeypatch, {"scores": [_entry(1)]})
    n = _run(gtp.rescore([1]))
    assert store.cleared == [[1]] and n == 1 and set(store.scores) == {1}


# =========================================================================== #
# 6. run_refresh — the whole job with fake sources / store / model
# =========================================================================== #

def _fake_fetch_all(monkeypatch, items, stats, *, boom=None, calls: list | None = None):
    async def _fetch(plan, **kw):
        if calls is not None:
            calls.append((plan, kw))
        if boom is not None:
            raise boom
        return list(items), dict(stats)
    monkeypatch.setattr(gts, "fetch_all", _fetch)


_STATS = {
    "google_trends": {"ok": True, "count": 2, "error": None, "ms": 300},
    "reddit": {"ok": True, "count": 1, "error": None, "ms": 500},
    "x_trends": {"ok": False, "count": 0, "error": "x_trends:US: HTTP 503 from trends24.in",
                 "ms": 900},
}
_ITEMS = [
    _item("tiktok", "US:pete hegseth", "Pete Hegseth", region="US",
          summary="Hegseth news", metric=2000, label="searches"),
    _item("tiktok", "US:nihilist penguin", "Nihilist Penguin", region="US",
          image_url="https://g/p.jpg"),
    _item("reddit", "t3_a", "The Nihilist Penguin", summary="sad bird"),
]


def _wire_run(monkeypatch, *, store=None, text_result=None, text_error=None,
              embed_boom=None):
    store = store or _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    _fake_fetch_all(monkeypatch, _ITEMS, _STATS)
    _fake_embed(monkeypatch, {"Pete Hegseth": _vec(0, 1, 0), "Nihilist Penguin": _vec(1, 0, 0),
                              "The Nihilist Penguin": _vec(1, 0, 0)}, boom=embed_boom)
    _brand(monkeypatch)
    _fake_text(monkeypatch, result=text_result, error=text_error)

    async def _no_thumb(url):
        return None
    monkeypatch.setattr(gtp, "_download_thumb", _no_thumb)
    return store


def _final(store, run_id):
    return store.runs[run_id]


def test_run_refresh_terminal_done_and_records_source_failure(monkeypatch):
    store = _wire_run(monkeypatch, text_result={"scores": [_entry(1), _entry(2)]})
    _run(gtp.run_refresh(41, metadata={"trigger": "manual"}))

    run = _final(store, 41)
    assert run["status"] == "done" and run["phase"] == "done"
    assert run["metadata"]["trigger"] == "manual"                # preserved
    assert run["metadata"]["sources"] == _STATS                  # failure recorded, run went on
    assert run["metadata"]["sources"]["x_trends"]["ok"] is False
    assert run["metadata"]["embeddings"] == "ok"
    assert run["items_found"] == 3 and run["sources_total"] >= 2
    assert run["sources_done"] == 3
    # 2 trends (penguin merged across google + reddit by exact slug), both new
    assert run["trends_new"] == 2 and run["trends_touched"] == 1
    assert run["trends_scored"] == 2
    assert run["progress"] == "2 trends from 2 sources · 2 new"
    assert run["error"] is None if "error" in run else True
    # status walked fetching -> clustering -> scoring -> done, in order
    statuses = [f["status"] for _, f in store.run_updates if "status" in f]
    assert statuses == ["fetching", "clustering", "scoring", "done"]
    # everything seen this run got a velocity
    assert set(store.velocities) == set(store.trends)


def test_run_refresh_scoring_unavailable_is_done_with_a_caveat(monkeypatch):
    store = _wire_run(monkeypatch, text_error=RuntimeError("billing is disabled"))
    _run(gtp.run_refresh(42, metadata={"trigger": "auto"}))
    run = _final(store, 42)
    assert run["status"] == "done"
    assert run["trends_scored"] == 0
    assert run["progress"].startswith("2 trends from 2 sources · 2 new. ")
    assert "billing" in run["progress"].lower()
    assert store.scores == {}


def test_run_refresh_embeddings_down_still_finishes(monkeypatch):
    store = _wire_run(monkeypatch, text_result={"scores": []},
                      embed_boom=RuntimeError("no credentials"))
    _run(gtp.run_refresh(43, metadata={}))
    run = _final(store, 43)
    assert run["status"] == "done"
    assert run["metadata"]["embeddings"] == "unavailable"
    assert run["trends_new"] == 2                                # exact slug still merged


def test_run_refresh_unexpected_exception_is_failed(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    _fake_fetch_all(monkeypatch, [], {}, boom=RuntimeError("DNS exploded"))
    _brand(monkeypatch)
    _run(gtp.run_refresh(44, metadata={"trigger": "manual"}))
    run = _final(store, 44)
    assert run["status"] == "failed" and run["phase"] == "failed"
    assert "DNS exploded" in run["error"]
    assert run["progress"] == "the refresh failed"
    assert run["metadata"]["trigger"] == "manual"


def test_run_refresh_reads_metadata_back_when_not_given(monkeypatch):
    store = _wire_run(monkeypatch, text_result={"scores": []})
    store.runs[45] = {"id": 45, "status": "fetching", "metadata": {"trigger": "auto"},
                      "active": True}
    _run(gtp.run_refresh(45))
    assert store.runs[45]["metadata"]["trigger"] == "auto"
    assert store.runs[45]["status"] == "done"


def test_run_refresh_all_sources_down_is_still_done_with_a_note(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    _fake_fetch_all(monkeypatch, [], {"google_trends": {"ok": False, "count": 0,
                                                        "error": "timeout", "ms": 20000}})
    _brand(monkeypatch)
    _fake_text(monkeypatch, {"scores": []})
    _run(gtp.run_refresh(46, metadata={}))
    run = _final(store, 46)
    assert run["status"] == "done"
    assert "no source could be reached" in run["progress"]


def test_run_refresh_passes_the_config_cap_the_caps_map_and_the_screen(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    calls: list = []
    _fake_fetch_all(monkeypatch, [], {}, calls=calls)
    _brand(monkeypatch)
    _fake_text(monkeypatch, {"scores": []})
    _run(gtp.run_refresh(47, metadata={}))
    plan, kw = calls[0]
    assert kw["max_items_per_unit"] == gtp.config.general_trends_max_items_per_source
    assert kw["caps"] == dict(gtp.config.general_trends_source_caps)
    assert callable(kw["screen"])
    assert all({"source", "unit", "region"} <= set(u) for u in plan)


def test_the_screen_is_safety_then_freshness(monkeypatch):
    screen = gtp.build_screen(now=_NOW)
    # safety drops first, and its reason is a bare slug the counters can key on
    verdict, reason = screen({"source": "reddit", "title": "OnlyFans model leak",
                              "published_at": _NOW.isoformat()})
    assert verdict == "drop" and reason in ("explicit", "harm", "bait", "avoid_topic")
    # freshness drops what safety kept
    old = (_NOW - timedelta(days=400)).isoformat()
    assert screen({"source": "reddit", "title": "A perfectly fine meme",
                   "published_at": old}) == ("drop", "beyond_max_age")
    # and a clean, current item survives
    assert screen({"source": "kym", "title": "Nihilist Penguin meme",
                   "published_at": _NOW.isoformat()}) == ("keep", "")


def test_run_refresh_records_why_the_feed_shrank(monkeypatch):
    """The owner has to be able to tell a fix from a bug: the drop counts go
    into run metadata AND into the progress line the run card shows."""
    stats = {
        "reddit": {"ok": True, "count": 3, "error": None, "ms": 10,
                   "dropped": 12, "dropped_by": {"bait": 9, "stale": 3}, "flagged": 1},
        "kym": {"ok": True, "count": 5, "error": None, "ms": 10,
                "dropped": 2, "dropped_by": {"bait": 2}, "flagged": 0},
    }
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    _fake_fetch_all(monkeypatch, _ITEMS, stats)
    _fake_embed(monkeypatch)
    _brand(monkeypatch)
    _fake_text(monkeypatch, {"scores": []})

    async def _no_thumb(url):
        return None
    monkeypatch.setattr(gtp, "_download_thumb", _no_thumb)
    _run(gtp.run_refresh(48, metadata={}))

    run = _final(store, 48)
    assert run["status"] == "done"
    assert run["metadata"]["dropped"] == 14
    assert run["metadata"]["dropped_by"] == {"bait": 11, "stale": 3}
    fetch_line = [f["progress"] for _, f in store.run_updates
                  if "item(s) from" in str(f.get("progress"))]
    assert "14 filtered at ingest" in fetch_line[0]


def test_run_refresh_labels_unavailable_still_finishes_done(monkeypatch):
    """MANDATORY DEGRADATION at the run level: clustering rung 3 is skipped,
    the run is 'done', and metadata says WHY the merges were weaker."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    _fake_fetch_all(monkeypatch, _ITEMS, _STATS)
    _fake_embed(monkeypatch)
    _brand(monkeypatch)
    _fake_text(monkeypatch, error=RuntimeError("PERMISSION_DENIED: 403"))

    async def _no_thumb(url):
        return None
    monkeypatch.setattr(gtp, "_download_thumb", _no_thumb)
    _run(gtp.run_refresh(49, metadata={}))

    run = _final(store, 49)
    assert run["status"] == "done"
    assert run["metadata"]["labels"] == "unavailable"
    assert run["trends_new"] == 2               # the ladder still ran
    assert store.canonical_lookups == []


def test_run_refresh_folds_the_labels_into_the_scoring_pass(monkeypatch):
    """The one call, two consumers: the same stage-1 answer that named the item
    for clustering triages it for scoring — with no second model call."""
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    _fake_fetch_all(monkeypatch, _ITEMS, _STATS)
    _fake_embed(monkeypatch)
    _brand(monkeypatch)
    seen: list = []

    async def _text(prompt, **kw):
        seen.append(kw.get("system"))
        if kw.get("system") is gtp._TRIAGE_SYSTEM:
            n = prompt.count("ITEM ")
            return {"items": [_tri_entry(i, canonical_name="Same Thing",
                                          identified="unknown") for i in range(1, n + 1)]}
        raise AssertionError("stage 2 must not run: every trend was triaged out")
    monkeypatch.setattr(gtp, "gemini_text", _text)

    async def _no_thumb(url):
        return None
    monkeypatch.setattr(gtp, "_download_thumb", _no_thumb)
    _run(gtp.run_refresh(50, metadata={}))

    run = _final(store, 50)
    assert run["status"] == "done"
    assert run["metadata"]["labels"] == "ok"
    assert seen == [gtp._TRIAGE_SYSTEM]                 # exactly one model pass
    assert "not usable" in run["progress"]
    # 'unknown' never becomes a clustering key, so nothing was merged by it
    assert store.canonical_lookups == []
    assert all(s["fit_score"] == 0 for s in store.scores.values())


def test_run_refresh_scoring_unavailable_after_a_good_triage_is_still_done(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    _fake_fetch_all(monkeypatch, _ITEMS, _STATS)
    _fake_embed(monkeypatch)
    _brand(monkeypatch)

    async def _text(prompt, **kw):
        if kw.get("system") is gtp._TRIAGE_SYSTEM:
            n = prompt.count("ITEM ")
            return {"items": [_tri_entry(i) for i in range(1, n + 1)]}
        raise RuntimeError("billing is disabled for project x")
    monkeypatch.setattr(gtp, "gemini_text", _text)

    async def _no_thumb(url):
        return None
    monkeypatch.setattr(gtp, "_download_thumb", _no_thumb)
    _run(gtp.run_refresh(51, metadata={}))

    run = _final(store, 51)
    assert run["status"] == "done" and run["trends_scored"] == 0
    assert "billing" in run["progress"].lower()


def test_start_refresh_queues_or_raises_busy(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(gtp, "store", store)
    # Collection is paused by default (`GENERAL_TRENDS_ENABLED`), and the pause
    # is checked before the queue — so this test, which is about the QUEUE, has
    # to describe the running world. The paused one is asserted in
    # tests/test_general_trends_paused.py.
    # `config` is a frozen dataclass, so it is REPLACED on the module rather
    # than mutated in place — the same move test_general_trends_config_api.py
    # uses. Mutating it would raise FrozenInstanceError, and mutating a shared
    # singleton would leak into every test that ran afterwards.
    monkeypatch.setattr(gtp, "config",
                        dataclasses.replace(gtp.config, general_trends_enabled=True))
    run = _run(gtp.start_refresh(trigger="manual"))
    assert run["status"] == "queued" and run["metadata"] == {"trigger": "manual"}
    with pytest.raises(gtp.RefreshBusy) as ei:
        _run(gtp.start_refresh())
    assert "already running" in str(ei.value)
    assert isinstance(ei.value, RuntimeError)
    _run(store.update_run(run["id"], status="done"))
    again = _run(gtp.start_refresh(trigger="auto"))
    assert again["id"] != run["id"] and again["metadata"]["trigger"] == "auto"


# =========================================================================== #
# 7. Poller — auto_refresh_due
# =========================================================================== #

def _done_run(hours_ago: float, *, status="done", finished=True):
    stamp = _NOW - timedelta(hours=hours_ago)
    return {"id": 1, "status": status, "active": False,
            "started_at": stamp - timedelta(minutes=3),
            "finished_at": stamp if finished else None}


def test_auto_refresh_due_disabled_when_hours_is_zero_or_negative():
    assert poller.auto_refresh_due(None, now=_NOW, every_hours=0) is False
    assert poller.auto_refresh_due(None, now=_NOW, every_hours=-1) is False
    assert poller.auto_refresh_due(None, now=_NOW, every_hours="junk") is False


def test_auto_refresh_due_when_no_run_ever():
    assert poller.auto_refresh_due(None, now=_NOW, every_hours=6) is True


def test_auto_refresh_not_due_while_a_run_is_active():
    active = {"id": 1, "status": "scoring", "active": True, "started_at": _NOW,
              "finished_at": None}
    assert poller.auto_refresh_due(active, now=_NOW, every_hours=6) is False
    queued = {"id": 1, "status": "queued", "active": True, "started_at": _NOW}
    assert poller.auto_refresh_due(queued, now=_NOW, every_hours=6) is False


def test_auto_refresh_due_by_age_of_the_last_finish():
    assert poller.auto_refresh_due(_done_run(5.9), now=_NOW, every_hours=6) is False
    assert poller.auto_refresh_due(_done_run(6.0), now=_NOW, every_hours=6) is True
    assert poller.auto_refresh_due(_done_run(30), now=_NOW, every_hours=6) is True
    # a failed run counts too — retry on the same cadence, not immediately
    assert poller.auto_refresh_due(_done_run(1, status="failed"), now=_NOW, every_hours=6) is False
    assert poller.auto_refresh_due(_done_run(7, status="failed"), now=_NOW, every_hours=6) is True


def test_auto_refresh_due_falls_back_to_started_at_and_iso_strings():
    run = _done_run(10, finished=False)                       # finished_at None
    assert poller.auto_refresh_due(run, now=_NOW, every_hours=6) is True
    run = {"status": "done", "active": False,
           "finished_at": (_NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")}
    assert poller.auto_refresh_due(run, now=_NOW, every_hours=6) is False
    assert poller.auto_refresh_due(run, now=_NOW.isoformat(), every_hours=1) is True
    assert poller.auto_refresh_due({"status": "done", "active": False}, now=_NOW,
                                   every_hours=6) is True   # no stamps at all


def test_poller_tick_queues_an_auto_run_and_runs_it(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(poller, "store", store)
    monkeypatch.setattr(poller, "config", SimpleNamespace(general_trends_refresh_hours=6))
    ran: list = []

    async def _fake_run(run_id, *, metadata=None):
        ran.append((run_id, metadata))
        await store.update_run(run_id, status="done")
    monkeypatch.setattr(poller.pipeline, "run_refresh", _fake_run)

    async def _fail_stale():
        return 0
    async def _pending(limit=1):
        return [dict(r) for r in store.runs.values() if r["status"] == "queued"][:limit]
    async def _claim(run_id):
        row = store.runs[run_id]
        if row["status"] != "queued":
            return False
        row["status"] = "fetching"
        return True
    store.fail_stale_runs = _fail_stale
    store.pending_runs = _pending
    store.claim_run = _claim

    _run(poller._poll_once())
    assert len(ran) == 1 and ran[0][1] == {"trigger": "auto"}
    assert store.runs[ran[0][0]]["status"] == "done"
    # a second tick straight away does nothing: the last run just finished
    _run(poller._poll_once())
    assert len(ran) == 1


# =========================================================================== #
# 8. Ideation — prompt + envelope
# =========================================================================== #

_IDEA_TREND = {
    "id": 512, "slug": "nihilist penguin", "title": "Nihilist Penguin meme",
    "summary": "A penguin walking away from the colony toward the mountains, alone.",
    "url": "https://knowyourmeme.com/memes/nihilist-penguin",
    "sources": ["kym", "reddit", "bluesky"], "regions": ["global", "US"],
    "sighting_count": 7, "velocity": 0.83,
    "first_seen_at": _NOW - timedelta(days=2), "last_seen_at": _NOW,
    "scored_at": _NOW, "topic": "Walking away from the herd", "topic_summary": "A penguin "
    "leaves the colony and heads inland; used for burnout / opting out jokes.",
    "theme": "meme format", "trend_kind": "meme", "sensitivity": "ok",
    "fit_score": 82, "fit_reason": "students can recreate it in the cluster",
    "uae_relevance": 35, "suggested_angle": "A student walks out of the cluster at 3am, "
    "toward the vending machine.", "suggested_format": "reel", "effort": "low",
    "tags": ["meme", "burnout"],
}
_IDEA_SIGHTINGS = [
    {"source": "kym", "region": "global", "title": "Nihilist Penguin",
     "metric_value": None, "metric_label": None},
    {"source": "reddit", "region": "global", "title": "the penguin that gave up",
     "metric_value": 4200, "metric_label": "posts"},
]


def test_ideation_prompt_has_the_trend_block_evidence_and_scorer_output():
    p = gti.build_prompt(trend=_IDEA_TREND, sightings=_IDEA_SIGHTINGS, tool="poster",
                         brand_note="voice: warm")
    assert p.startswith("WHAT 42 SOUNDS AND LOOKS LIKE:\nvoice: warm")
    assert "THE TREND:" in p
    assert "name: Nihilist Penguin meme" in p
    assert "seen on: kym, reddit, bluesky" in p
    assert "heat: 83/100" in p
    assert "link: https://knowyourmeme.com/memes/nihilist-penguin" in p
    assert "WHERE WE SAW IT:" in p
    assert "- Reddit · global · the penguin that gave up · 4,200 posts" in p
    assert "WHAT THE SCORER ALREADY WORKED OUT:" in p
    assert "what it is: A penguin leaves the colony" in p
    assert "how 42 could ride it: A student walks out" in p
    assert "sensitivity: ok" in p and "kind: meme" in p
    assert ti._TOOL_BRIEFS["poster"] in p
    assert p.rstrip().endswith("Fill only the fields this tool asks for.")


def test_ideation_prompt_unscored_trend_says_so_and_skips_the_scorer_block():
    t = {**_IDEA_TREND, "scored_at": None}
    p = gti.build_prompt(trend=t, sightings=[], tool="general_video")
    assert "has not been scored yet" in p
    assert "WHAT THE SCORER" not in p and "WHERE WE SAW IT" not in p
    assert "WHAT 42 SOUNDS" not in p
    assert ti._TOOL_BRIEFS["general_video"] in p


def test_ideation_prompt_caps_evidence_at_six():
    ev = [{"source": "reddit", "region": "global", "title": f"e{i}"} for i in range(10)]
    p = gti.build_prompt(trend=_IDEA_TREND, sightings=ev, tool="poster")
    assert p.count("- Reddit · global · e") == 6


@pytest.mark.parametrize("tool", ti.TOOLS)
def test_ideation_prompt_builds_for_every_tool(tool):
    assert ti._TOOL_BRIEFS[tool] in gti.build_prompt(trend=_IDEA_TREND, sightings=None, tool=tool)


def test_prefill_post_shim_carries_the_trend_fields():
    shim = gti.prefill_post_for(_IDEA_TREND)
    assert shim == {"id": 512, "topic": _IDEA_TREND["topic"], "caption": _IDEA_TREND["summary"],
                    "suggested_angle": _IDEA_TREND["suggested_angle"],
                    "topic_summary": _IDEA_TREND["topic_summary"],
                    "permalink": _IDEA_TREND["url"]}


def _fake_ideation(monkeypatch, *, reply=None, trend=_IDEA_TREND, boom=None,
                   prompts: list | None = None):
    store = _FakeStore(trends=[trend] if trend else [])
    store.evidence = {512: list(_IDEA_SIGHTINGS)}
    monkeypatch.setattr(gti, "store", store)

    async def _brand_note():
        return ""
    monkeypatch.setattr(gti.trend_scoring, "_brand_note", _brand_note)

    async def _gemini_text(prompt, **kwargs):
        if prompts is not None:
            prompts.append((prompt, kwargs))
        if boom is not None:
            raise boom
        return reply
    monkeypatch.setattr(gti, "gemini_text", _gemini_text)
    return store


def test_generate_prefill_returns_the_envelope_with_trend_id_and_url(monkeypatch):
    prompts: list = []
    _fake_ideation(monkeypatch, prompts=prompts, reply={
        "title": "The 3am walk-out",
        "topic": "A student walks out of the cluster toward the vending machines.",
        "dialogue": ["I've been staring at this segfault for six hours."],
        "notes": "rides the format",
    })
    out = _run(gti.generate_prefill(trend_id=512, tool="talking_head_v2"))
    assert out["tool"] == "talking_head_v2"
    assert out["post_id"] == 512                                       # the TREND id
    assert out["permalink"] == "https://knowyourmeme.com/memes/nihilist-penguin"
    assert out["title"] == "The 3am walk-out"
    assert out["dialogue"] == ["I've been staring at this segfault for six hours."]
    assert out["intro_topic"] == "" and out["character"] == ""
    prompt, kw = prompts[0]
    assert "WHERE WE SAW IT:" in prompt and "the penguin that gave up" in prompt
    assert kw["json_schema"] == ti.schema_for("talking_head_v2")
    assert kw["max_tokens"] >= 6000 and kw["system"] is gti._SYSTEM


def test_generate_prefill_falls_back_to_scorer_text_when_the_model_is_thin(monkeypatch):
    _fake_ideation(monkeypatch, reply={"title": "", "topic": ""})
    out = _run(gti.generate_prefill(trend_id=512, tool="poster"))
    assert out["title"] == _IDEA_TREND["topic"]
    assert out["topic"] == _IDEA_TREND["suggested_angle"]


def test_generate_prefill_unknown_tool_is_a_valueerror(monkeypatch):
    _fake_ideation(monkeypatch, reply={})
    with pytest.raises(ValueError):
        _run(gti.generate_prefill(trend_id=512, tool="tiktok"))


def test_generate_prefill_missing_trend_is_a_lookuperror(monkeypatch):
    _fake_ideation(monkeypatch, reply={}, trend=None)
    with pytest.raises(LookupError):
        _run(gti.generate_prefill(trend_id=999, tool="poster"))


def test_generate_prefill_billing_failure_is_ideation_unavailable(monkeypatch):
    _fake_ideation(monkeypatch, boom=RuntimeError("billing is disabled for project x"))
    with pytest.raises(gti.IdeationUnavailable) as ei:
        _run(gti.generate_prefill(trend_id=512, tool="poster"))
    # It IS a RuntimeError, which is exactly why the router must catch it FIRST.
    assert isinstance(ei.value, RuntimeError)
    assert gti.IdeationUnavailable is ti.IdeationUnavailable


def test_generate_prefill_transient_failure_is_a_plain_runtimeerror(monkeypatch):
    _fake_ideation(monkeypatch, boom=RuntimeError("504 gateway timeout"))
    with pytest.raises(RuntimeError) as ei:
        _run(gti.generate_prefill(trend_id=512, tool="poster"))
    assert not isinstance(ei.value, gti.IdeationUnavailable)


def test_generate_prefill_raw_sentinel_is_a_runtimeerror(monkeypatch):
    _fake_ideation(monkeypatch, reply={"_raw": "```json {"})
    with pytest.raises(RuntimeError) as ei:
        _run(gti.generate_prefill(trend_id=512, tool="poster"))
    assert not isinstance(ei.value, gti.IdeationUnavailable)


# =========================================================================== #
# 9. Router glue — the pure /sources merge
# =========================================================================== #

def test_source_items_merge_config_plan_with_latest_run_stats():
    plan = gts.build_fetch_plan(sources=["google_trends", "reddit", "youtube", "x_trends"],
                                geos=["US", "AE"], subreddits=["memes"], youtube_api_key="")
    latest = {"metadata": {"sources": {
        "google_trends": {"ok": True, "count": 40, "error": None, "ms": 1},
        "x_trends": {"ok": False, "count": 0, "error": "HTTP 503", "ms": 1},
    }}}
    items = {i["id"]: i for i in router_mod._source_items(plan, latest)}
    assert list(items) == list(gts.SOURCE_IDS)                    # every source listed
    g = items["google_trends"]
    # dropped/dropped_by are the ingest screen's counters: None for a run that
    # predates the screen, a number once fetch_all has one.
    assert g == {"id": "google_trends", "label": "Google Trends", "enabled": True,
                 "regions": ["US", "AE"], "ok": True, "count": 40, "error": None,
                 "dropped": None, "dropped_by": None}
    assert items["x_trends"]["ok"] is False and items["x_trends"]["error"] == "HTTP 503"
    assert items["reddit"]["regions"] == ["global"] and items["reddit"]["ok"] is None
    assert items["youtube"]["enabled"] is False                   # no key ⇒ not in plan
    assert items["bluesky"]["enabled"] is False and items["bluesky"]["count"] is None


def test_source_items_tolerates_no_run_and_junk_metadata():
    plan = gts.build_fetch_plan(sources=["kym"], geos=[], subreddits=[], youtube_api_key="")
    for latest in (None, {}, {"metadata": None}, {"metadata": {"sources": "junk"}},
                   {"metadata": {"sources": {"kym": "junk"}}}):
        items = router_mod._source_items(plan, latest)
        kym = next(i for i in items if i["id"] == "kym")
        assert kym["enabled"] is True and kym["ok"] is None


def test_router_error_classes_are_the_shared_ones():
    assert router_mod.pipeline.ScoringUnavailable is gtp.ScoringUnavailable
    assert router_mod.ideation.IdeationUnavailable is ti.IdeationUnavailable
    assert issubclass(router_mod.pipeline.RefreshBusy, RuntimeError)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


# =========================================================================== #
# What counts as a trend — the participation definition
# =========================================================================== #
# The owner's correction, in their words: a trend is "things people are doing —
# following, creating the same content again and again". The penguin that
# thousands restaged (a UAE shop posted it walking to THEIR store); the
# skyscraper proposal that became a format once people put their own message on
# the notice. An election or a search spike is a thing that HAPPENED — nobody
# can make their own version, so it is not a trend however big it is.

def test_the_scorer_is_told_what_a_trend_is():
    """The definition lives in the system prompt, with both of the owner's
    worked examples, because the model cannot infer it from the enum alone."""
    sys_prompt = gtp._SYSTEM.lower()
    assert "format people are copying" in sys_prompt
    # both examples survive
    assert "penguin" in sys_prompt
    assert "skyscraper" in sys_prompt
    # and the negative case is stated explicitly
    assert "things that" in sys_prompt and "happened" in sys_prompt
    assert "what would our version of this look like" in sys_prompt


def test_replication_is_asked_for_before_the_kind_is_chosen():
    """Structured output is autoregressive: describing WHO is copying the thing
    before naming the kind means the kind follows the description instead of
    being rationalised after it."""
    props = list(gtp._SCHEMA["properties"]["scores"]["items"]["properties"])
    assert "replication" in props
    assert props.index("replication") < props.index("trend_kind")
    ordering = gtp._SCHEMA["properties"]["scores"]["items"]["propertyOrdering"]
    assert ordering.index("replication") < ordering.index("trend_kind")
    # required, because Gemini silently drops optional fields
    assert "replication" in gtp._SCHEMA["properties"]["scores"]["items"]["required"]


def test_replication_is_parsed_and_kept():
    trends = [{"id": 7, "title": "Nihilist Penguin"}]
    raw = {"scores": [{
        "index": 1, "clean_title": "Nihilist Penguin meme",
        "replication": "thousands restaging it; ours = the walk out of the cluster at 6am",
        "trend_kind": "format", "sensitivity": "ok",
        "fit_reason": "students can shoot it on campus", "fit_score": 82,
        "uae_relevance": 60, "confidence": 80, "topic": "walking away",
        "topic_summary": "A penguin leaves the colony.", "theme": "meme format",
        "suggested_angle": "a student walks out of the cluster at 6am",
        "suggested_format": "reel", "effort": "low", "tags": ["campus"],
    }]}
    out = gtp.parse_batch_response(raw, trends)
    assert out[7]["replication"].startswith("thousands restaging it")
    assert out[7]["trend_kind"] == "format"


def test_a_missing_replication_does_not_break_parsing():
    trends = [{"id": 8, "title": "x"}]
    out = gtp.parse_batch_response(
        {"scores": [{"index": 1, "trend_kind": "meme", "fit_score": 10}]}, trends)
    assert out[8]["replication"] is None


def test_a_news_source_alone_mints_nothing(monkeypatch):
    """The redefinition, enforced. google_news, hacker_news, github, lemmy,
    bluesky and google_trends all USED to create cards — 300 of 328 items in a
    real fetch. None of them reports a thing people are RECREATING, so none of
    them may create a card any more; they can only corroborate one."""
    for source in ("google_news", "hacker_news", "github", "lemmy",
                   "bluesky", "google_trends", "wikipedia", "reddit"):
        store = _FakeStore()
        monkeypatch.setattr(gtp, "store", store)
        res = _run(gtp.cluster_items(
            [_item(source, "k1", "Something That Happened")],
            run_id=1, embed=None, seen_at=_NOW))
        assert store.created == [], f"{source} minted a trend"
        assert res["new"] == 0, source
        assert res["not_minted"] == 1, source


def test_a_format_registry_still_mints(monkeypatch):
    for source in ("tiktok", "kym", "instagram"):
        store = _FakeStore()
        monkeypatch.setattr(gtp, "store", store)
        res = _run(gtp.cluster_items(
            [_item(source, "k1", f"A Format From {source}")],
            run_id=1, embed=None, seen_at=_NOW))
        assert res["new"] == 1, source
        assert len(store.created) == 1, source


# =========================================================================== #
# trend_kind from the source, not from the model
# =========================================================================== #
# Know Your Meme publishes a curated `Type:` per entry, and its vocabulary IS
# the owner's definition in the source's own words. Asking a model to re-derive
# it is paying for a worse answer.

def _kym_trend(types, *, is_format=True, kind_from_model="news"):
    return {
        "id": 1, "title": "A Thing",
        "evidence": [{"source": "kym_entries",
                      "extra": {"types": types, "is_format": is_format}}],
    }, {"trend_kind": kind_from_model, "replication": "some people"}


def test_kym_type_overrides_the_models_kind():
    for types, expected in (
        (["dance", "participatory-media"], "challenge"),
        (["exploitable", "reaction"], "format"),      # most specific wins
        (["song"], "sound"),
        (["catchphrase"], "meme"),
        (["snowclone", "image-macro"], "format"),
    ):
        trend, score = _kym_trend(types)
        gtp.apply_source_kind(score, trend)
        assert score["trend_kind"] == expected, types
        # the override is visible, not mysterious
        assert "Know Your Meme" in score["replication"]


def test_kym_saying_it_is_not_a_format_is_also_a_classification():
    """`death`, `film`, `viral-video` — KYM classified it and none of the types
    is participatory. That is a real answer, and the gate should hold it back."""
    trend, score = _kym_trend(["death", "viral-video"], is_format=False,
                              kind_from_model="meme")
    gtp.apply_source_kind(score, trend)
    assert score["trend_kind"] == "culture"


def test_a_non_kym_source_leaves_the_models_kind_alone():
    trend = {"id": 2, "evidence": [{"source": "tiktok", "extra": {"posts": 10}}]}
    score = {"trend_kind": "format", "replication": "x"}
    gtp.apply_source_kind(score, trend)
    assert score["trend_kind"] == "format"
    assert score["replication"] == "x"


def test_source_kind_survives_junk_evidence():
    for evidence in (None, [], [None], [{"source": "kym_entries"}],
                     [{"source": "kym_entries", "extra": None}],
                     [{"source": "kym_entries", "extra": {"types": []}}],
                     [{"source": "kym_entries", "extra": {"types": ["unheard-of"]}}]):
        assert gtp.source_kind({"id": 3, "evidence": evidence}) is None
