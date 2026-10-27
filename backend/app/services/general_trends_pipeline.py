"""General Trends — the refresh pipeline: fetch → label → cluster → heat → score.

One refresh ("run") pulls every enabled public source (see
``general_trends_sources``) for every configured region, folds the raw items
into ONE trend row per story (``cluster_items``), recomputes each trend's heat
(``refresh_velocities``) and finally asks the model how well 42 Abu Dhabi could
ride each new trend (``score_pending``). ``run_refresh`` owns the run's terminal
status and never raises — a poller drives it, exactly like the competitor scan.

WHY THIS FILE WAS REWRITTEN (measured on the first live run: 363 trends)
-----------------------------------------------------------------------
358 of 363 rows had ``source_count = 1``: ANY single item minted a card, so
47% of the feed was one Reddit post and 22% was a bare trends24 hashtag the
model had invented an English meaning for. Three rules fix that, in this order
of impact:

  * **minting rights** — a source in ``gts.NON_MINTING_SOURCES`` may attach a
    sighting to an existing trend but may NEVER create one (``_file_item``).
    Minting-source items are filed FIRST (stable sort in ``cluster_items``), or
    a Reddit item arriving before the Bluesky item that would have minted the
    trend is lost for the run.
  * **one ingest screen** — ``general_trends_safety`` then
    ``general_trends_freshness``, handed to ``fetch_all(screen=…)`` so it runs
    inside the per-unit worker BEFORE the cap slice: the cap then yields good
    items, and the drop counts land on the /sources health row.
  * **the canonical name** — one cheap flash pass over the RAW ITEMS
    (``label_items``) both names each item (clustering rung 3) and triages it
    (scoring stage 1). Deliberately ONE call serving two consumers.

Clustering, in order of trust:

  1. the same source saw the same thing recently  (``find_trend_by_source_key``)
  2. exact normalised title, 30-day window        (``find_trend_by_slug``)
  3. exact ``canonical_key`` from the labelling pass — in-run map first, then
     ``find_trend_by_canonical_key``. This is the rung that crosses languages
     and survives across runs with no model call.
  4. anything filed earlier IN THIS RUN (slug map)
  5. the lexical name rung (containment / significant-token overlap), over the
     recent-slug lexicon plus any candidates the embeddings surfaced
  6. otherwise a brand-new trend — subject to minting rights.

**The cosine DECIDER is gone.** Over all 65,703 pairs of the 363 stored vectors
``count(distance <= 0.12) = 0``; the minimum distance in the whole corpus is
0.1230 and that closest pair is FALSE ("juju smith" ~ "najee harris"). The
true/false distributions are interleaved, not overlapping — no threshold
exists. The vectors are kept as a CANDIDATE-HINT generator: they propose rows
the 14-day slug lexicon would not have offered, and the lexical rung decides.

Embeddings come from ``gemini_embed`` in batches of 32. If that fails (Vertex
unconfigured, billing off, network) the run does NOT fail: clustering degrades
to exact matches only and ``run.metadata.embeddings = "unavailable"`` records
why cross-source merging was weaker that time. The same is true of the
labelling pass: ``run.metadata.labels = "unavailable"`` and the ladder simply
skips rung 3.

Scoring reuses the competitor scorer's classifier + clamps
(``trend_scoring._is_unavailable`` etc.) so a billing/403 outage raises
``ScoringUnavailable`` once and the run finishes ``done`` with a caveat instead
of a stack trace per batch.

Names imported BY NAME below (``gemini_embed``, ``gemini_text``, ``_brand_note``)
are the monkeypatch seams the tests use: ``monkeypatch.setattr(gtp, "gemini_embed", …)``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Any, Awaitable, Callable, Optional

import httpx
import numpy as np

from ..config import config
from . import general_trends_freshness as freshness
from . import general_trends_ranking as ranking
from . import general_trends_safety as safety
from . import general_trends_sources as gts
from . import general_trends_store as store
from .general_trends_store import SENSITIVITIES, TREND_KINDS
from .storage import asset_local_path, relativise
# ONE copy of the write-for-a-stranger rule, shared with both ideation prompts.
# It is imported rather than pasted so the scorer and the brief cannot drift
# apart: the scorer's `suggested_angle` is the SEED of the finished post, so a
# rule that only lands downstream lands too late (see the constant's comment in
# trend_ideation). trend_ideation imports trend_scoring/trend_store/vertex_client
# only — no cycle back to this module.
from .trend_ideation import PLAIN_LANGUAGE_RULE
from .trend_scoring import (  # noqa: F401 — re-exported seams
    ScoringUnavailable,
    _brand_note,
    _clamp_int,
    _explain_unavailable,
    _is_unavailable,
    _one_of,
)
from .vertex_client import gemini_embed, gemini_text

log = logging.getLogger("archivist.general_trends_pipeline")

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

# How far out the embeddings are allowed to PROPOSE a candidate. This is not a
# merge threshold and there is no merge threshold any more: over all 65,703
# pairs of the 363 vectors of the first live run, count(distance <= 0.12) = 0,
# the minimum distance in the whole corpus is 0.1230, and THAT CLOSEST PAIR IS
# FALSE ("juju smith" ~ "najee harris") — closer than every true pair. The
# true and false distributions are interleaved, not merely overlapping, so no
# cosine threshold exists at any usable recall. What the vectors CAN still do
# is offer rows the 14-day slug lexicon never showed us; the lexical rung then
# decides on the names. 0.20 is a generous "worth looking at", not a verdict.
KNN_HINT_MAX_DISTANCE = 0.20
KNN_HINT_LIMIT = 5
LEXICON_DAYS = 14               # recent-slug window for the lexical matcher
EMBED_BATCH = 32
# What is embedded per item: the title plus the first bit of evidence text.
EMBED_SUMMARY_CHARS = 300
# Sources whose "summary" is a stats line rather than evidence — Wikipedia's is
# "#N most-viewed on English Wikipedia · X views", identical across all forty
# articles, and it dragged every article into ONE trend in a real run. Their
# title alone is embedded.
EMBED_TITLE_ONLY_SOURCES = frozenset({"wikipedia"})
# Evidence lines shown to the scorer per trend. (The batch sizes and token
# budgets live with their stages, down in the Scoring section.)
EVIDENCE_PER_TREND = 6
# How many independent DB round-trips run side by side. Every store call is one
# trip to the (remote) pooler — ~0.7 s from a dev machine — and a refresh makes
# ~5 per item, so strictly sequential clustering of 200 items takes ~10 minutes.
# The pool is 10 wide and shared with the API, so stay well under it.
DB_CONCURRENCY = 4

_THUMB_ASSET_TYPE = "general_trend_thumbs"
_THUMB_TIMEOUT = 20.0
_THUMB_MAX_BYTES = 5 * 1024 * 1024

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


class RefreshBusy(RuntimeError):
    """A refresh is already queued or running — one at a time."""


class RefreshPaused(RuntimeError):
    """Collection is switched off at `GENERAL_TRENDS_ENABLED`.

    Deliberately NOT a subclass of `RefreshBusy`: "come back in a minute" and
    "this stream is paused pending research" are different answers, and a
    caller that retries the first would retry forever on the second."""


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

_LEADING_ARTICLES = ("the", "a", "an")
_SEPARATORS = re.compile(r"[-_/\\|]+")
_NOT_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")


def normalise_title(title: Any) -> str:
    """The exact-match key for a trend.

    lowercase → strip ``#``/``@`` → unicode NFKC → separators (``-_/``) become
    spaces → every other punctuation character is dropped → whitespace
    collapsed → leading article (the/a/an) dropped → stripped. Empty ⇒ the
    caller skips the item.

        "#TheNihilistPenguin"  -> "thenihilistpenguin"
        "The Nihilist Penguin" -> "nihilist penguin"
        "Spider-Man: Brand New Day" -> "spider man brand new day"
    """
    s = str(title or "").lower()
    s = s.replace("#", "").replace("@", "")
    s = unicodedata.normalize("NFKC", s)
    s = _SEPARATORS.sub(" ", s)
    s = _NOT_WORD.sub("", s)
    s = _WS.sub(" ", s).strip()
    if not s:
        return ""
    words = s.split(" ")
    if words and words[0] in _LEADING_ARTICLES:
        words = words[1:]
    return " ".join(words).strip()


def embed_text_for(item: dict) -> str:
    """What gets embedded for one raw item — title plus a slice of evidence
    (title only for sources whose summary is boilerplate, see
    EMBED_TITLE_ONLY_SOURCES)."""
    title = gts.collapse_ws(str(item.get("title") or ""))
    if str(item.get("source") or "") in EMBED_TITLE_ONLY_SOURCES:
        return title
    summary = gts.collapse_ws(str(item.get("summary") or ""))[:EMBED_SUMMARY_CHARS]
    return f"{title}. {summary}" if summary else title


def cosine_similarity(a: Any, b: Any) -> float:
    """Plain cosine similarity; 0.0 when either vector is empty/degenerate."""
    va = np.asarray(a, dtype=float).ravel()
    vb = np.asarray(b, dtype=float).ravel()
    if va.size == 0 or vb.size == 0 or va.size != vb.size:
        return 0.0
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


# --------------------------------------------------------------------------- #
# Lexical matching — the rung between exact slug and embedding KNN
# --------------------------------------------------------------------------- #
# Measured on the first live runs: cosine distance is NOT discriminative for
# short trend titles in the 0.12-0.20 band ("Hayden Panettiere" ~ "Hayden
# Panettiere dies at 36" = 0.158 sits BELOW false pairs like "JuJu
# Smith-Schuster" ~ "Najee Harris" = 0.123), so KNN stays tight and the
# obvious cross-source merges — one source names the thing, another names the
# story about it — are caught by name instead: token-bounded containment of a
# multi-word name, or a strong overlap of significant tokens.

LEXICAL_MIN_TOKENS = 2          # the contained name must be at least this many words
LEXICAL_MIN_CHARS = 8           # ... and this long ("nba 2k" is too generic)
LEXICAL_MIN_SHARED = 2          # token-overlap rule: shared significant tokens
LEXICAL_MIN_JACCARD = 0.5       # ... and their Jaccard over the union
# How far into the longer title the contained name may start, as a fraction of
# its length. MENTION IS NOT TOPIC: "natalie harp" containment fired on an
# incidental mention buried at the end of a 120-character Bluesky sentence
# about Jon Ossoff's post, and merged two unrelated cards. A title is ABOUT the
# thing it opens with; anything past the first ~40% is a reference to it.
LEXICAL_HEAD_FRACTION = 0.40

# The stopword list was INVERTED. It stripped the words that carry the story —
# controversy, reveal, sparks, discourse — and kept the template words, so
# "what going segura" and "what going github" shared {what, going} out of a
# 4-token union and hit Jaccard EXACTLY 0.5. There are 12+ "What's going on
# with X" posts in the live corpus and greedy first-match plus transitive
# chaining collapsed them into one mega-card. Content words are back in play;
# the templates, the feed nouns and the subreddit names are what get stripped.
_LEXICAL_STOPWORDS = frozenset({
    # grammar / glue
    "the", "and", "for", "with", "from", "that", "this", "into", "over", "about",
    "after", "before", "your", "their", "there", "just", "been", "have",
    # question-template words — the actual cause of the mega-card
    "what", "whats", "going", "deal", "why", "how", "does", "did", "anyone",
    "explain", "update", "please", "help", "someone", "everyone", "wondering",
    "happening", "happened", "thoughts", "opinion", "question",
    # words that describe the CHANNEL, never the story. Deliberately short:
    # "github", "tiktok" and "youtube" are NOT here, because a GitHub outage or
    # a TikTok ban is a real subject and stripping the subject is the mistake
    # this list was making in the first place.
    "trend", "trends", "trending", "news", "today", "spike", "page", "post",
    "posts", "thread", "wikipedia", "reddit", "subreddit",
    # the subreddits we read: they appear in titles and are not the subject
    "meme", "memes", "programmerhumor", "damnthatsinteresting", "technology",
    "outoftheloop", "popular", "tiktokcringe",
})


def significant_tokens(slug: str) -> frozenset[str]:
    """Tokens that carry a name: >= 4 chars, not stopwords, plural-stripped."""
    out: set[str] = set()
    for tok in (slug or "").split():
        if len(tok) < 4 or tok in _LEXICAL_STOPWORDS:
            continue
        if len(tok) > 4 and tok.endswith("s"):
            tok = tok[:-1]
        if tok in _LEXICAL_STOPWORDS:       # plural of a template word ("posts")
            continue
        out.add(tok)
    return frozenset(out)


def lexical_score(a: str, b: str) -> float:
    """0.0 (unrelated) .. 1.0 (identical) — how strongly two normalised titles
    name the same thing. Used for both the boolean rung and the non-greedy
    "best candidate" choice."""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if (len(short) >= LEXICAL_MIN_CHARS
            and len(short.split()) >= LEXICAL_MIN_TOKENS):
        pos = f" {long_} ".find(f" {short} ")
        # Contained AND near the front: the longer title is a story ABOUT the
        # shorter name rather than a sentence that happens to mention it.
        if 0 <= pos <= max(1.0, len(long_) * LEXICAL_HEAD_FRACTION):
            # Longer containment is stronger evidence; keep it above the
            # Jaccard band so containment wins a tie against a weak overlap.
            return min(1.0, 0.80 + 0.20 * (len(short) / len(long_)))
    ta, tb = significant_tokens(a), significant_tokens(b)
    shared = ta & tb
    if len(shared) < LEXICAL_MIN_SHARED:
        return 0.0
    union = ta | tb
    jaccard = len(shared) / len(union) if union else 0.0
    if jaccard < LEXICAL_MIN_JACCARD:
        return 0.0
    return float(jaccard) * 0.79        # strictly below the containment band


def slugs_match_lexically(a: str, b: str) -> bool:
    """True when two normalised titles name the same thing (see module note)."""
    return lexical_score(a, b) > 0.0


def lexical_match(slug: str, candidates: Iterable[tuple[int, str]]) -> Optional[int]:
    """The BEST matching candidate ``(trend_id, slug)``, or None.

    Non-greedy on purpose. The old version returned the first match in dict
    order, which — with a stopword list that made half the "What's going on
    with X" family look identical — chained unrelated posts together through
    whichever one happened to be filed first."""
    if not slug:
        return None
    best_id: Optional[int] = None
    best_score = 0.0
    for trend_id, cand in candidates:
        score = lexical_score(slug, cand)
        if score > best_score:
            best_score, best_id = score, int(trend_id)
    return best_id


class _RunMatcher:
    """In-run memory of the trends this run has already filed items under, so
    the same story arriving from a second source in the same run merges
    without a database round-trip (and even when embeddings are off, via the
    slug map)."""

    def __init__(self) -> None:
        self.slug_to_id: dict[str, int] = {}
        # canonical_key -> trend id: rung 3 inside one run. The DB copy
        # (find_trend_by_canonical_key) covers run N+1; this covers the second
        # item of the SAME run, with no round-trip.
        self.canon_to_id: dict[str, int] = {}
        self._ids: list[int] = []
        self._slugs: list[str] = []
        self._vecs: list[np.ndarray] = []
        self._matrix: Optional[np.ndarray] = None

    def remember(self, trend_id: int, slug: str, vector: Optional[list[float]],
                 canonical_key: str = "") -> None:
        if slug:
            self.slug_to_id.setdefault(slug, int(trend_id))
        if canonical_key:
            self.canon_to_id.setdefault(canonical_key, int(trend_id))
        if vector:
            v = np.asarray(vector, dtype=float).ravel()
            n = float(np.linalg.norm(v))
            if v.size and n > 0.0:
                self._ids.append(int(trend_id))
                self._slugs.append(str(slug or ""))
                self._vecs.append(v / n)
                self._matrix = None  # rebuilt lazily

    def lexical(self, slug: str) -> Optional[int]:
        """A trend filed earlier in this run whose slug names the same thing."""
        return lexical_match(slug, ((tid, s) for s, tid in self.slug_to_id.items()))

    def _sims(self, vector: Optional[list[float]]) -> Optional[np.ndarray]:
        if not vector or not self._vecs:
            return None
        q = np.asarray(vector, dtype=float).ravel()
        n = float(np.linalg.norm(q))
        if q.size == 0 or n == 0.0:
            return None
        if self._matrix is None or self._matrix.shape[0] != len(self._vecs):
            self._matrix = np.vstack(self._vecs)
        if self._matrix.shape[1] != q.size:
            return None
        return self._matrix @ (q / n)

    def nearest(self, vector: Optional[list[float]]) -> Optional[tuple[int, float]]:
        """(trend_id, cosine distance) of the closest remembered vector.

        A measurement, NOT a decision: nothing merges on this number any more
        (see KNN_HINT_MAX_DISTANCE). It backs :meth:`candidates`."""
        sims = self._sims(vector)
        if sims is None:
            return None
        best = int(np.argmax(sims))
        return self._ids[best], float(1.0 - sims[best])

    def candidates(self, vector: Optional[list[float]],
                   limit: int = KNN_HINT_LIMIT) -> list[tuple[int, str]]:
        """``[(trend_id, slug), …]`` for the nearest in-run vectors within
        KNN_HINT_MAX_DISTANCE — rows worth showing the lexical rung, never a
        merge in themselves."""
        sims = self._sims(vector)
        if sims is None:
            return []
        floor = 1.0 - float(KNN_HINT_MAX_DISTANCE)
        order = np.argsort(-sims)[:max(1, int(limit))]
        return [(self._ids[int(i)], self._slugs[int(i)])
                for i in order if float(sims[int(i)]) >= floor]


async def _gather_limited(coros: list, *, limit: int = DB_CONCURRENCY) -> list:
    """``asyncio.gather(return_exceptions=True)`` under a semaphore."""
    sem = asyncio.Semaphore(max(1, int(limit)))

    async def _one(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(_one(c) for c in coros), return_exceptions=True)


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #

async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed in batches of EMBED_BATCH via ``gemini_embed`` (looked up by name
    so tests can swap it). Raises on failure — ``cluster_items`` catches and
    degrades to exact matching."""
    out: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        chunk = texts[start:start + EMBED_BATCH]
        vecs = await gemini_embed(chunk)
        if len(vecs) != len(chunk):
            raise RuntimeError(
                f"embedding batch returned {len(vecs)} vectors for {len(chunk)} texts"
            )
        out.extend([list(v) for v in vecs])
    return out


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #

