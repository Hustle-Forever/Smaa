"""General Trends — the pure ranking maths (heat, opportunity, sorting).

Nothing in here touches the network, the database or the model — it is the
piece the tests drive directly and the piece the API re-runs on every request
so the weight sliders can re-rank instantly.

WHY THIS WAS REWRITTEN
----------------------
The previous velocity was

    0.40*source_score + 0.30*recency + 0.20*growth + 0.10*newness

and it collapsed, measured on the live corpus (363 trends, one run):

  * ``source_score = min(source_count/3, 1)`` — 358/363 rows had
    ``source_count = 1``, so it was the constant 0.3333: **40% of the weight,
    0.0000 population sd**.
  * ``recency = exp(-hours_since_last_seen/48)`` — ``last_seen_at`` is stamped
    from the run's single ``seen_at``, so it is a PER-RUN CONSTANT by
    construction: 30% of the weight, two reachable values, sd 0.0021.
  * ``newness`` — everything was first seen in the same run: constant 1.0,
    10% of the weight, sd 0.0000.
  * ``growth`` — ``log1p(last24)/log1p(prev24+1)/2`` over *our own fetch
    counts*, 3 reachable values, carrying ~99% of what little variance there
    was.

i.e. ``velocity = 0.532 + 0.20*growth`` → **4 distinct values over 363 rows**,
IQR 0.10, and a number that could never overturn a fit gap wider than 10.8
points. The feed was therefore sorted by the LLM's opinion alone.

THE REPLACEMENT — every term varies WITHIN a single run
-------------------------------------------------------
Nothing below is a function of ``seen_at``. The inputs are per-item metrics,
per-item ranks, vendor-supplied deltas and item CONTENT time.

    A  amplitude    per item: its metric against that source's own median in
                    the same run, log-compressed and confidence-damped (the
                    house pattern from the competitor finder). Rank-only
                    sources use a geometric rank curve. Trend level:
                    0.7*max + 0.3*mean.
    G  growth       scale-free run-over-run / day-over-day ratio, best-of over
                    the sources that actually have one. May be absent.
    F  freshness    exp(-age_h/48) from CONTENT time (latest_content_at /
                    published_at), never from fetch time.
    T  trust        corroboration multiplier, 0.55 (one source) → 1.0 (three).

    heat = T * (Σ W[k]·v_k over PRESENT k) / (Σ W[k] over PRESENT k)
    W = {amplitude .45, growth .30, freshness .25}

Renormalising over the signals that exist is mandatory: scoring a missing G as
0 would cost 30% of every trend's heat on run #1 and re-create the flat feed.
It is the same fix already documented in ``trend_scoring.compute_opportunity``
("a missing signal must not be scored as zero").

Two numbers per trend, same names and same DB columns as before:

  * **velocity** (0..1) — the heat above. Recomputed by the pipeline every run
    and stored on the row. The public function is still ``compute_velocity``
    and the column is still ``velocity``; only the maths changed.
  * **opportunity_score** (0..100) — now a weighted GEOMETRIC mean of heat,
    fit-for-42 and UAE relevance, times a sensitivity multiplier. Additive
    blending is what let ``fit=90, uae=100`` float a dead, uncorroborated
    trend above a genuinely cross-corroborated meme; a product cannot be
    bought back on one axis.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# Opportunity weights. Heat and fit matter most; UAE relevance is the tie-breaker
# that keeps the feed locally grounded. Keys are what the API's w_vel/w_fit/w_uae
# map onto — unchanged. They are now geometric-mean EXPONENTS rather than
# additive weights, which keeps the slider contract identical.
DEFAULT_WEIGHTS = {"velocity": 0.4, "fit": 0.4, "uae": 0.2}

SORTS = ("opportunity", "velocity", "fit", "recent", "sources")

# --------------------------------------------------------------------------- #
# Calibration constants. Each one records what it is calibrated against.
# --------------------------------------------------------------------------- #

# Heat component weights. Amplitude leads because it is the only term with real
# spread on a cold start; growth is what the owner actually asked for but is the
# noisiest and needs two runs (or a vendor delta); freshness is mostly a gate —
# the 30-day hard cut in the pipeline does the heavy lifting.
HEAT_WEIGHTS = {"amplitude": 0.45, "growth": 0.30, "freshness": 0.25}

# Amplitude. AMP_CEILING is trend_scoring.OUTPERFORMANCE_CEILING verbatim: a
# linear cap flattens the top of the feed (44x, 34x and 22x all tie), a log
# curve spreads the 1x-5x band where real items live.
AMP_CEILING = 8.0
# Sibling of trend_finder.MIN_POSTS_FOR_BASELINE = 6. Below this many items a
# source's median is not a baseline, so the ratio is damped and capped.
MIN_ITEMS_FOR_MEDIAN = 8
THIN_SAMPLE_MAX_CLAIM = 3.0
# Rank-only feeds (reddit /rising, kym, tiktok Creative Center — anything with
# no metric) get a geometric curve from 2.0 down to 0.5. This single pair of
# constants IS the "how much do we trust an unmetered source" policy: the #1
# item of such a feed is worth exactly a 2x-median metric item (amp 0.500) and
# the last one is worth a half-median item (amp 0.185).
RANK_TOP_RATIO = 2.0
RANK_BOTTOM_RATIO = 0.5
# Reddit /rising and the trends24 lists both run ~40 long; used when the parser
# did not record how long the list was.
RANK_FANOUT_DEFAULT = 40
# No metric AND no rank AND no baseline (e.g. a caller that has not been updated
# to pass `source_stats`): a neutral, deliberately un-flooring 0.5. It sits
# exactly on SOLO_AMP_MIN so the legacy path never trips the solo cap.
NEUTRAL_AMPLITUDE = 0.5
# Trend-level blend. Max alone reproduces the old one-outlier failure; mean
# alone punishes a trend that is genuinely huge on one platform and absent
# elsewhere.
AMP_MAX_SHARE = 0.7

# Growth. g = metric_now / metric_prev. GROWTH_CEILING 4.0 means a 4x jump
# between comparable observations is full marks; g=1 (flat) → 0, g=2 → exactly
# 0.500, shrinking → 0. Scale-free, unlike the old ratio-of-logarithms which
# scored the same 2x move 0.73 or 0.59 depending on absolute magnitude.
GROWTH_CEILING = 4.0
# Below this gap a run-over-run delta is fetch noise, not growth.
MIN_GAP_HOURS = 3.0

# Freshness. exp(-age_h/48): 6h → 0.882, 24h → 0.607, 48h → 0.368, 7d → 0.018.
FRESH_TAU_HOURS = 48.0

# Corroboration. Multiplicative, so no amount of fit can buy back corroboration
# a trend does not have. S=1 → 0.550, S=2 → 0.834, S>=3 → 1.000.
TRUST_MIN = 0.55
TRUST_SAT = 3.0
# A 2nd subreddit / 2nd geo is worth a quarter of a 2nd platform.
UNIT_CREDIT = 0.25

# The "Digital Twins in City Planning — one Reddit post, ranked #4 in the feed"
# floor. SOLO_AMP_MIN is not arbitrary: 0.50 is exactly the amplitude of the #1
# item of a rank-only feed and of a 2x-median metric item. A single
# uncorroborated item must be either the top of its feed or >=2x its source's
# median to compete at all. SOLO_HEAT_CAP sits below the "rising" band (0.45)
# so such a row can never carry a hot/rising pill.
SOLO_AMP_MIN = 0.50
SOLO_HEAT_CAP = 0.35

# Opportunity. Nothing collapses to exactly zero — a zero factor would make the
# whole product zero and leave rows unorderable.
OPPORTUNITY_FLOOR = 0.05
# 'avoid' rows are filtered before the model now; the multiplier only matters
# for legacy rows already stored (and those are hidden by query_trends anyway).
SENSITIVITY_MULTIPLIERS = {"ok": 1.0, "caution": 0.75, "avoid": 0.5}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _to_dt(value: Any) -> Optional[datetime]:
    """datetime (aware or naive) or ISO string -> tz-aware UTC datetime; None
    for anything unusable. Naive datetimes are taken as UTC."""
    if value is None or value == "":
        return None
    dt: Optional[datetime]
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _clamp01(value: Any) -> float:
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v):
        return 0.0
    return min(max(v, 0.0), 1.0)


def _clamp100(value: Any) -> float:
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v):
        return 0.0
    return min(max(v, 0.0), 100.0)


def _num(value: Any) -> Optional[float]:
    """float(value) or None — never raises, and rejects NaN/inf/bools."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _extra(sighting: dict) -> dict:
    """The sighting's ``extra`` JSONB as a dict. psycopg hands back a dict, but
    a hand-rolled fixture or a raw text column may hand back a JSON string."""
    raw = sighting.get("extra")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _median(values: list[float]) -> float:
    """Median of a list (0.0 for empty). Median rather than mean because every
    one of these distributions is a power law — one `1M+` Google entry would
    drag a mean far above anything typical."""
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def _dicts(sightings: Optional[Iterable[dict]]) -> list[dict]:
    return [s for s in (sightings or []) if isinstance(s, dict)]