def item_content_time(item: dict) -> Optional[datetime]:
    """When the CONTENT behind an item happened, or None when its source has no
    real content age (a window feed like Wikipedia's daily top-1000). Delegates
    to the freshness module so one definition serves the screen, the trend row's
    ``latest_content_at`` and the ranking's freshness term."""
    try:
        return freshness.content_time(item)
    except Exception:  # noqa: BLE001 — a clock must never fail a run
        return None


def _new_trend_fields(item: dict, slug: str, seen_at: datetime,
                      vector: Optional[list[float]],
                      canonical_key: str = "") -> dict:
    return {
        "slug": slug,
        # Stamped at creation so the very first row already carries the label —
        # otherwise run N+1's canonical rung has nothing to match against until
        # something happens to touch the row.
        "canonical_key": canonical_key or None,
        "title": gts.collapse_ws(str(item.get("title") or ""))[:200],
        "summary": (item.get("summary") or "")[:600] or None,
        "url": item.get("url") or None,
        "image_url": item.get("image_url") or None,
        "category": item.get("category") or None,
        "regions": [item.get("region")] if item.get("region") else [],
        "sources": [item.get("source")] if item.get("source") else [],
        # touch_trend (called right after) bumps this to 1 and merges the rest.
        "sighting_count": 0,
        "peak_metric": item.get("metric_value"),
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
        # The ingest screen's verdict, carried onto the row. touch_trend would
        # stamp it a moment later anyway, but a minted row must never exist —
        # even briefly — claiming to be clean when the screen flagged it.
        "safety": item.get("safety") or "ok",
        "safety_reason": item.get("safety_reason") or None,
        "latest_content_at": item_content_time(item),
        "embedding": vector,
        "metadata": {
            "first_source": item.get("source"),
            "first_source_key": item.get("source_key"),
        },
    }


async def cluster_items(
    items: list[dict], *, run_id: Optional[int], embed: Optional[EmbedFn] = None,
    seen_at: Optional[datetime] = None,
    labels: Optional[list[dict]] = None,
) -> dict:
    """File every raw item under a trend (existing or new) and record it as a
    sighting.

    ``embed`` is an async ``texts -> vectors`` callable (``embed_texts`` in
    production, a fake in tests) or None for exact-match-only clustering.

    ``labels`` is the stage-1 answer for each item, PARALLEL TO ``items``
    (``label_items``): ``canonical_name`` drives clustering rung 3 and the rest
    of the dict is handed to the scoring pass. ``None``, a short list, or an
    empty dict per item all mean "no label" — the ladder simply skips rung 3.

    Returns::

        {
          "new":        int,   # trends created by this call
          "touched":    int,   # items filed under a trend that already existed
                               #  (before this item — including ones created
                               #  earlier in the same call)
          "skipped":    int,   # empty title/slug, a duplicate (source,
                               #  source_key) within this call, a screened-out
                               #  item, or a per-item failure
          "not_minted": int,   # items from a NON_MINTING source that matched
                               #  nothing: they may corroborate, never create
          "attached":   [(trend_id, item), ...],   # every filed item, in order
          "trend_ids":  [int, ...],                # distinct, in first-seen order
          "image_urls": {trend_id: image_url},     # first image seen per trend
                                                   #  (thumbnail candidates)
          "labels":     {trend_id: triage},        # stage 1, folded onto trends
          "embeddings": "ok" | "unavailable" | "off",
        }
    """
    seen_at = seen_at or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "new": 0, "touched": 0, "skipped": 0, "not_minted": 0,
        "attached": [], "trend_ids": [], "image_urls": {}, "labels": {},
        "embeddings": "off" if embed is None else "ok",
    }
    label_list = list(labels or [])

    # 1. slugs (drop what can't be keyed). The same source item can arrive
    #    twice in one run — a reddit post rising in two subs we read, a Google
    #    entry repeated in the feed — and it is ONE sighting: the sightings
    #    table would drop the second insert (UNIQUE run/source/key) but
    #    touch_trend would still bump sighting_count for it, so only the
    #    first occurrence of each (source, source_key) is filed.
    keyed: list[tuple[dict, str, dict]] = []
    seen_keys: set[tuple[str, str]] = set()
    for idx, it in enumerate(items or []):
        if not isinstance(it, dict):
            result["skipped"] += 1
            continue
        # The HARD half of the ingest screen, re-run here so a direct caller
        # (a replay script, a future code path) cannot bypass the brand filter
        # by skipping fetch_all. Idempotent: a drop is a drop either way, and
        # a "flag" is left alone because flags are kept on purpose.
        try:
            if safety.screen(it, mode=config.general_trends_safety,
                             allow_terms=frozenset(config.general_trends_allow_terms
                                                   or ()))[0] == "drop":
                result["skipped"] += 1
                continue
        except Exception:  # noqa: BLE001 — a filter must never end a run
            log.warning("[general-trends] safety re-check failed for an item", exc_info=True)
        slug = normalise_title(it.get("title"))
        if not slug:
            result["skipped"] += 1
            continue
        source = str(it.get("source") or "")
        key = str(it.get("source_key") or "")
        if source and key:
            if (source, key) in seen_keys:
                result["skipped"] += 1
                continue
            seen_keys.add((source, key))
        label = label_list[idx] if idx < len(label_list) else {}
        keyed.append((it, slug, label if isinstance(label, dict) else {}))
    if not keyed:
        return result

    # 1b. MINTING ORDER. Filing is sequential and only a minting source may
    #     create a trend, so a Reddit item that arrives before the Bluesky item
    #     which would have minted the same story finds nothing and is lost for
    #     the whole run. A STABLE sort (False < True) puts every minting item
    #     first and leaves the order inside each group exactly as fetched.
    keyed.sort(key=lambda k: str(k[0].get("source") or "") in gts.NON_MINTING_SOURCES)

    # 2. vectors, all up-front (one degradation decision per run)
    vectors: list[Optional[list[float]]] = [None] * len(keyed)
    if embed is not None:
        try:
            got = await embed([embed_text_for(it) for it, _, _ in keyed])
            if len(got) != len(keyed):
                raise RuntimeError("embedding count mismatch")
            vectors = [list(v) if v else None for v in got]
        except Exception as exc:  # noqa: BLE001 — degrade, never fail the run
            log.warning("[general-trends] embeddings unavailable (%s) — "
                        "clustering by exact title only", str(exc)[:200])
            result["embeddings"] = "unavailable"
            vectors = [None] * len(keyed)

    # 3. exact lookups against the DB as it was BEFORE this run — independent
    #    per item, so they run side by side (rungs 1 and 2 of the ladder)
    prehits = await _gather_limited([_prelookup(it, slug) for it, slug, _ in keyed])

    # 4. file each item, in order (in-run memory + canonical + lexical + create
    #    are sequential). The lexicon is every recent trend's slug, loaded once.
    matcher = _RunMatcher()
    seen_ids: set[int] = set()
    stamped: set[int] = set()
    lexicon: list[tuple[int, str]] = []
    try:
        lexicon = [(int(r["id"]), str(r.get("slug") or ""))
                   for r in await store.recent_trend_slugs(since_days=LEXICON_DAYS)]
    except Exception:  # noqa: BLE001 — without it we still have the exact rungs
        log.exception("[general-trends] could not load the recent-slug lexicon")

    for (item, slug, label), vector, prehit in zip(keyed, vectors, prehits):
        canonical_key = canonical_key_for(label)
        try:
            trend_id, created = await _file_item(
                item, slug, vector,
                prehit=(prehit if isinstance(prehit, int) else None),
                matcher=matcher, seen_at=seen_at, lexicon=lexicon,
                canonical_key=canonical_key,
            )
        except Exception:  # noqa: BLE001 — one bad item must not end the run
            log.exception("[general-trends] could not file %s:%s",
                          item.get("source"), item.get("source_key"))
            result["skipped"] += 1
            continue
        if trend_id is None:
            # Either a non-minting source with nothing to attach to (the
            # common, intended case) or a write that failed twice.
            if str(item.get("source") or "") in gts.NON_MINTING_SOURCES:
                result["not_minted"] += 1
            else:
                result["skipped"] += 1
            continue

        matcher.remember(trend_id, slug, vector, canonical_key)
        if created:
            result["new"] += 1
        else:
            result["touched"] += 1
            # Persist the label on a row that matched some OTHER rung, so the
            # next run can find it by exact key with no model call. Created
            # rows already carry it (see _new_trend_fields); once per trend
            # per run, never per item.
            if canonical_key and trend_id not in stamped:
                stamped.add(trend_id)
                try:
                    await store.set_canonical_key(trend_id, canonical_key)
                except Exception:  # noqa: BLE001 — a label is not worth a run
                    log.info("[general-trends] could not stamp canonical key for #%s",
                             trend_id)
        if label and trend_id not in result["labels"]:
            result["labels"][trend_id] = dict(label)
        result["attached"].append((trend_id, item))
        if trend_id not in seen_ids:
            seen_ids.add(trend_id)
            result["trend_ids"].append(trend_id)
        if item.get("image_url") and trend_id not in result["image_urls"]:
            result["image_urls"][trend_id] = item["image_url"]

    # 5. record every sighting + fold it into its trend — independent
    #    single-row writes, so they run side by side too
    await _gather_limited([
        _record_sighting(trend_id, run_id, item, seen_at)
        for trend_id, item in result["attached"]
    ])
    return result