# --------------------------------------------------------------------------- #
# Component maths — one-line pure functions so the tests pin the curve rather
# than the blend.
# --------------------------------------------------------------------------- #

def trusted_ratio(raw: Any, confidence: Any) -> float:
    """Damp and cap a raw ratio by how much baseline we actually have.

    Copied (maths, not import) from
    ``backend/app/services/trend_finder.py::_trusted_outperformance`` — the
    competitor finder's calibrated version. `confidence` is 1.0 once a source
    has enough items in the run to have a real median. Below that the ratio is
    shrunk toward 1.0 (no evidence of anything unusual) AND capped, because
    shrinking alone doesn't help: a 100x from three items is still 50x after
    halving.

    FOLLOW-UP: this duplicates trend_finder; the de-duplication the plan wants
    is one public ``trusted_ratio`` in ``trend_scoring.py`` that both import."""
    conf = max(0.0, min(_num(confidence) or 0.0, 1.0))
    r = _num(raw)
    if r is None:
        return 1.0
    damped = 1.0 + (r - 1.0) * conf
    if conf >= 1.0:
        return round(damped, 3)
    ceiling = 1.0 + conf * (THIN_SAMPLE_MAX_CLAIM - 1.0)
    return round(min(damped, ceiling), 3)


def normalise_amplitude(ratio: Any) -> float:
    """Ratio-against-the-source's-own-median → 0..1 on a log curve.

    Anchors: ratio 0.25 → 0.102, ratio 1 (a typical item on its own source) →
    0.3155, ratio 2 → 0.500, ratio 8 → 1.0 (saturated)."""
    r = max(_num(ratio) or 0.0, 0.0)
    return min(math.log1p(r) / math.log1p(AMP_CEILING), 1.0)