async def _prelookup(item: dict, slug: str) -> Optional[int]:
    """Rungs 1-2: the trend id this item exactly matches in the DB, or None.
    Never raises (a failed lookup just means "treat as new")."""
    source = str(item.get("source") or "")
    key = str(item.get("source_key") or "")
    try:
        if source and key:
            hit = await store.find_trend_by_source_key(source, key)
            if hit:
                return int(hit["id"])
        hit = await store.find_trend_by_slug(slug)
        if hit:
            return int(hit["id"])
    except Exception:  # noqa: BLE001
        log.exception("[general-trends] lookup failed for %s:%s", source, key)
    return None


async def _record_sighting(trend_id: int, run_id: Optional[int], item: dict,
                           seen_at: datetime) -> None:
    try:
        await store.insert_sighting(trend_id, run_id, item, seen_at)
        await store.touch_trend(trend_id, item=item, seen_at=seen_at)
    except Exception:  # noqa: BLE001
        log.exception("[general-trends] could not record sighting for trend #%s", trend_id)


# Names the model returns when it has nothing: they must never become a key, or
# every unidentified item in the run collapses into one card.
_NON_NAMES = frozenset({"unknown", "none", "n/a", "na", "null", "unidentified",
                        "not applicable", "?", "-"})


def canonical_key_for(label: Optional[dict]) -> str:
    """The clustering key from one stage-1 answer — the model's canonical name
    put through the SAME normaliser as a slug, so "Al-Nassr vs Al-Diriyah" and
    "al nassr vs al diriyah" are one key. Empty when the pass was unavailable,
    the model declined to name the thing, or the name is a placeholder."""
    if not isinstance(label, dict):
        return ""
    # A name the model itself could not stand behind is not a key. The prompt
    # asks for the literal string "unknown" in that case, but a model that says
    # `identified: unknown` AND offers a plausible-sounding name is exactly the
    # failure that produced "Financial Transactions / Fintech" — and here it
    # would cost a false MERGE, which is worse than a false split.
    if label.get("identified") == "unknown":
        return ""
    name = str(label.get("canonical_name") or "").strip()
    if not name or name.lower() in _NON_NAMES:
        return ""
    return normalise_title(name)


async def _file_item(
    item: dict, slug: str, vector: Optional[list[float]], *,
    prehit: Optional[int], matcher: _RunMatcher, seen_at: datetime,
    lexicon: Optional[list[tuple[int, str]]] = None,
    canonical_key: str = "",
) -> tuple[Optional[int], bool]:
    """(trend_id, created) for one item, walking the trust ladder in order.
    ``prehit`` is the result of rungs 1-2 (``_prelookup``), computed up-front;
    ``lexicon`` is ``[(trend_id, slug), ...]`` for recent trends in the DB.
    ``(None, False)`` means "not filed": either a non-minting source with
    nothing to attach to, or a create that failed twice."""
    # 1-2. same source saw the same thing recently / exact normalised title
    if prehit is not None:
        return int(prehit), False

    # 3. the canonical name from the labelling pass. In-run first (free), then
    #    the DB, where it also matches rows minted by PREVIOUS runs — which is
    #    the whole point: an exact key crosses languages and costs no call.
    if canonical_key:
        hit = matcher.canon_to_id.get(canonical_key)
        if hit is not None:
            return int(hit), False
        try:
            row = await store.find_trend_by_canonical_key(canonical_key)
        except Exception:  # noqa: BLE001 — the ladder must survive a bad lookup
            log.info("[general-trends] canonical lookup failed for %r", canonical_key)
            row = None
        if row:
            return int(row["id"]), False

    # 4. filed earlier in this run, by exact slug
    if slug in matcher.slug_to_id:
        return matcher.slug_to_id[slug], False

    # 5. the lexical NAME rung. Candidates come from three places and are
    #    scored together, so the best match wins rather than the first: this
    #    run's own trends, the recent-slug lexicon, and any row the embeddings
    #    suggest. The vectors PROPOSE here; they never decide (see
    #    KNN_HINT_MAX_DISTANCE).
    candidates: list[tuple[int, str]] = [(tid, s) for s, tid in matcher.slug_to_id.items()]
    candidates.extend(matcher.candidates(vector))
    if lexicon:
        candidates.extend(lexicon)
    if vector:
        try:
            candidates.extend(
                (int(c["id"]), str(c.get("slug") or ""))
                for c in await store.nearest_trends(
                    vector, limit=KNN_HINT_LIMIT, max_distance=KNN_HINT_MAX_DISTANCE)
                if c.get("id") is not None
            )
        except Exception:  # noqa: BLE001 — hints are optional by construction
            log.info("[general-trends] nearest-trend hints unavailable")
    lex = lexical_match(slug, candidates)
    if lex is not None:
        return lex, False

    # 6. brand new — IF this source is allowed to mint one. A single Reddit
    #    post, a single Wikipedia row, a bare x_trends token and one watched
    #    Instagram post are all evidence of nothing on their own; they may
    #    corroborate a trend some other source created, and that is all.
    if str(item.get("source") or "") in gts.NON_MINTING_SOURCES:
        return None, False
    try:
        row = await store.create_trend(
            _new_trend_fields(item, slug, seen_at, vector, canonical_key))
    except Exception as exc:  # noqa: BLE001 — most likely a slug race
        again = await store.find_trend_by_slug(slug)
        if again:
            return int(again["id"]), False
        log.warning("[general-trends] create_trend failed for %r: %s", slug, str(exc)[:200])
        return None, False
    return int(row["id"]), True


# --------------------------------------------------------------------------- #
# Thumbnails
# --------------------------------------------------------------------------- #

async def _download_thumb(url: str) -> Optional[str]:
    """Fetch a trend image into local storage; relative path or None.

    Best-effort and bounded: only http(s), only ``image/*`` responses, at most
    5 MB. Source image URLs (Reddit previews, YouTube thumbs, Google's
    ``ht:picture``) go stale, and the card should keep rendering after that."""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return None
    try:
        url_hash = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:32]
        dest: Path = asset_local_path(_THUMB_ASSET_TYPE, url_hash, "jpg")
        if dest.is_file() and dest.stat().st_size > 0:
            return relativise(dest)
        data = await _fetch_image_bytes(str(url))
        if not data:
            return None
        dest.write_bytes(data)
        return relativise(dest)
    except Exception:  # noqa: BLE001 — a missing thumbnail must never fail a run
        log.info("[general-trends] thumbnail download failed for %s", str(url)[:120])
        return None


async def _fetch_image_bytes(url: str) -> Optional[bytes]:
    """GET ``url`` as a STREAM and stop the moment it turns out not to be a
    small image: wrong content-type, a Content-Length over the cap, or a body
    that keeps coming past the cap. Third-party feeds hand us arbitrary image
    URLs, so the cap has to bound memory and bandwidth — not just disk."""
    async with httpx.AsyncClient(
        timeout=_THUMB_TIMEOUT, follow_redirects=True,
        headers={"User-Agent": config.user_agent},
    ) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                return None
            length = r.headers.get("content-length")
            if length:
                try:
                    if int(length) > _THUMB_MAX_BYTES:
                        return None
                except ValueError:
                    pass
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > _THUMB_MAX_BYTES:
                    return None
    return bytes(buf) if buf else None


async def _thumb_one(trend_id: int, fallback: str) -> bool:
    try:
        trend = await store.get_trend(int(trend_id))
        if not trend or trend.get("local_thumb_path"):
            return False
        rel = await _download_thumb(trend.get("image_url") or fallback or "")
        if not rel:
            return False
        await store.set_local_thumb(int(trend_id), rel)
        return True
    except Exception:  # noqa: BLE001
        log.info("[general-trends] thumb step failed for trend #%s", trend_id)
        return False


async def download_thumbs(image_urls: dict[int, str]) -> int:
    """For every candidate trend that still has no local thumb, download its
    image (the trend row's own image_url first, the item's as fallback).
    Candidates are processed a few at a time."""
    results = await _gather_limited([
        _thumb_one(int(tid), url) for tid, url in (image_urls or {}).items()
    ])
    return sum(1 for r in results if r is True)


# --------------------------------------------------------------------------- #
# Velocity
# --------------------------------------------------------------------------- #

async def refresh_velocities(now: Optional[datetime] = None) -> int:
    """Recompute + store the 0..1 heat of every trend seen in the last 14 days
    (dismissed ones included, so "Show dismissed" stays consistent)."""
    now = now or datetime.now(timezone.utc)
    sightings = await store.recent_sightings_for_velocity(72)
    # The per-source medians amplitude is measured against. Built once over the
    # WHOLE window, not per trend: "4x the typical Google entry" only means
    # something if "typical" is the same number for every card in the run.
    stats = ranking.source_stats([s for rows_ in sightings.values() for s in rows_])
    rows: list[dict] = []
    # include_filtered / content_days=None: this is the recompute, not the feed.
    # A flagged or ageing row still needs a current heat or "Show hidden" shows
    # a stale number, and query_trends now applies both gates by default.
    kw = {"include_sensitive": True, "include_filtered": True,
          "since_days": 14, "content_days": None, "cap": 5000}
    rows.extend(await store.query_trends(**kw))
    rows.extend(await store.query_trends(status="dismissed", **kw))
    seen: set[int] = set()
    todo: list[tuple[int, float]] = []
    for t in rows:
        tid = int(t["id"])
        if tid in seen:
            continue
        seen.add(tid)
        todo.append((tid, ranking.compute_velocity(t, sightings.get(tid, []), now=now,
                                                   source_stats=stats)))

    async def _save(tid: int, v: float) -> bool:
        try:
            await store.save_velocity(tid, v)
            return True
        except Exception:  # noqa: BLE001
            log.exception("[general-trends] could not save velocity for trend #%d", tid)
            return False

    results = await _gather_limited([_save(tid, v) for tid, v in todo])
    return sum(1 for r in results if r is True)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

# The pass runs in three stages, cheapest first:
#
#   0. free, in Python  — the ingest screen already dropped unsafe and stale
#      items; here we only work out how much EVIDENCE each trend actually has.
#   1. one flash call over the RAW ITEMS, before clustering (``label_items``) —
#      it names each item (clustering rung 3) and triages it. Deliberately ONE
#      call serving two consumers.
#   2. the good model, small batches — the anchored fit score.
#
# Every rejected trend is still WRITTEN (fit_score 0 + an honest fit_reason),
# so nothing disappears from the feed and the next refresh can rescue it once a
# second source corroborates it.

# --------------------------------------------------------------------------- #
# Scoring — stage 0: what we actually know, for free
# --------------------------------------------------------------------------- #

# Sources whose items carry NO descriptive text of their own — the title (often
# a bare hashtag) plus a counter is the entire evidence. trends24 sets
# summary="" for every item; Wikipedia's summary is the identical boilerplate
# "#N most-viewed on English Wikipedia · X views" for all forty articles.
NO_TEXT_SOURCES = frozenset({"x_trends", "wikipedia"})
# ... and of those, the ones whose TITLE is not an identification either. A
# Wikipedia article title ("Hayden Panettiere") names a real thing; an X trend
# ("#المياه_الإقليمية", "Official Sources") names nothing on its own, so it is
# only worth scoring once a second source corroborates it.
UNIDENTIFIABLE_ALONE_SOURCES = frozenset({"x_trends"})