def ratio_from_rank(rank: Any, n: Any = None) -> Optional[float]:
    """Rank-only feed position → the equivalent metric ratio.

    Geometric from RANK_TOP_RATIO (2.0) at #1 down to RANK_BOTTOM_RATIO (0.5)
    at #N: ``2.0 * 0.25 ** ((rank-1)/(N-1))``. A one-item feed is its own #1."""
    r = _num(rank)
    if r is None or r < 1:
        return None
    total = _num(n)
    size = int(total) if total is not None and total >= 1 else RANK_FANOUT_DEFAULT
    size = max(size, int(r))
    if size <= 1:
        return RANK_TOP_RATIO
    span = min(max((r - 1.0) / (size - 1.0), 0.0), 1.0)
    return RANK_TOP_RATIO * (RANK_BOTTOM_RATIO / RANK_TOP_RATIO) ** span


def normalise_growth(g: Any) -> float:
    """Growth ratio (now/prev) → 0..1, scale-free.

    ``min(log1p(max(g-1, 0)) / log1p(GROWTH_CEILING-1), 1.0)``: g=1 (flat) → 0,
    g=2 (doubled) → exactly 0.500, g=4 → 1.0, shrinking → 0. 50→100 and 2→4
    both score 0.500, which the old formula could not do."""
    ratio = _num(g)
    if ratio is None:
        return 0.0
    return min(math.log1p(max(ratio - 1.0, 0.0)) / math.log1p(GROWTH_CEILING - 1.0), 1.0)