# A summary shorter than this is not evidence.
MIN_EVIDENCE_CHARS = 60
# Corroboration thresholds — either bar clears "thin".
CORROBORATED_SOURCES = 2
CORROBORATED_SIGHTINGS = 3

# The fit below which a trend is hidden by default in the UI. Skipped trends are
# stored at fit_score=0 with an honest fit_reason rather than deleted, so
# "Min fit: any" still shows everything. Mirrors trend_scoring's measured
# NEWS_MIN_FIT_DEFAULT convention.
TRENDS_MIN_FIT_DEFAULT = 25


def _to_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def evidence_profile(trend: dict, *, now: Optional[datetime] = None) -> dict:
    """What we actually know about one trend (or one raw item), before any
    model sees it.

    Returns ``{sources, source_count, sighting_count, has_text, quality,
    age_days}`` where ``quality`` is:

      * ``corroborated`` — 2+ independent sources, or 3+ pieces of evidence
      * ``thin``         — one source, but it shipped real descriptive text
      * ``bare``         — one source, a name and a counter, nothing else

    Corroboration counts ``evidence_count`` (distinct source+key actually on
    file) in preference to ``sighting_count``, which is a POLL counter: one
    Wikipedia page re-read nine times is nine sightings and one piece of
    evidence, and reading the poll counter is how "Frank Beard / ZZ Top" looked
    nine-times corroborated from a single source."""
    now = now or datetime.now(timezone.utc)
    sources = [str(s) for s in (trend.get("sources") or []) if s]
    if not sources:
        sources = [str(e.get("source")) for e in (trend.get("evidence") or [])
                   if isinstance(e, dict) and e.get("source")]
    if not sources and trend.get("source"):        # a raw item, not a trend row
        sources = [str(trend.get("source"))]
    sources = list(dict.fromkeys(sources))

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    n_evidence = _int(trend.get("evidence_count"))
    if n_evidence <= 0:
        n_evidence = _int(trend.get("sighting_count"))
    n_evidence = max(n_evidence, len([e for e in (trend.get("evidence") or [])
                                      if isinstance(e, dict)]), 1 if sources else 0)

    summary = gts.collapse_ws(str(trend.get("summary") or ""))
    has_text = (len(summary) >= MIN_EVIDENCE_CHARS
                and not all(s in NO_TEXT_SOURCES for s in sources or ["?"]))

    if len(sources) >= CORROBORATED_SOURCES or n_evidence >= CORROBORATED_SIGHTINGS:
        quality = "corroborated"
    elif has_text:
        quality = "thin"
    else:
        quality = "bare"

    first = _to_dt(trend.get("first_seen_at"))
    age_days = (now - first).total_seconds() / 86400.0 if first else 0.0
    return {
        "sources": sources, "source_count": len(sources),
        "sighting_count": n_evidence, "has_text": has_text, "quality": quality,
        "age_days": round(max(age_days, 0.0), 1),
    }


# Why a trend never reached the model → the fit_reason stored on its row, so the
# card explains itself instead of showing a silent 0.
_SKIP_REASONS = {
    "no_evidence": "Filtered before scoring: one bare token from one source, with "
                   "no description, no corroboration and nothing to identify it by. "
                   "It will be scored if a second source picks it up.",
}


def prefilter_trend(trend: dict, *, now: Optional[datetime] = None
                    ) -> tuple[str, Optional[str], dict]:
    """``(verdict, reason, profile)`` for one pending trend, in Python only.

    Deliberately ONE rule. Content safety and recency used to be duplicated
    here as a second regex and a second clock; they now live in
    ``general_trends_safety`` / ``general_trends_freshness`` and run at INGEST,
    where they are measured and where they stop us paying to embed and score
    what we will hide. What is left is the one judgement that needs the folded
    trend rather than the raw item: a bare token, from one source, from a
    source whose titles do not identify anything."""
    prof = evidence_profile(trend, now=now)
    if (prof["quality"] == "bare"
            and prof["source_count"] <= 1
            and all(s in UNIDENTIFIABLE_ALONE_SOURCES
                    for s in (prof["sources"] or ["?"]))):
        return "skip", "no_evidence", prof
    return "score", None, prof


def skip_score(trend: dict, reason: str, profile: dict) -> dict:
    """The row written for a trend that never reached the model."""
    score = {
        "clean_title": None,            # never overwrite the raw title
        "topic": None, "topic_summary": None, "theme": None, "trend_kind": None,
        "sensitivity": "caution",
        "fit_score": 0,
        "fit_reason": _SKIP_REASONS.get(reason, "Filtered before scoring."),
        "uae_relevance": 0,
        "suggested_angle": None, "suggested_format": None, "effort": None,
        "tags": [],
        "confidence": 0,
        "evidence_quality": profile.get("quality"),
        "skip_reason": reason,
    }
    score["opportunity_score"] = ranking.compute_opportunity({**trend, **score})
    return score


# --------------------------------------------------------------------------- #
# Scoring — stage 1: label + triage (cheap model, big batches, RAW ITEMS)
# --------------------------------------------------------------------------- #
# This runs BEFORE clustering, over the raw items, because clustering needs the
# canonical name before any trend row exists — and because items and trends are
# within 10% of each other in count anyway (395 items -> 363 trends in the
# measured run), so there is no batch to save by waiting.

TRIAGE_BATCH_SIZE = 20
TRIAGE_MAX_TOKENS = 4000

_TRIAGE_SYSTEM = (
    "You are triaging raw signals for the social team at 42 Abu Dhabi — a "
    "tuition-free, peer-to-peer coding school in Abu Dhabi, UAE.\n\n"
    "Each item below is something a public source reported as popular. Most of "
    "them are NOT trends. Your only job in this pass is to NAME each item, say "
    "what it actually is, and say whether it is safe to look at further. You "
    "are not writing content ideas here and you are not scoring anything.\n\n"
    "Answer these, in order, for every item.\n\n"
    "1. `identified` — do you actually know what this refers to?\n"
    "   - `known`: you recognise the specific thing, from your own knowledge or "
    "from the evidence shown, well enough to explain it to someone who has not "
    "seen it.\n"
    "   - `inferred`: the evidence in front of you describes something concrete "
    "(a post with a body, a news line, a meme write-up) that you can summarise, "
    "even though you did not know it before.\n"
    "   - `unknown`: you cannot say what it refers to. A hashtag you can "
    "TRANSLATE is still `unknown` — turning the words into English is not "
    "knowing the story behind them. Never guess a plausible-sounding meaning. "
    "`unknown` is a correct and useful answer, and it is the answer we want "
    "whenever the evidence is a bare phrase and a counter.\n\n"
    "2. `canonical_name` — the ONE name this thing should be filed under, so "
    "two sources reporting the same story land on the same card. Rules:\n"
    "   - Use the most widely recognised English name for the subject: "
    "'Al-Nassr vs Al-Diriyah', not 'الدرعية ضد النصر' and not 'Saudi league "
    "match'.\n"
    "   - Name the SUBJECT, not the story about it: 'Hayden Panettiere', not "
    "'Hayden Panettiere death hoax spreads'.\n"
    "   - Keep it short — a name, not a headline — and be consistent: the same "
    "thing must get the same name every time you see it.\n"
    "   - If `identified` is `unknown`, write exactly `unknown`. Do NOT invent "
    "a name for something you cannot identify; a wrong name merges two "
    "unrelated stories into one card, which is worse than two cards.\n\n"
    "3. `is_trend` — what kind of thing is it?\n"
    "   - `movement`: many people are making, doing or talking about this right "
    "now — a meme format, a sound, a challenge, a live news moment, a release "
    "people are reacting to.\n"
    "   - `single_post`: one post that did well. Possibly interesting, but one "
    "post is not a movement.\n"
    "   - `evergreen`: a permanently popular page, term or topic that looks "
    "'trending' only because it is popular every single day — a well-known "
    "company, country, app or celebrity's reference page.\n"
    "   - `not_a_trend`: a site's own housekeeping, a feed artefact, a generic "
    "phrase, or something too vague to be anything at all.\n\n"
    "4. `freshness` — judge from what YOU know, not from when we saw it. We "
    "fetch continuously, so 'we saw it today' tells you nothing.\n"
    "   - `breaking`: started in the last few days and still climbing.\n"
    "   - `current`: alive this week; a young person would recognise it now.\n"
    "   - `fading`: peaked weeks ago; posting it now would read as late.\n"
    "   - `old`: peaked months or years ago, or it is a permanent fixture "
    "rather than a moment. Say `old` even though we just saw it — especially "
    "then.\n\n"
    "5. `sensitivity` — what a school's public account can touch.\n"
    "   - `avoid`: sexual or suggestive content of any kind, thirst traps, "
    "nudity, 'rate my girlfriend/boyfriend', dating, hookups, relationship "
    "drama, cheating, anything whose appeal is a body; deaths, funerals, "
    "tragedies, accidents, disasters, missing people; war and armed conflict; "
    "party politics, elections, politicians, protests, sanctions, borders and "
    "territorial disputes; religion or religious practice as something to joke "
    "about or join in on; crime, courts, weapons, drugs, alcohol, vaping, "
    "gambling; hate, harassment, doxxing, body-shaming; medical or "
    "mental-health crises; the private misfortune of a named person.\n"
    "   - `caution`: real but delicate — grief and memorials, illness and "
    "recovery, layoffs and money stress, exam stress and burnout, visas and "
    "immigration, brand pile-ons, any debate where a joke could read as "
    "punching down.\n"
    "   - `ok`: everything else.\n"
    "   The test: if you would not be comfortable with a 42 staff member "
    "reading the post out loud at an open day with parents in the room, it is "
    "not `ok`. Set `blocked_reason` to the matching category whenever "
    "`sensitivity` is `avoid`, and to `none` otherwise.\n\n"
    "6. `what_it_is` — one plain sentence, at most 25 words, saying what the "
    "item actually is. If `identified` is `unknown`, write what we literally "
    "have instead: 'an Arabic-language hashtag with 12K tweets and no other "
    "information'.\n\n"
    "Be blunt. A wrong `known` costs us far more than a cautious `unknown`, "
    "because everything downstream treats your answer as fact."
)

_TRIAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                # Declaration order IS generation order for structured output.
                # `identified` comes first so the naming below is constrained by
                # a judgement already made, rather than the judgement being
                # rationalised from a name the model liked the sound of.
                "properties": {
                    "index": {"type": "integer",
                              "description": "the ITEM index given in the prompt"},
                    "identified": {"type": "string",
                                   "enum": ["known", "inferred", "unknown"]},
                    "canonical_name": {
                        "type": "string",
                        "description": "the one short English name to file this "
                                       "under, or exactly 'unknown'"},
                    "is_trend": {"type": "string",
                                 "enum": ["movement", "single_post", "evergreen",
                                          "not_a_trend"]},
                    "freshness": {"type": "string",
                                  "enum": ["breaking", "current", "fading", "old"]},
                    "sensitivity": {"type": "string",
                                    "enum": ["ok", "caution", "avoid"]},
                    "blocked_reason": {
                        "type": "string",
                        "enum": ["none", "sexual", "dating_or_relationships",
                                 "death_or_tragedy", "war_or_conflict", "politics",
                                 "religion", "crime_drugs_or_gambling",
                                 "hate_or_harassment", "health_crisis"]},
                    "what_it_is": {"type": "string",
                                   "description": "one plain sentence, <= 25 words"},
                },
                # EVERY field is required: Gemini silently drops optional ones.
                "required": ["index", "identified", "canonical_name", "is_trend",
                             "freshness", "sensitivity", "blocked_reason",
                             "what_it_is"],
            },
        },
    },
    "required": ["items"],
}
# Some SDK schema converters do not preserve dict insertion order. Stating the
# order explicitly costs nothing and makes the intent survive that conversion.
_TRIAGE_SCHEMA["properties"]["items"]["items"]["propertyOrdering"] = list(
    _TRIAGE_SCHEMA["properties"]["items"]["items"]["properties"])

TRIAGE_VERDICTS = ("known", "inferred", "unknown")
TRIAGE_KINDS = ("movement", "single_post", "evergreen", "not_a_trend")
FRESHNESS = ("breaking", "current", "fading", "old")


def build_triage_prompt(items: list[dict]) -> str:
    """The triage prompt: the raw text verbatim in its original script, the
    corroboration line, and nothing that invites a story."""
    lines = [f"Triage the {len(items)} items below. Return one entry per item, "
             "echoing its index."]
    for i, t in enumerate(items, start=1):
        prof = t.get("_profile") or evidence_profile(t)
        raw = gts.collapse_ws(str(t.get("title") or ""))[:160]
        bits = [f"ITEM {i}",
                f'raw text (verbatim, may be a hashtag in any language): "{raw}"',
                "corroboration: " + corroboration_line(t, prof)]
        summary = gts.collapse_ws(str(t.get("summary") or ""))[:280]
        bits.append("description from the source: "
                    + (summary or "(none — the source gave us no description)"))
        evidence = [e for e in (t.get("evidence") or []) if isinstance(e, dict)]
        if evidence:
            bits.append("where we saw it:")
            for e in evidence[:EVIDENCE_PER_TREND]:
                bits.append("  - " + evidence_line(e))
        elif t.get("source"):
            bits.append("where we saw it:\n  - " + evidence_line(t))
        lines.append("\n".join("  " + b for b in bits))
    return "\n\n".join(lines)