def freshness(age_hours: Any) -> float:
    """exp(-age/48) over the age of the CONTENT, not of our fetch."""
    age = _num(age_hours)
    if age is None:
        return 0.0
    return math.exp(-max(age, 0.0) / FRESH_TAU_HOURS)


def corroboration_trust(sources: Any, units: Any = None) -> float:
    """Multiplicative 0.55..1.0 from how many independent places carry this.

    ``S_eff = S + 0.25*(units - S)`` then
    ``TRUST_MIN + (1-TRUST_MIN)*min(log1p(S_eff-1)/log1p(2.0), 1)``.
    S=1 → 0.550, S=2 → 0.834, S=3+ → 1.000. A second subreddit or a second geo
    (a "unit") is worth a quarter of a second platform."""
    s = max(int(_num(sources) or 0), 0)
    if s <= 0:
        return TRUST_MIN
    u = max(int(_num(units) or 0), s)
    s_eff = s + UNIT_CREDIT * (u - s)
    span = min(math.log1p(max(s_eff - 1.0, 0.0)) / math.log1p(TRUST_SAT - 1.0), 1.0)
    return TRUST_MIN + (1.0 - TRUST_MIN) * span


def blend_heat(
    *, amplitude: Optional[float], growth: Optional[float],
    fresh: Optional[float], trust: float, solo: bool = False,
) -> float:
    """``T * (Σ W[k]·v_k over present k) / (Σ W[k] over present k)``.

    `solo` applies SOLO_HEAT_CAP: a single item from a single source that is
    not outstanding on its own feed can never reach the "rising" band."""
    parts = [
        (k, v) for k, v in (
            ("amplitude", amplitude), ("growth", growth), ("freshness", fresh),
        ) if v is not None
    ]
    if not parts:
        return 0.0
    weight = sum(HEAT_WEIGHTS[k] for k, _ in parts)
    inner = sum(HEAT_WEIGHTS[k] * _clamp01(v) for k, v in parts) / weight
    heat = min(max(trust, 0.0), 1.0) * inner
    if solo:
        heat = min(heat, SOLO_HEAT_CAP)
    return min(max(heat, 0.0), 1.0)


# --------------------------------------------------------------------------- #
# Run-level baselines
# --------------------------------------------------------------------------- #

def source_stats(sightings: Iterable[dict]) -> dict[str, dict]:
    """Per-source baselines for ONE run: ``{source: {median, n, items}}``.

    * ``median`` — median ``metric_value`` over that source's items in the run,
      counting only positive metrics (0.0 when the source has none — a
      rank-only feed like reddit or kym).
    * ``n`` — how many of those positive-metric items there were; the
      confidence denominator for :func:`trusted_ratio`.
    * ``items`` — every item the source contributed, metric or not. Additive
      extra, used as the rank-curve fanout N when the parser did not record
      how long the list was.

    The pipeline builds this once per run — in SQL that is
    ``percentile_cont(0.5) WITHIN GROUP (ORDER BY metric_value) ... WHERE
    run_id = %s AND metric_value > 0 GROUP BY source`` plus a plain
    ``count(*) GROUP BY source`` for ``items`` — and passes the same dict to
    every :func:`compute_velocity` call in that run."""
    metrics: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for s in _dicts(sightings):
        src = s.get("source")
        if not src:
            continue
        src = str(src)
        counts[src] = counts.get(src, 0) + 1
        value = _num(s.get("metric_value"))
        if value is not None and value > 0:
            metrics.setdefault(src, []).append(value)
    out: dict[str, dict] = {}
    for src, total in counts.items():
        vals = metrics.get(src, [])
        out[src] = {"median": _median(vals), "n": len(vals), "items": int(total)}
    return out


# --------------------------------------------------------------------------- #
# Per-sighting signal extraction
# --------------------------------------------------------------------------- #

def _item_amplitude(sighting: dict, stats: Optional[dict]) -> Optional[float]:
    """0..1 amplitude for one sighting, or None when nothing supports one.

    Metric path first (real magnitude against the source's own run median),
    rank path second (position on an unmetered feed). A rank needs no run
    baseline, so it survives ``source_stats=None``."""
    src = str(sighting.get("source") or "")
    extra = _extra(sighting)
    per_source = (stats or {}).get(src) if isinstance(stats, dict) else None
    if not isinstance(per_source, dict):
        per_source = None

    metric = _num(sighting.get("metric_value"))
    if metric is not None and metric > 0 and per_source is not None:
        median = _num(per_source.get("median")) or 0.0
        if median > 0:
            # max(median, 1.0): a source whose typical item is a fraction of a
            # unit must not turn every item into a 1000x hero.
            raw = metric / max(median, 1.0)
            n = _num(per_source.get("n")) or 0.0
            conf = min(n / MIN_ITEMS_FOR_MEDIAN, 1.0)
            return normalise_amplitude(trusted_ratio(raw, conf))

    rank = _num(extra.get("rank"))
    if rank is not None and rank >= 1:
        fanout = _num(extra.get("rank_total")) or _num(extra.get("total"))
        if fanout is None and per_source is not None:
            fanout = _num(per_source.get("items"))
        ratio = ratio_from_rank(rank, fanout)
        if ratio is not None:
            return normalise_amplitude(ratio)
    return None


def _item_growth_ratio(sighting: dict, *, now: datetime) -> Optional[float]:
    """The raw growth ratio (now/prev) this sighting supports, or None.

    Four ways, best first — all of them from payloads the parsers already keep
    and nothing reads:

      1. ``extra.growth_ratio`` — a ratio the fetcher computed itself.
      2. ``extra.prev_metric`` / ``extra.views_yesterday`` — an explicit
         previous value (wikipedia ships yesterday's views on run #1; the
         pipeline's ``LAG(metric_value)`` fills this in for everything else).
      3. ``extra.views_delta`` — the previous value implied by the delta.
      4. ``extra.prev_rank`` — rank improvement on an unmetered feed.

    "Entered the list" (no previous value, or a previous value of 0) is
    UNAVAILABLE, not infinite growth."""
    extra = _extra(sighting)

    explicit = _num(extra.get("growth_ratio"))
    if explicit is not None and explicit > 0:
        return explicit

    # A previous observation younger than MIN_GAP_HOURS is fetch noise, not
    # growth — this is what stops a re-poll 20 minutes later inventing a spike.
    prev_at = _to_dt(extra.get("prev_seen_at") or extra.get("prev_metric_at"))
    if prev_at is not None and (now - prev_at).total_seconds() / 3600.0 < MIN_GAP_HOURS:
        return None

    metric = _num(sighting.get("metric_value"))
    if metric is not None and metric >= 0:
        prev = _num(extra.get("prev_metric"))
        if prev is None:
            prev = _num(extra.get("prev_metric_value"))
        if prev is None:
            prev = _num(extra.get("views_yesterday"))
        if prev is None:
            delta = _num(extra.get("views_delta"))
            if delta is not None:
                prev = metric - delta
        if prev is not None and prev > 0:
            return metric / prev

    rank = _num(extra.get("rank"))
    prev_rank = _num(extra.get("prev_rank"))
    if rank is not None and prev_rank is not None and rank >= 1 and prev_rank >= 1:
        fanout = _num(extra.get("rank_total")) or _num(extra.get("total")) or RANK_FANOUT_DEFAULT
        fanout = max(fanout, rank, prev_rank)
        return (fanout - rank + 1.0) / (fanout - prev_rank + 1.0)
    return None


def _item_content_time(sighting: dict) -> Optional[datetime]:
    """When the CONTENT happened, per sighting. ``published_at`` is written by
    the parsers today and read by nothing; the extras are the vendor-specific
    equivalents (bluesky ``startedAt``, wikipedia ``day``)."""
    dt = _to_dt(sighting.get("published_at"))
    if dt is not None:
        return dt
    extra = _extra(sighting)
    for key in ("published_at", "startedAt", "started_at", "day", "created_at"):
        dt = _to_dt(extra.get(key))
        if dt is not None:
            return dt
    return None