def parse_triage_response(raw: Any, items: list[dict]) -> dict[int, dict]:
    """Map the triage reply back onto items BY POSITION (0-based), clamped to
    the enums.

    Position, not trend id, because this pass runs over RAW ITEMS which have no
    id yet — the caller zips the answers back onto its own list."""
    out: dict[int, dict] = {}
    if not isinstance(raw, dict):
        return out
    for entry in (raw.get("items") or []):
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            _DROPS["triage_bad_index"] += 1
            continue
        if not (1 <= idx <= len(items)):
            _DROPS["triage_bad_index"] += 1
            continue
        out[idx - 1] = {
            "identified": _one_of(entry.get("identified"), TRIAGE_VERDICTS) or "unknown",
            "canonical_name": gts.collapse_ws(str(entry.get("canonical_name") or ""))[:120],
            "is_trend": _one_of(entry.get("is_trend"), TRIAGE_KINDS) or "not_a_trend",
            "freshness": _one_of(entry.get("freshness"), FRESHNESS) or "current",
            "sensitivity": (_one_of(entry.get("sensitivity"), tuple(sorted(SENSITIVITIES)))
                            or "caution"),
            "blocked_reason": str(entry.get("blocked_reason") or "none").strip().lower()[:40],
            "what_it_is": gts.collapse_ws(str(entry.get("what_it_is") or ""))[:300] or None,
        }
    return out


async def triage_batch(items: list[dict]) -> dict[int, dict]:
    """Stage 1 for ONE batch → ``{position: triage}``.

    Empty dict on a transient failure or an unreadable reply — the caller then
    treats those items as un-labelled and lets stage 2 judge them on the
    corroboration line alone. ``ScoringUnavailable`` when the model cannot be
    reached at all, so the caller stops instead of paying for 20 more failures."""
    if not items:
        return {}
    prompt = build_triage_prompt(items)
    try:
        raw = await gemini_text(
            prompt, system=_TRIAGE_SYSTEM, json_schema=_TRIAGE_SCHEMA,
            temperature=0.1, max_tokens=TRIAGE_MAX_TOKENS,
            # Flash + a schema + hidden thinking tokens is the known truncation
            # trap (see caption_service / asset_analyzer). This pass is
            # classification, not reasoning, so the budget is off and the reply
            # always fits.
            thinking_budget=0,
            model=(config.general_trends_triage_model
                   or config.vertex_gemini_fast_model or None),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_unavailable(exc):
            raise ScoringUnavailable(_explain_unavailable(exc)) from exc
        _DROPS["triage_batches_failed"] += 1
        log.exception("[general-trends] triage batch failed (%d item(s))", len(items))
        return {}
    if isinstance(raw, dict) and raw.get("_raw") is not None:
        _DROPS["triage_batches_failed"] += 1
        log.warning("[general-trends] triage returned unparseable JSON — skipping batch")
        return {}
    _DROPS["triage_batches_ok"] += 1
    return parse_triage_response(raw, items)


async def label_items(items: list[dict]) -> tuple[list[dict], str]:
    """Stage 1 over the whole run's RAW ITEMS.

    Returns ``(labels, status)`` where ``labels`` is PARALLEL to ``items`` (an
    empty dict where the pass could not answer) and ``status`` is:

      * ``"ok"``          — every batch answered
      * ``"partial"``     — some batch failed; its items carry ``{}``
      * ``"unavailable"`` — the model could not be reached at all

    MANDATORY DEGRADATION: this function never raises. Clustering rung 3 is
    skipped when there is no label, scoring falls back to the corroboration
    line, and the run still finishes ``done``. The labelling pass is a layer on
    top, never a dependency."""
    labels: list[dict] = [{} for _ in (items or [])]
    if not items:
        return labels, "ok"
    status = "ok"
    for start in range(0, len(items), TRIAGE_BATCH_SIZE):
        batch = items[start:start + TRIAGE_BATCH_SIZE]
        try:
            answers = await triage_batch(batch)
        except ScoringUnavailable as exc:
            log.warning("[general-trends] labelling unavailable (%s) — clustering "
                        "without canonical names", exc)
            return [{} for _ in items], "unavailable"
        except Exception:  # noqa: BLE001 — belt and braces; triage_batch swallows
            log.exception("[general-trends] labelling batch blew up")
            answers = {}
        if not answers:
            status = "partial"
        for pos, tri in answers.items():
            labels[start + pos] = tri
    return labels, status


def triage_verdict(triage: dict) -> tuple[str, Optional[str]]:
    """``("score"|"skip", reason)`` from one triage answer. An empty answer
    always scores: a clerical failure in the cheap pass must not delete a
    trend."""
    if not triage:
        return "score", None
    if triage.get("sensitivity") == "avoid":
        return "skip", "sensitive"
    if triage.get("identified") == "unknown":
        return "skip", "unidentified"
    if triage.get("is_trend") == "not_a_trend":
        return "skip", "not_a_trend"
    if triage.get("freshness") == "old":
        return "skip", "old"
    return "score", None


_TRIAGE_SKIP_REASONS = {
    "sensitive": "Not scored: the topic is one 42's feed stays away from ({why}).",
    "unidentified": "Not scored: nobody could say what this actually refers to "
                    "from the evidence we have.",
    "not_a_trend": "Not scored: this is a feed artefact or a generic phrase, "
                   "not something people are doing.",
    "old": "Not scored: recognised, but it peaked long ago — posting it now "
           "would read as late.",
}


def triage_skip_score(trend: dict, reason: str, triage: dict, profile: dict) -> dict:
    """The row written for a trend the triage pass rejected. Keeps the model's
    one-line ``what_it_is`` so the card still says what the thing was."""
    why = str(triage.get("blocked_reason") or "").replace("_", " ")
    text = _TRIAGE_SKIP_REASONS.get(reason, "Not scored.")
    score = {
        "clean_title": None,
        "topic": None,
        "topic_summary": triage.get("what_it_is"),
        "theme": None, "trend_kind": None,
        "sensitivity": triage.get("sensitivity") or "caution",
        "fit_score": 0,
        "fit_reason": text.format(why=why or "sensitive"),
        "uae_relevance": 0,
        "suggested_angle": None, "suggested_format": None, "effort": None,
        "tags": [],
        "confidence": 0,
        "evidence_quality": profile.get("quality"),
        "skip_reason": reason,
        "identified": triage.get("identified"),
        "is_trend": triage.get("is_trend"),
        "freshness": triage.get("freshness"),
    }
    score["opportunity_score"] = ranking.compute_opportunity({**trend, **score})
    return score


# --------------------------------------------------------------------------- #
# Scoring — stage 2: the fit pass (the good model, small batches)
# --------------------------------------------------------------------------- #
# The batch shrank from 8 to 6 and the schema grew by one field. That is
# deliberate: a live batch of 8 already truncated at max_tokens=6000 (2.5-pro
# spends hidden thinking tokens out of the SAME output pool) and a truncated
# reply comes back as the {"_raw": …} sentinel — the whole batch is lost
# SILENTLY. A bigger schema is paid for with a smaller batch, never a bigger
# cap: raising max_tokens only moves the cliff.
SCORE_BATCH_SIZE = 6
SCORE_MAX_TOKENS = 8000

# The anchors below are INVENTED, not drawn from any scored run. Lifting real
# examples would let the prompt win by leakage and any before/after measurement
# would mean nothing. Same convention as trend_scoring._SYSTEM_NEWS, which
# fixed this exact bimodal compression (164 rows at 0-9, 91 at 70-100) for the
# news feed and was never back-ported here.
_SYSTEM = (
    "You advise the social team at 42 Abu Dhabi — a tuition-free, peer-to-peer, "
    "project-based coding school in Abu Dhabi, UAE. Students learn without "
    "teachers or lectures, admission runs through an intensive month called the "
    "Piscine, and no prior coding experience is required.\n\n"
    # "a computer lab", not "a cluster": this line is where the model learns
    # its vocabulary for the whole answer, and it used to hand it the insider
    # word for the room with nothing attached. The nickname is kept, labelled
    # as a nickname.
    "What 42 HAS: a campus, a computer lab open around the clock (students call "
    "it the cluster; nobody outside does), students "
    "who are online all day, phones, and no budget. What 42 does NOT have: "
    "talent fees, a film crew, drones, brand partnerships, or standing to speak "
    "for anyone but itself.\n\n"
    "Every item below has already been checked: it is a real, identifiable "
    "thing that a school is allowed to talk about. Your job is to say how well "
    "42 could ride it, and how.\n\n"

    "WHAT COUNTS AS A TREND — READ THIS FIRST\n"
    "A trend is a FORMAT PEOPLE ARE COPYING. Many different people are making "
    "their own version of the same thing, and a brand can join by making one "
    "too. It is not a thing that merely happened.\n"
    "- A penguin walking away from its colony toward the mountains became a "
    "trend when thousands of people restaged it for their own situation — a "
    "shop in the UAE posted the penguin walking to THEIR store. The trend is "
    "the reusable joke, not the penguin.\n"
    "- Couples proposing at the top of a skyscraper holding a handwritten "
    "notice started as a news moment, and BECAME a trend when other people "
    "started putting their own message on the notice. A news moment is a trend "
    "only once people are remaking it.\n"
    "- An earthquake, an election, a product launch, a football result, a "
    "celebrity story, a page lots of people searched: these are things that "
    "HAPPENED. Nobody can make their own version. They are NOT trends, however "
    "big they are.\n\n"

    "`trend_kind` is that judgement, and it is used to decide whether the item "
    "reaches the team at all:\n"
    "- `format` — a reusable structure people fill with their own content: a "
    "template, a transition, a shot list, a caption pattern, a before/after.\n"
    "- `meme` — a specific image, character, phrase or joke people are "
    "reposting and remixing.\n"
    "- `sound` — an audio clip people are making videos to. Using the sound IS "
    "joining in.\n"
    "- `challenge` — an action people are performing and filming themselves "
    "doing.\n"
    "- `news`, `event`, `product`, `culture`, `other` — everything that is a "
    "thing that happened rather than a thing people are making. Be honest and "
    "use these when they are right; they are not a failure grade, and a "
    "mislabelled news story wastes the team's time more than a missing card "
    "does.\n\n"

    "So: before anything else, ask 'what would OUR version of this look like?' "
    "If you cannot describe our version in one sentence, it is not a format, "
    "and `replication` must say so.\n\n"

    "VIEWS PER POST — the strongest clue you will get\n"
    "A TikTok hashtag line may carry 'N views per post'. That number separates "
    "the two cases better than anything else in the evidence:\n"
    "- A LOW number (roughly under 1,500) means a great many people each made "
    "their own video and most were seen by a few hundred people. That is what "
    "mass participation looks like. It is a FORMAT.\n"
    "- A HIGH number (roughly above 5,000) means a few videos were watched by "
    "an enormous number of people who did not make one themselves. That is a "
    "NEWS MOMENT wearing a hashtag.\n"
    "Measured on real data: a game's summer-fest tag ran at 196 views per post "
    "with 180,000 posts — everyone was making one. A tag for a celebrity's "
    "death ran at 10,725 views per post — everyone was watching, nobody was "
    "making. The two look identical if you only read the view count.\n"
    "Use it, and say so in `replication` when it decides the call.\n\n"

    "WHAT YOU ARE TOLD, AND WHAT IT MEANS\n"
    "Each item carries a `corroboration:` line. That line is everything we know "
    "about how widely the thing is actually spreading, and you must read it "
    "literally.\n"
    "- '3 sources' means three independent platforms saw it. That is a real "
    "trend.\n"
    "- '1 source · 1 sighting' means one post, on one platform, once. That is "
    "one post. It can still be a usable IDEA, but it is not evidence that "
    "anything is spreading, and you must not write about it as if it were.\n"
    "Do not invent context. If the evidence does not support a claim, leave the "
    "claim out of `topic_summary` and out of `fit_reason`.\n\n"

    "FIT — USE THE WHOLE SCALE\n"
    "`fit_score` answers one question: if 42 posted about this in the next few "
    "days, would it land? Anchor your number to this scale and interpolate "
    "between the anchors.\n"
    "- 100 — already 42's world: coding, building, learning, AI changing what a "
    "student can do, all-nighters, the feeling of finishing something hard, "
    "jobs and futures for young people in the UAE. Four students and a phone "
    "could shoot it on campus today and it would look native. Example: a format "
    "where people show the ugly first version of a thing next to the finished "
    "one.\n"
    "- 80 — a general internet format with an obvious campus reskin. No budget, "
    "no talent, no permissions — just a rewrite. Example: a trending audio "
    "where people list the lies they tell themselves, restaged as the lies "
    # "a lab", not "the cluster": this is the ONLY example of a GOOD angle
    # in the fit anchors, so its vocabulary is the vocabulary the model
    # copies into every suggested_angle it writes.
    "students tell themselves at 3am in a lab that never closes.\n"
    "- 60 — usable, but it needs one real idea or one small production step: a "
    "specific spot on campus, a prop, a graphic, one student willing to be on "
    "camera. Relevant to the audience without being native to the school.\n"
    "- 40 — 42 can comment but not participate. The subject is adjacent — an "
    "industry story, a regional moment 42 has no stake in — so the post would "
    "be commentary about someone else's thing.\n"
    "- 20 — this belongs to another world: a sports result, a celebrity's "
    "private life, a product 42 has no relationship with. Posting it would make "
    "42 look like a fan account.\n"
    "- 0 — nothing to post: a topic 42 must stay away from, or a name with no "
    "story attached.\n\n"

    "Rules that override your instinct:\n"
    "- A huge, beautiful trend 42 cannot make anything for is a LOW fit, "
    "however big it is.\n"
    "- A small, unglamorous trend that maps exactly onto student life is a HIGH "
    "fit, however small it is.\n"
    "- Spread your numbers within a batch. If more than two items in a batch "
    "score 85 or above, you are inflating. Ties are allowed when two items "
    "really are equally usable — say so in `fit_reason` when you tie.\n"
    "- The anchors are anchors, not a menu. 73 and 47 are normal answers; a "
    "batch that only contains 0, 20, 40, 60, 80 and 100 is a batch you have not "
    "thought about.\n\n"

    "CAPS YOU MUST RESPECT (they are checked, and a number over the cap is cut "
    "down anyway — so use the room below the cap to discriminate)\n"
    "- `evidence: bare` → `fit_score` at most 25.\n"
    "- `this is ONE post` → at most 55, UNLESS the post itself is the format (a "
    "meme template, a sound, a challenge anyone can copy), in which case score "
    "it on its merits and say why in `fit_reason`.\n"
    "- `freshness: fading` → at most 60.\n\n"

    # The audience rule sits directly above WHAT TO WRITE because that is the
    # section that produces words a stranger will read. `suggested_angle` in
    # particular is not an internal note: it is handed to the person who films
    # the post AND to the ideation prompt as the seed of the finished caption,
    # so an angle only a 42 employee can parse is a defect, not a style choice.
    + PLAIN_LANGUAGE_RULE + "\n\n"

    "WHAT TO WRITE\n"
    "- `clean_title` — the human name of the thing, at most 60 characters, not "
    "a headline: 'Nihilist Penguin meme', not 'the nihilist penguin meme is "
    "taking over tiktok'. If the raw text is a hashtag you cannot improve on, "
    "keep the hashtag.\n"
    "- `topic_summary` — one or two plain sentences on what it IS, for a "
    "colleague who has not seen it. Only what the evidence supports.\n"
    "- `theme` — a short reusable bucket: 'meme format', 'gaming', 'tech "
    "culture', 'pop culture', 'sports', 'UAE moment', 'news'.\n"
    "- `fit_reason` — write this BEFORE you pick the number, and make it the "
    "reason FOR the number: one concrete sentence naming what 42 would do, or "
    "what stops it.\n"
    "- `topic` — the transferable ANGLE, at most 8 words. Not a description of "
    "the trend.\n"
    "- `suggested_angle` — how 42 specifically does it: the place on campus, "
    "the people, the action. This one is not a note to a colleague. A person "
    "reads it and films it, and its words become the words of the finished "
    "post, so write it the way the post will sound: 'Three students at 2am in "
    "a lab that never closes, screens behind them, one line each' — not 'make "
    "a fun reel about coding', and not 'Piscine students in the cluster', "
    "which means nothing to the person scrolling past it. Describe the room, "
    "not its 42 nickname.\n"
    "- `uae_relevance` — how much a UAE audience would recognise or care about "
    "this right now, not how much you wish they would.\n"
    "- `confidence` — 0-100: how sure you are that this whole assessment is "
    "right. Low when you are working from a name alone or the evidence is one "
    "line.\n"
    "- `sensitivity` — 'avoid' for anything about death, tragedy, war, "
    "politics, religion, crime, drugs, sex, dating or a named person's private "
    "misfortune; 'caution' for grief, illness, money stress, burnout, or "
    "anything divisive; 'ok' otherwise. A school's brand never jokes about "
    "someone's death."
)

_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                # Declaration order IS generation order for structured output.
                # `fit_reason` is declared BEFORE `fit_score` on purpose: the
                # old schema emitted the number first, so the sentence was a
                # post-hoc rationalisation of a number chosen from nothing.
                "properties": {
                    "index": {"type": "integer",
                              "description": "the TREND index given in the prompt"},
                    "clean_title": {"type": "string",
                                    "description": "human name of the trend, <= 60 chars"},
                    "topic_summary": {"type": "string",
                                      "description": "what the trend IS, 1-2 plain "
                                                     "sentences, evidence only"},
                    "theme": {"type": "string",
                              "description": "a short reusable bucket, e.g. 'meme "
                                             "format', 'gaming', 'tech culture', "
                                             "'pop culture', 'sports', 'UAE moment', "
                                             "'news'"},
                    # BEFORE trend_kind on purpose: structured output is
                    # autoregressive, so making the model describe who is
                    # copying the thing first means the kind is chosen from
                    # that description rather than rationalised after it.
                    "replication": {"type": "string",
                                    "description": "who is making their own version, "
                                                   "and what one looks like in a "
                                                   "sentence — or exactly 'nobody: "
                                                   "this is a thing that happened'"},
                    "trend_kind": {"type": "string", "enum": sorted(TREND_KINDS)},
                    "sensitivity": {"type": "string",
                                    "enum": ["ok", "caution", "avoid"]},
                    "fit_reason": {"type": "string",
                                   "description": "one concrete sentence — the reason "
                                                  "FOR the score, written before it"},
                    "fit_score": {"type": "integer",
                                  "description": "0-100 against the anchored scale"},
                    "uae_relevance": {"type": "integer", "description": "0-100"},
                    "confidence": {"type": "integer",
                                   "description": "0-100, how sure you are of this "
                                                  "whole assessment"},
                    "topic": {"type": "string",
                              "description": "the transferable angle, max 8 words"},
                    "suggested_angle": {"type": "string",
                                        "description": "how 42 rides it — the specific "
                                                       "reframe for a coding school in "
                                                       "Abu Dhabi, in words someone who "
                                                       "has never heard of 42 already "
                                                       "understands"},
                    "suggested_format": {"type": "string",
                                         "enum": ["reel", "carousel", "static"]},
                    "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                # EVERY field is required: Gemini silently drops optional ones.
                "required": ["index", "clean_title", "topic_summary", "theme",
                             "replication",
                             "trend_kind", "sensitivity", "fit_reason", "fit_score",
                             "uae_relevance", "confidence", "topic", "suggested_angle",
                             "suggested_format", "effort", "tags"],
            },
        },
    },
    "required": ["scores"],
}
# Belt and braces for the reason-before-score fix: some SDK schema converters
# rebuild the properties map and lose Python's insertion order.
_SCHEMA["properties"]["scores"]["items"]["propertyOrdering"] = list(
    _SCHEMA["properties"]["scores"]["items"]["properties"])