def _unit_key(sighting: dict) -> tuple[str, str]:
    """(source, unit) — the "independent place" a sighting came from. A second
    subreddit or a second geo is a unit; a second platform is a source."""
    extra = _extra(sighting)
    unit = (extra.get("sub") or extra.get("geo") or sighting.get("region")
            or sighting.get("source_key") or "")
    return (str(sighting.get("source") or ""), str(unit))


def _source_count(trend: dict, sightings: Iterable[dict]) -> int:
    raw = trend.get("source_count")
    if raw is not None:
        try:
            n = int(raw)
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    sources = trend.get("sources")
    if isinstance(sources, (list, tuple, set)) and sources:
        return len({str(s) for s in sources if s})
    seen = {str(s.get("source")) for s in sightings if isinstance(s, dict) and s.get("source")}
    return len(seen)


def _evidence_count(trend: dict, sightings: list[dict]) -> int:
    """Distinct (source, source_key) — how many *different items* back this
    trend. NEVER ``sighting_count``, which is a poll counter: it is bumped on
    every re-poll, so an evergreen Wikipedia page shows 9 "sightings" from one
    single source."""
    raw = _num(trend.get("evidence_count"))
    if raw is not None and raw > 0:
        return int(raw)
    keys = {(str(s.get("source") or ""), str(s.get("source_key") or "")) for s in sightings}
    return len(keys)


# --------------------------------------------------------------------------- #
# Heat
# --------------------------------------------------------------------------- #

def heat_signals(
    trend: dict, sightings: list[dict], *, now: datetime,
    source_stats: Optional[dict] = None,
) -> dict:
    """Every heat component for one trend, as plain JSON-able numbers.

    The pipeline persists this on ``general_trends.metadata.signals`` so the
    feed can explain a score ("4x the typical Google entry, +150% day over
    day") without recomputing anything.

    Keys: ``amplitude`` / ``growth`` / ``freshness`` (0..1, or None when the
    signal does not exist for this trend), ``trust``, ``sources``, ``units``,
    ``evidence``, ``age_hours``, ``content_at`` (ISO or None), ``capped`` and
    ``heat``."""
    now_dt = _to_dt(now) or datetime.now(timezone.utc)
    items = _dicts(sightings)
    stats = source_stats if isinstance(source_stats, dict) else None

    # --- amplitude ---------------------------------------------------------
    amps = [a for a in (_item_amplitude(s, stats) for s in items) if a is not None]
    measured = bool(amps)
    if amps:
        amplitude: Optional[float] = (AMP_MAX_SHARE * max(amps)
                                      + (1.0 - AMP_MAX_SHARE) * (sum(amps) / len(amps)))
    else:
        # No metric, no rank, no baseline (or a caller that predates
        # `source_stats`): neutral, so the trend is ranked on growth and
        # freshness rather than punished for our own missing plumbing.
        amplitude = NEUTRAL_AMPLITUDE

    # --- growth ------------------------------------------------------------
    # max, not mean: one platform genuinely accelerating IS the signal.
    ratios = [g for g in (_item_growth_ratio(s, now=now_dt) for s in items) if g is not None]
    growth: Optional[float] = normalise_growth(max(ratios)) if ratios else None

    # --- freshness ---------------------------------------------------------
    content_at = _to_dt(trend.get("latest_content_at"))
    if content_at is None:
        times = [t for t in (_item_content_time(s) for s in items) if t is not None]
        content_at = max(times) if times else None
    if content_at is None:
        # Last resort only. first_seen_at equals the run stamp for a brand-new
        # trend — exactly the per-run constant this rewrite exists to remove —
        # but for a carried-over trend it is real history, and the freshness
        # gate upstream drops undated items when configured to.
        content_at = _to_dt(trend.get("first_seen_at"))
    if content_at is None:
        fresh: Optional[float] = None
        age_h: Optional[float] = None
    else:
        age_h = max((now_dt - content_at).total_seconds() / 3600.0, 0.0)
        fresh = freshness(age_h)

    # --- corroboration -----------------------------------------------------
    n_sources = _source_count(trend, items)
    units = len({_unit_key(s) for s in items}) if items else n_sources
    trust = corroboration_trust(n_sources, units)
    evidence = _evidence_count(trend, items)

    # The solo cap fires when a single uncorroborated item is not outstanding on
    # its own feed — and ALSO when its magnitude could not be measured at all
    # while baselines WERE available. That second arm matters: `amplitude` is
    # never None (it falls back to NEUTRAL_AMPLITUDE, which sits exactly ON
    # SOLO_AMP_MIN), so without it a source that publishes neither a counter nor
    # a rank would sail past the cap on a technicality — and those are precisely
    # the single-post feeds the cap exists to contain. `stats is None` means the
    # CALLER did not give us baselines, which is our missing plumbing rather
    # than the trend's missing evidence, so that path is left uncapped.
    solo = (n_sources <= 1 and evidence <= 1
            and (amplitude < SOLO_AMP_MIN or (not measured and stats is not None)))
    heat = blend_heat(amplitude=amplitude, growth=growth, fresh=fresh,
                      trust=trust, solo=solo)

    return {
        "amplitude": round(amplitude, 4) if amplitude is not None else None,
        "growth": round(growth, 4) if growth is not None else None,
        "freshness": round(fresh, 4) if fresh is not None else None,
        "trust": round(trust, 4),
        "sources": n_sources,
        "units": units,
        "evidence": evidence,
        "age_hours": round(age_h, 2) if age_h is not None else None,
        "content_at": content_at.isoformat() if content_at is not None else None,
        "capped": bool(solo),
        "heat": round(heat, 4),
    }


def compute_velocity(
    trend: dict, sightings: list[dict], *, now: datetime,
    source_stats: Optional[dict] = None,
) -> float:
    """0..1 heat for one trend — the number stored in the ``velocity`` column.

        A = 0.7*max(amp_i) + 0.3*mean(amp_i)   per-item metric vs that source's
                                               own run median, or the rank curve
        G = normalise_growth(max(g_i))         absent when nothing supports it
        F = exp(-age_h/48)                     age of the CONTENT
        T = 0.55 .. 1.0                        corroboration
        heat = T * Σ W·v / Σ W                 over the signals that are PRESENT

    `sightings` are the trend's own rows from ``general_trend_sightings``:
    ``source``, ``source_key``, ``metric_value``, ``published_at``, ``region``
    and ``extra`` (``rank``, ``rank_total``, ``prev_metric``,
    ``views_yesterday``, ``views_delta``, ``prev_rank``, ``sub``, ``geo``).
    `trend` supplies ``source_count``/``sources``, ``evidence_count`` and
    ``latest_content_at``; anything missing is derived from the sightings.

    `source_stats` is :func:`source_stats` over the whole run. Passing None
    degrades amplitude to a neutral 0.5 (rank-only items still work), so a
    caller that has not been updated keeps producing a sane number. `now` is
    only ever used to age CONTENT timestamps — never to measure when we
    fetched."""
    return float(heat_signals(trend, sightings, now=now, source_stats=source_stats)["heat"])


def heat_band(velocity: Any) -> str:
    """Bucket a velocity for the card pill: hot / rising / warm."""
    v = _clamp01(velocity)
    if v >= 0.7:
        return "hot"
    if v >= 0.45:
        return "rising"
    return "warm"


# --------------------------------------------------------------------------- #
# Opportunity
# --------------------------------------------------------------------------- #

def normalise_weights(weights: Optional[dict]) -> dict:
    """Clamp to non-negative and renormalise to sum 1, so any slider combination
    the UI sends produces a comparable 0-100 score. Garbage (strings, negatives,
    all-zero) falls back to the defaults."""
    w = dict(DEFAULT_WEIGHTS)
    for key in w:
        raw = (weights or {}).get(key) if isinstance(weights, dict) else None
        if raw is not None:
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isnan(val) or math.isinf(val):
                continue
            w[key] = max(0.0, val)
    total = sum(w.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in w.items()}