def _fmt_metric(value: Any, label: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(v):
        return ""
    if v >= 1_000_000:
        num = f"{v / 1_000_000:.1f}M"
    elif v >= 10_000:
        num = f"{v / 1_000:.0f}K"
    else:
        num = f"{int(v):,}"
    return f"{num} {label}".strip() if label else num


def evidence_line(s: dict) -> str:
    """``Google Trends · US · pete hegseth · 2,000 searches`` — one sighting."""
    source = str(s.get("source") or "")
    bits = [gts.SOURCE_LABELS.get(source, source) or "?",
            str(s.get("region") or "global"),
            gts.collapse_ws(str(s.get("title") or ""))[:120] or "(untitled)"]
    metric = _fmt_metric(s.get("metric_value"), s.get("metric_label"))
    if metric:
        bits.append(metric)
    return " · ".join(bits)


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value or "")[:10]


def corroboration_line(trend: dict, profile: Optional[dict] = None) -> str:
    """The one line that tells the model how much this is actually spreading.

    The old prompt printed `sources:` and `sightings: 3` and left the model to
    decide what that was worth — which is how one Reddit post became a fit-95
    "internet trend". This line states what the number is WORTH."""
    p = profile or evidence_profile(trend)
    labels = ", ".join(gts.SOURCE_LABELS.get(s, s) for s in p["sources"]) or "unknown source"
    n_src, n_sight = p["source_count"], p["sighting_count"]
    if p["quality"] == "corroborated":
        head = (f"{n_src} independent source(s) ({labels}), {n_sight} sighting(s) "
                "— corroborated, this is spreading")
    elif p["quality"] == "thin":
        head = (f"1 source ({labels}), {n_sight} sighting(s) — this is ONE post "
                "with a description, not evidence that anything is spreading")
    else:
        head = (f"1 source ({labels}), {n_sight} sighting(s), no description — "
                "evidence: bare. We have the words and a counter, nothing else")
    if p["age_days"] >= 1:
        head += f" · first seen {p['age_days']:.0f} day(s) ago"
    return head


def build_batch_prompt(trends: list[dict], brand_note: str = "") -> str:
    """One prompt describing N trends. Each trend dict may carry ``evidence``
    (a list of sighting dicts, up to EVIDENCE_PER_TREND shown), ``_profile``
    (from ``evidence_profile``) and ``_triage`` (the stage-1 answer)."""
    lines: list[str] = []
    if brand_note:
        lines.append("WHAT 42 SOUNDS AND LOOKS LIKE:\n" + brand_note.strip() + "\n")
    lines.append(
        f"Assess the {len(trends)} trends below. Return one entry per trend, "
        "echoing its index."
    )
    for i, t in enumerate(trends, start=1):
        prof = t.get("_profile") or evidence_profile(t)
        tri = t.get("_triage") or {}
        # Verbatim, in its original script: translate-and-embellish then has to
        # be a visibly separate act rather than something the prompt did for the
        # model before it started thinking.
        raw = gts.collapse_ws(str(t.get("title") or ""))[:160]
        bits = [f"TREND {i}", f'raw text (verbatim): "{raw}"']
        if tri.get("what_it_is"):
            bits.append(f"what it is: {tri['what_it_is']}")
        if tri.get("freshness"):
            bits.append(f"freshness: {tri['freshness']}"
                        + (" — riding this now would read as late"
                           if tri["freshness"] == "fading" else ""))
        bits.append("corroboration: " + corroboration_line(t, prof))
        if prof["quality"] != "corroborated" and tri.get("is_trend") == "single_post":
            bits.append("this is ONE post — cap fit at 55 unless the post itself "
                        "is a copyable format")
        regions = [str(r) for r in (t.get("regions") or []) if r]
        if regions:
            bits.append("regions: " + ", ".join(regions))
        first, last = _date(t.get("first_seen_at")), _date(t.get("last_seen_at"))
        if first or last:
            bits.append(f"seen: {first or '?'} → {last or '?'}")
        summary = gts.collapse_ws(str(t.get("summary") or ""))[:400]
        bits.append(f"summary: {summary or '(none)'}")
        evidence = [e for e in (t.get("evidence") or []) if isinstance(e, dict)]
        if evidence:
            bits.append("what we saw:")
            for e in evidence[:EVIDENCE_PER_TREND]:
                bits.append("  - " + evidence_line(e))
        lines.append("\n".join("  " + b for b in bits))
    return "\n\n".join(lines)


# The caps the prompt states, enforced in Python. THE PROMPT ASKS; THE PARSER
# GUARANTEES. Never trust a model to obey a ceiling it can rationalise past.
FIT_CAPS = {"bare": 25, "fading": 60, "single_post": 55, "old": 25}
# A meme template, a sound or a challenge posted once IS the trend, so the
# single-post cap is forgiven for those kinds and only those.
FORMAT_KINDS = ("meme", "format", "sound", "challenge")


def apply_fit_caps(score: dict, profile: dict, triage: Optional[dict] = None) -> dict:
    """Cut ``fit_score`` down to what the evidence can carry and record why.

    The tightest cap wins. The cap appends ``[capped at N: reason]`` to
    ``fit_reason`` — the frontend renders that suffix as a chip, so it is the
    user-visible explanation of a low score, not a silent edit."""
    tri = triage or {}
    fit = score.get("fit_score")
    if fit is None:
        return score
    caps: list[tuple[str, int]] = []
    if profile.get("quality") == "bare":
        caps.append(("bare evidence", FIT_CAPS["bare"]))
    if tri.get("freshness") == "fading":
        caps.append(("fading", FIT_CAPS["fading"]))
    if tri.get("freshness") == "old":
        caps.append(("old", FIT_CAPS["old"]))
    if (tri.get("is_trend") == "single_post"
            and _one_of(score.get("trend_kind"), FORMAT_KINDS) is None):
        caps.append(("a single post", FIT_CAPS["single_post"]))
    if not caps:
        return score
    why, cap = min(caps, key=lambda c: c[1])
    if int(fit) > cap:
        score["fit_score"] = cap
        _DROPS["fit_capped"] += 1
        reason = (score.get("fit_reason") or "").rstrip()
        score["fit_reason"] = (f"{reason} [capped at {cap}: {why}]".strip())[:600]
    return score


# How often the model's reply is unusable, and in what way. Ported from
# trend_scoring._DROPS, which exists precisely because silent drops hid a
# ranking bug for a long time; General Trends copied that module's clamps and
# left its counters behind. Process-local and monotonic.
_DROPS: dict[str, int] = {
    "batches_ok": 0, "batches_unparseable": 0, "batches_failed": 0,
    "entries_bad_index": 0, "items_not_returned": 0,
    "fit_missing": 0, "uae_missing": 0, "fit_capped": 0,
    "triage_batches_ok": 0, "triage_batches_failed": 0, "triage_bad_index": 0,
    "prefilter_skipped": 0, "triage_skipped": 0, "scored": 0,
}


def drop_stats() -> dict[str, int]:
    """A snapshot of the scoring-loss counters."""
    return dict(_DROPS)


# Know Your Meme's own `Type:` taxonomy, mapped onto our kinds. KYM classifies
# every entry, and its vocabulary IS the owner's definition in the source's own
# words — `participatory-media`, `exploitable`, `snowclone`, `dance` are formats
# people remake; `death`, `film`, `character`, `viral-video` are things that
# happened. The most specific type wins, in this order.
_KYM_TYPE_TO_KIND: tuple[tuple[str, str], ...] = (
    ("dance", "challenge"),
    ("song", "sound"),
    ("participatory-media", "challenge"),
    ("exploitable", "format"),
    ("snowclone", "format"),
    ("image-macro", "format"),
    ("remix", "format"),
    ("visual-effect", "format"),
    ("fan-labor", "format"),
    ("reaction", "meme"),
    ("catchphrase", "meme"),
    ("copypasta", "meme"),
    ("parody", "meme"),
    ("slang", "meme"),
    ("ai-generated", "meme"),
)


def source_kind(trend: dict) -> Optional[str]:
    """The kind a SOURCE already told us, or None to leave it to the model.

    Only Know Your Meme qualifies today: it publishes a curated `Type:` per
    entry, so asking a model to re-derive it is paying for a worse answer. When
    KYM says an entry has no format type at all (`is_format` False — a death, a
    film, a viral video), that is a real classification too, and the honest
    kind is the one the taxonomy implies rather than a guess."""
    evidence = trend.get("evidence")
    rows = [e for e in (evidence or []) if isinstance(e, dict)]
    for row in rows:
        if str(row.get("source") or "") not in ("kym_entries", "kym"):
            continue
        extra = row.get("extra")
        if not isinstance(extra, dict):
            continue
        types = [str(t).strip().lower() for t in (extra.get("types") or []) if str(t).strip()]
        if not types:
            continue
        for slug, kind in _KYM_TYPE_TO_KIND:
            if slug in types:
                return kind
        # KYM classified it and none of the types is participatory: it is a
        # thing that happened, and the participation gate should hold it back.
        if extra.get("is_format") is False:
            return "culture"
    return None


def apply_source_kind(score: dict, trend: dict) -> None:
    """Overwrite the model's `trend_kind` when a source already published one.

    Mutates in place and records the override in `replication` so the change is
    visible rather than mysterious."""
    kind = source_kind(trend)
    if not kind or kind == score.get("trend_kind"):
        return
    was = score.get("trend_kind")
    score["trend_kind"] = kind
    _DROPS["kind_from_source"] = _DROPS.get("kind_from_source", 0) + 1
    note = f"[kind set from Know Your Meme's own type: {kind}"
    note += f", model said {was}]" if was else "]"
    score["replication"] = f"{(score.get('replication') or '').strip()} {note}".strip()[:400]


def parse_batch_response(raw: Any, trends: list[dict]) -> dict[int, dict]:
    """Map the model's reply back onto trend ids by the echoed index, clamping
    every field into range and enforcing the evidence caps. Tolerant: a short
    or partly malformed reply scores what it can."""
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
        if not (1 <= idx <= len(trends)):
            _DROPS["entries_bad_index"] += 1
            continue
        trend = trends[idx - 1]
        prof = trend.get("_profile") or evidence_profile(trend)
        tri = trend.get("_triage") or {}
        score = {
            "clean_title": gts.collapse_ws(str(entry.get("clean_title") or ""))[:60] or None,
            "topic": str(entry.get("topic") or "").strip()[:160] or None,
            "topic_summary": str(entry.get("topic_summary") or "").strip()[:600] or None,
            "theme": (str(entry.get("theme") or "").strip().lower()[:60] or None),
            # The model's answer to "who is making their own version of this",
            # which is what trend_kind is derived FROM. Kept (in metadata) so a
            # kind you disagree with can be audited rather than argued about.
            "replication": str(entry.get("replication") or "").strip()[:400] or None,
            "trend_kind": _one_of(entry.get("trend_kind"), tuple(sorted(TREND_KINDS))),
            "sensitivity": _one_of(entry.get("sensitivity"), tuple(sorted(SENSITIVITIES))),
            "fit_score": _clamp_int(entry.get("fit_score")),
            "fit_reason": str(entry.get("fit_reason") or "").strip()[:600] or None,
            "uae_relevance": _clamp_int(entry.get("uae_relevance")),
            "confidence": _clamp_int(entry.get("confidence")),
            "suggested_angle": str(entry.get("suggested_angle") or "").strip()[:600] or None,
            "suggested_format": _one_of(entry.get("suggested_format"),
                                        ("reel", "carousel", "static")),
            "effort": _one_of(entry.get("effort"), ("low", "medium", "high")),
            "tags": [str(t)[:40] for t in (entry.get("tags") or []) if str(t).strip()][:8],
            "evidence_quality": prof.get("quality"),
            "identified": tri.get("identified"),
            "is_trend": tri.get("is_trend"),
            "freshness": tri.get("freshness"),
            "skip_reason": None,
        }
        if score["fit_score"] is None:
            _DROPS["fit_missing"] += 1
        if score["uae_relevance"] is None:
            _DROPS["uae_missing"] += 1
        apply_fit_caps(score, prof, tri)
        # STOP PERSISTING THE LAUNDERING. save_score does
        # `title = COALESCE(clean_title, title)`, so an invented English name
        # REPLACES the raw evidence on the row — which is how the bare hashtag
        # "#المياه_الإقليمية" became the card "Financial Transactions / Fintech,
        # fit 75". A title we cannot vouch for must not overwrite what we saw.
        if tri.get("identified") == "unknown" or prof.get("quality") == "bare":
            score["clean_title"] = None
        apply_source_kind(score, trend)
        score["opportunity_score"] = ranking.compute_opportunity({**trend, **score})
        out[int(trend["id"])] = score
    missing = len(trends) - len(out)
    if missing > 0:
        _DROPS["items_not_returned"] += missing
    return out


async def score_batch(trends: list[dict], brand_note: str = "") -> dict[int, dict]:
    """Score one batch → {trend_id: score}. Empty dict on a transient failure
    or an unreadable reply; ScoringUnavailable when the model can't be reached
    at all (billing/403/no creds) so the caller stops instead of retrying."""
    if not trends:
        return {}
    prompt = build_batch_prompt(trends, brand_note)
    try:
        # 2.5-pro spends hidden thinking tokens out of the SAME output pool and
        # a truncated reply comes back as the {"_raw": …} sentinel, not an
        # error — silently losing the whole batch. See SCORE_BATCH_SIZE.
        raw = await gemini_text(
            prompt, system=_SYSTEM, json_schema=_SCHEMA,
            temperature=0.3, max_tokens=SCORE_MAX_TOKENS,
            model=config.general_trends_model or None,
        )
    except Exception as exc:  # noqa: BLE001 — one bad batch must not end the pass
        if _is_unavailable(exc):
            raise ScoringUnavailable(_explain_unavailable(exc)) from exc
        _DROPS["batches_failed"] += 1
        log.exception("[general-trends] scoring batch failed (%d trend(s))", len(trends))
        return {}
    if isinstance(raw, dict) and raw.get("_raw") is not None:
        _DROPS["batches_unparseable"] += 1
        log.warning("[general-trends] scoring returned unparseable JSON — skipping batch")
        return {}
    _DROPS["batches_ok"] += 1
    return parse_batch_response(raw, trends)