def sensitivity_multiplier(value: Any) -> float:
    """'ok' → 1.0, 'caution' → 0.75, 'avoid' → 0.5, anything else → 1.0."""
    key = str(value or "ok").strip().lower()
    return SENSITIVITY_MULTIPLIERS.get(key, 1.0)


def compute_opportunity(trend: dict, weights: Optional[dict] = None) -> float:
    """Blend heat, fit and UAE relevance into a single 0-100 score — a weighted
    GEOMETRIC mean over the factors that exist, times a sensitivity multiplier.

        opportunity = 100 * M * Π max(v_k, 0.05) ** (w_k / Σ w_present)

    Geometric because "opportunity" means heat AND fit AND local relevance.
    Under the old additive blend a shrinking, uncorroborated trend with
    fit=90/uae=100 scored 63.2 — above a genuinely cross-corroborated meme at
    fit 75. A product refuses to let one axis buy the ranking.

    Only the signals that ACTUALLY EXIST are blended, and the exponents are
    renormalised over them. This is the same partial-parse bug ``trend_scoring``
    already fixed: the old code fell back to velocity only when fit AND uae were
    both None, so a reply that dropped ``fit_score`` but kept ``uae_relevance``
    scored fit as 0 and ranked BELOW a trend that was never scored at all.
    Absent evidence is not evidence of a bad fit."""
    w = normalise_weights(weights)
    parts: list[tuple[str, float]] = [("velocity", _clamp01(trend.get("velocity")))]

    fit = trend.get("fit_score")
    if fit is not None:
        parts.append(("fit", _clamp100(fit) / 100.0))
    uae = trend.get("uae_relevance")
    if uae is not None:
        parts.append(("uae", _clamp100(uae) / 100.0))

    total_w = sum(w[k] for k, _ in parts)
    if total_w <= 0:
        # Every present factor's slider is at zero — fall back to heat alone
        # rather than returning a meaningless 100 from an empty product.
        parts = [("velocity", _clamp01(trend.get("velocity")))]
        w, total_w = dict(w, velocity=1.0), 1.0

    log_sum = sum((w[k] / total_w) * math.log(max(v, OPPORTUNITY_FLOOR)) for k, v in parts)
    score = math.exp(log_sum) * sensitivity_multiplier(trend.get("sensitivity"))
    return round(100.0 * min(max(score, 0.0), 1.0), 2)


# --------------------------------------------------------------------------- #
# Sorting
# --------------------------------------------------------------------------- #

def _fit_key(row: dict):
    fit = row.get("fit_score")
    # None last: (has_value, value)
    return (fit is not None, _clamp100(fit) if fit is not None else 0.0)


def _recent_key(row: dict):
    # CONTENT time first: `last_seen_at` is stamped once per run, so sorting by
    # it ordered the feed by nothing at all.
    dt = _to_dt(row.get("latest_content_at")) or _to_dt(row.get("last_seen_at"))
    return (dt is not None, dt or datetime.min.replace(tzinfo=timezone.utc))


def rank_trends(
    rows: list[dict], *, sort: str = "opportunity",
    weights: Optional[dict] = None,
) -> list[dict]:
    """Re-score and order the feed. Pure, so the weight sliders re-rank instantly
    without touching the database or the model. Always recomputes
    ``opportunity_score`` — the stored one used whatever weights were live at
    scoring time."""
    w = normalise_weights(weights)
    out: list[dict] = []
    for r in rows or []:
        row = dict(r)
        row["opportunity_score"] = compute_opportunity(row, w)
        out.append(row)

    if sort == "velocity":
        out.sort(key=lambda r: _clamp01(r.get("velocity")), reverse=True)
    elif sort == "fit":
        out.sort(key=_fit_key, reverse=True)
    elif sort == "recent":
        out.sort(key=_recent_key, reverse=True)
    elif sort == "sources":
        out.sort(
            key=lambda r: (int(r.get("source_count") or 0), _clamp01(r.get("velocity"))),
            reverse=True,
        )
    else:  # "opportunity" and anything unknown
        out.sort(key=lambda r: float(r.get("opportunity_score") or 0.0), reverse=True)
    return out