async def score_pending(
    *, limit: int = 400,
    labels: Optional[dict[int, dict]] = None,
    on_progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> dict:
    """Score every trend waiting on the AI pass, in three stages.

        0. free, in Python — the evidence profile, and the one bare-token rule
        1. the stage-1 labels this run already produced (``labels``, keyed by
           trend id, from ``cluster_items``) — no model call happens here
        2. the anchored fit pass, only for what survived both

    Every rejected trend is still WRITTEN at ``fit_score=0`` with an honest
    ``fit_reason``, so nothing disappears and the next refresh can rescue it
    once a second source corroborates it.

    A trend with no label — an older row, or one whose triage batch failed — is
    NOT thrown away: it goes to stage 2 with an empty triage dict and is judged
    on the corroboration line, with the caps still applied.

    Returns ``{"scored", "prefiltered", "triaged_out", "total"}``."""
    pending = await store.unscored_trends(limit=limit)
    if not pending:
        return {"scored": 0, "prefiltered": 0, "triaged_out": 0, "total": 0}
    evidence = await store.sightings_for([int(t["id"]) for t in pending],
                                         per_trend=EVIDENCE_PER_TREND)
    now = datetime.now(timezone.utc)
    for t in pending:
        t["evidence"] = evidence.get(int(t["id"]), [])

    by_id = {int(k): v for k, v in (labels or {}).items() if isinstance(v, dict)}
    total = len(pending)
    done = 0
    counts = {"scored": 0, "prefiltered": 0, "triaged_out": 0, "total": total}

    async def _save(trend_id: int, score: dict) -> None:
        nonlocal done
        try:
            await store.save_score(trend_id, score)
            done += 1
        except Exception:  # noqa: BLE001
            log.exception("[general-trends] could not save score for trend #%d", trend_id)

    async def _tick() -> None:
        if on_progress:
            try:
                await on_progress(done, total)
            except Exception:  # noqa: BLE001 — progress is cosmetic
                pass

    # --- stage 0: free ----------------------------------------------------
    survivors: list[dict] = []
    for t in pending:
        verdict, reason, prof = prefilter_trend(t, now=now)
        t["_profile"] = prof
        if verdict == "skip":
            _DROPS["prefilter_skipped"] += 1
            counts["prefiltered"] += 1
            await _save(int(t["id"]), skip_score(t, reason or "no_evidence", prof))
        else:
            survivors.append(t)

    # --- stage 1: the labels already in hand ------------------------------
    kept: list[dict] = []
    for t in survivors:
        tri = by_id.get(int(t["id"])) or {}
        t["_triage"] = tri
        verdict, reason = triage_verdict(tri)
        if verdict == "skip":
            _DROPS["triage_skipped"] += 1
            counts["triaged_out"] += 1
            await _save(int(t["id"]), triage_skip_score(t, reason or "not_a_trend",
                                                        tri, t["_profile"]))
        else:
            kept.append(t)
    await _tick()
    log.info("[general-trends] %d/%d trend(s) reach the fit pass (%d pre-filtered, "
             "%d triaged out)", len(kept), total, counts["prefiltered"],
             counts["triaged_out"])

    # --- stage 2: fit -----------------------------------------------------
    brand_note = await _brand_note() if kept else ""
    for start in range(0, len(kept), SCORE_BATCH_SIZE):
        batch = kept[start:start + SCORE_BATCH_SIZE]
        scores = await score_batch(batch, brand_note)   # ScoringUnavailable propagates
        for trend_id, score in scores.items():
            _DROPS["scored"] += 1
            counts["scored"] += 1
            await _save(trend_id, score)
        await _tick()

    log.info("[general-trends] scored %d/%d trend(s)", counts["scored"], total)
    return counts


async def rescore(ids: Optional[list[int]] = None) -> int:
    """Clear scores and run the pass again (brand changed, prompt tuned…).

    Returns the number scored — the router puts it straight in its reply. The
    trends being re-scored carry no fresh stage-1 label (that pass runs over
    raw items during a refresh), so they are judged on the corroboration line
    with the caps applied: exactly the documented no-label path."""
    cleared = await store.clear_scores(ids)
    log.info("[general-trends] cleared %d score(s) for re-scoring", cleared)
    return int((await score_pending()).get("scored") or 0)


# --------------------------------------------------------------------------- #
# The refresh job
# --------------------------------------------------------------------------- #

def _watch_accounts() -> list[str]:
    """The Instagram handles to watch. Config defaults to empty and
    ``build_fetch_plan`` emits ZERO units for an empty list, so without this
    fallback the watch lane would stay dark even after the token is refreshed.
    config.py cannot import services; the pipeline can."""
    configured = [a for a in (config.general_trends_ig_watch_accounts or []) if a]
    if configured:
        return list(configured)
    try:
        from . import general_trends_instagram as ig
        return list(ig.DEFAULT_WATCH_ACCOUNTS)
    except Exception:  # noqa: BLE001 — module absent ⇒ the lane is simply off
        return []


def _fetch_plan() -> list[dict]:
    return gts.build_fetch_plan(
        sources=list(config.general_trends_sources),
        geos=list(config.general_trends_geos),
        subreddits=list(config.general_trends_subreddits),
        youtube_api_key=config.youtube_api_key,
        tiktok_regions=list(config.general_trends_tiktok_regions),
        shorts_regions=list(config.general_trends_shorts_regions),
        ig_hashtags=list(config.general_trends_ig_hashtags),
        ig_watch_accounts=_watch_accounts(),
    )


def build_screen(*, now: datetime) -> gts.ScreenFn:
    """The ONE ingest screen handed to ``fetch_all``: safety, then freshness.

    Safety runs first because it is cheaper and harder — a drop for content is
    a drop whatever the timestamp says, and a "flag" from safety must survive
    the freshness check (a flagged item is KEPT, stored for audit, and never
    sent to the model). Freshness only ever returns keep/drop, so the verdict
    it cannot change is the one safety already made.

    Never raises: ``fetch_all`` keeps an item whose screen blew up, and this
    function must not be the reason a source disappears."""
    mode = str(config.general_trends_safety or "on")
    allow = frozenset(config.general_trends_allow_terms or ())
    max_age = int(config.general_trends_max_content_age_days or 0)
    drop_undated = bool(config.general_trends_drop_undated)

    def _screen(item: dict) -> tuple[str, str]:
        verdict, reason = safety.screen(item, mode=mode, allow_terms=allow)
        if verdict == "drop":
            return "drop", reason
        stale, why = freshness.screen(item, now=now, max_age_days=max_age,
                                      drop_undated=drop_undated)
        if stale == "drop":
            return "drop", why
        # Shape last: it is the cheapest of the three but the least important,
        # and running it last keeps the `dropped_by` counters readable — an
        # item that is BOTH explicit and a bare token is reported as explicit.
        shape, why = gts.screen_shape(item)
        if shape == "drop":
            return "drop", why
        return verdict, reason

    return _screen


def _reset_run_state() -> None:
    """Per-run state that lives at module level in the source modules: the
    Instagram circuit breaker (a token that died mid-run must not stay dead for
    the life of the process) and the freshness module's once-per-source undated
    warning."""
    try:
        from . import general_trends_instagram as ig
        ig.reset_circuit()
    except Exception:  # noqa: BLE001 — the module is optional
        pass
    try:
        freshness.reset_undated_warnings()
    except Exception:  # noqa: BLE001
        pass


def _drop_totals(stats: dict) -> tuple[int, dict[str, int]]:
    """``(total dropped, {reason: n})`` across every source of one run."""
    total = 0
    by_reason: dict[str, int] = {}
    for st in (stats or {}).values():
        if not isinstance(st, dict):
            continue
        try:
            total += int(st.get("dropped") or 0)
        except (TypeError, ValueError):
            pass
        for reason, n in (st.get("dropped_by") or {}).items():
            try:
                by_reason[str(reason)] = by_reason.get(str(reason), 0) + int(n)
            except (TypeError, ValueError):
                continue
    return total, by_reason


async def run_refresh(run_id: int, *, metadata: Optional[dict] = None) -> None:
    """Background job: fetch → cluster → velocity → score. Owns the run's
    terminal status and never raises.

    ``metadata`` is the run's existing metadata (the poller passes it from the
    queued row so ``trigger`` survives); when omitted it is read back from
    ``latest_run()`` if that is this run."""
    meta: dict = dict(metadata) if isinstance(metadata, dict) else {}
    if metadata is None:
        try:
            latest = await store.latest_run()
            if latest and int(latest.get("id") or 0) == int(run_id):
                meta = dict(latest.get("metadata") or {})
        except Exception:  # noqa: BLE001
            meta = {}
    seen_at = datetime.now(timezone.utc)

    # Liveness for the silent stretches (Reddit pacing, clustering round-trips):
    # progress writes stamp a heartbeat, but nothing is written while fetch_all
    # waits on a rate-limit window, and 30 min without one reads as "stale".
    beat = asyncio.create_task(_heartbeat_loop(run_id), name=f"general-trends-heartbeat-{run_id}")
    try:
        # --- fetch --------------------------------------------------------
        plan = _fetch_plan()
        source_ids = list(dict.fromkeys(u["source"] for u in plan))
        await store.update_run(
            run_id, status="fetching", phase="fetching",
            sources_total=len(source_ids), metadata=meta,
            progress=f"reading {len(source_ids)} source(s)…",
        )
        _reset_run_state()
        items, stats = await gts.fetch_all(
            plan, max_items_per_unit=config.general_trends_max_items_per_source,
            caps=dict(config.general_trends_source_caps or {}),
            screen=build_screen(now=seen_at),
        )
        meta["sources"] = stats
        dropped, dropped_by = _drop_totals(stats)
        meta["dropped"] = dropped
        meta["dropped_by"] = dropped_by
        sources_ok = sum(1 for s in stats.values() if s.get("ok"))
        # The owner has to be able to see WHY the feed shrank without reading a
        # log: "filtered at ingest" is the difference between a fix and a bug.
        fetched = f"{len(items)} item(s) from {sources_ok}/{len(source_ids)} source(s)"
        if dropped:
            fetched += f" · {dropped} filtered at ingest"
        await store.update_run(
            run_id, sources_done=len(stats), items_found=len(items), metadata=meta,
            progress=fetched,
        )

        # --- label (stage 1: one cheap pass, two consumers) ----------------
        # Deliberately still the "clustering" STATUS: store.RUN_STATUSES is a
        # closed set shared with the UI, and a new status would need a change
        # in a file this module does not own. The phase and the progress line
        # carry the detail instead.
        await store.update_run(
            run_id, status="clustering", phase="labelling",
            progress=f"working out what {len(items)} item(s) actually are…",
        )
        labels, label_status = await label_items(items)
        meta["labels"] = label_status

        # --- cluster ------------------------------------------------------
        await store.update_run(
            run_id, phase="clustering", metadata=meta,
            progress=f"clustering {len(items)} item(s) into trends…",
        )
        clustered = await cluster_items(items, run_id=run_id, embed=embed_texts,
                                        seen_at=seen_at, labels=labels)
        meta["embeddings"] = clustered["embeddings"]
        meta["not_minted"] = clustered["not_minted"]
        await store.update_run(
            run_id, trends_new=clustered["new"], trends_touched=clustered["touched"],
            metadata=meta,
            progress=f"{len(clustered['trend_ids'])} trend(s) · {clustered['new']} new",
        )
        try:
            await download_thumbs(clustered["image_urls"])
        except Exception:  # noqa: BLE001 — cosmetic
            log.exception("[general-trends] thumbnail step failed")

        # --- velocity -----------------------------------------------------
        await store.update_run(run_id, progress="working out what's hot…")
        try:
            await refresh_velocities(datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001 — stale heat beats a failed run
            log.exception("[general-trends] velocity refresh failed")

        # --- score --------------------------------------------------------
        await store.update_run(
            run_id, status="scoring", phase="scoring",
            progress="working out which of these 42 could actually ride…",
        )

        async def _progress(done: int, total: int) -> None:
            await store.update_run(
                run_id, trends_scored=done,
                progress=f"scoring {done}/{total} trend(s)…",
            )

        base = (f"{len(clustered['trend_ids'])} trends from {sources_ok} sources · "
                f"{clustered['new']} new")
        if plan and not sources_ok:
            base += " · no source could be reached"
        try:
            counts = await score_pending(labels=clustered["labels"],
                                         on_progress=_progress)
            scored = int(counts.get("scored") or 0)
            # Why the feed is smaller than the item count, on the run card.
            filtered = int(counts.get("prefiltered") or 0)
            not_usable = int(counts.get("triaged_out") or 0)
            note = base
            if filtered:
                note += f" · {filtered} filtered"
            if not_usable:
                note += f" · {not_usable} not usable"
        except ScoringUnavailable as exc:
            # The AI is a layer on top, not a dependency: trends still rank on
            # heat, so this is a DONE run with a caveat rather than a failure.
            scored = 0
            note = f"{base}. {exc}"
            log.warning("[general-trends] run #%d: %s", run_id, exc)

        await store.update_run(
            run_id, status="done", phase="done", trends_scored=scored,
            metadata=meta, progress=note,
        )
        log.info("[general-trends] run #%d complete: %s (scored %d)", run_id, note, scored)
    except Exception as exc:  # noqa: BLE001 — a run must always terminate
        log.exception("[general-trends] run #%d failed", run_id)
        try:
            await store.update_run(
                run_id, status="failed", phase="failed", error=str(exc)[:500],
                metadata=meta, progress="the refresh failed",
            )
        except Exception:  # noqa: BLE001
            log.exception("[general-trends] could not mark run #%d failed", run_id)
    finally:
        beat.cancel()
        try:
            await beat
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


HEARTBEAT_SEC = 60


async def _heartbeat_loop(run_id: int, *, interval: float = HEARTBEAT_SEC) -> None:
    """Every ``interval`` seconds, stamp the run's heartbeat. Never raises out
    (a failed stamp is logged and retried next tick); ends on cancel."""
    while True:
        await asyncio.sleep(interval)
        try:
            await store.heartbeat_run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("[general-trends] heartbeat for run #%d failed", run_id, exc_info=True)


async def start_refresh(*, trigger: str = "manual") -> dict:
    """QUEUE a refresh and return the run row immediately; the poller runs it
    (so it survives a restart). One at a time.

    Raises `RefreshPaused` while the stream is switched off. The guard is here,
    not only in the router, because this is the one function every caller goes
    through — the HTTP endpoint, a cron, a REPL — and queueing a run that the
    (also stopped) poller can never claim would leave a row stuck `pending` and
    the UI waiting on a refresh that is not coming."""
    if not config.general_trends_enabled:
        raise RefreshPaused(
            "Viral Social Trends collection is paused — set GENERAL_TRENDS_ENABLED=true "
            "and restart the backend to start collecting again"
        )
    await store.ensure_tables()
    if await store.run_is_active():
        existing = await store.latest_run() or {}
        raise RefreshBusy(
            "a refresh is already running "
            f"({existing.get('progress') or existing.get('status') or 'queued'})"
        )
    return await store.create_run(metadata={"trigger": trigger})


if __name__ == "__main__":  # pragma: no cover — manual end-to-end run
    import asyncio
    import sys

    # psycopg async + Windows: ProactorEventLoop is incompatible. Force Selector
    # BEFORE any asyncio code creates an event loop.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Trend titles are full of non-cp1252 characters; don't let a Windows
    # console encoding blow up the summary print after the run has finished.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    from ..db import close_pool, open_pool

    async def _main() -> None:
        await open_pool()
        try:
            run = await start_refresh(trigger="manual")
            # Claim it exactly like the poller does: if a live backend's poller
            # shares this DB it may grab the queued row first, and two processes
            # must never run the same refresh (sightings would double-count).
            if not await store.claim_run(int(run["id"])):
                print(f"run #{run['id']} was picked up by the backend poller — "
                      "watch it in the UI instead")
                return
            print(f"run #{run['id']} queued — running now")
            await run_refresh(int(run["id"]), metadata=run.get("metadata"))
            latest = await store.latest_run() or {}
            print("\nRUN:")
            for k in ("id", "status", "phase", "progress", "sources_total", "sources_done",
                      "items_found", "trends_new", "trends_touched", "trends_scored",
                      "error", "started_at", "finished_at"):
                print(f"  {k}: {latest.get(k)}")
            print("  sources:")
            for sid, st in ((latest.get("metadata") or {}).get("sources") or {}).items():
                print(f"    {sid}: {st}")
            print(f"  embeddings: {(latest.get('metadata') or {}).get('embeddings')}")
            rows = ranking.rank_trends(await store.query_trends(include_sensitive=True))
            print("\nTOP 10 TRENDS:")
            for t in rows[:10]:
                print(f"  #{t['id']:<5} {ranking.heat_band(t.get('velocity')):<7} "
                      f"v={t.get('velocity')}  fit={t.get('fit_score')}  "
                      f"uae={t.get('uae_relevance')}  opp={t.get('opportunity_score')}  "
                      f"{t.get('title')!r}  [{', '.join(t.get('sources') or [])}]")
        finally:
            await close_pool()

    asyncio.run(_main())
