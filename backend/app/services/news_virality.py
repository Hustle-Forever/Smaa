"""Industry Signal - viral potential for the stories found before it existed.

The judge scores every story it keeps for how likely a post about it would
travel (`viral_potential`, 0-100), and the feed's news channel is ordered by
that number. The stories already in the table were judged before the number
existed, and an order that means something for half the cards is worse than
no order. So the housekeeping tick scores them, a batch at a time, with the
same rules the judge reads, until none are left. Once they are done this
costs one cheap query per tick and no model call.
"""
from __future__ import annotations

import logging

from . import news_search
from . import trend_store as store

log = logging.getLogger("archivist.news_virality")

# One model call per tick, over this many stories. Fifteen keeps the reply
# well inside its budget even when the model thinks at length; the tick is
# every five minutes, so a backlog of fifty is gone in four ticks.
BATCH = 15


async def backfill(limit: int = BATCH) -> int:
    """Score the stories that have no viral potential yet. Returns how many."""
    try:
        rows = await store.news_without_virality(limit=limit)
    except Exception:  # noqa: BLE001
        log.exception("[news] could not list stories without a viral score")
        return 0
    if not rows:
        return 0

    items = []
    for r in rows:
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        items.append({
            "headline": str(r.get("caption") or ""),
            "why": str(meta.get("why_it_matters") or ""),
        })

    try:
        rated = await news_search.rate_virality(items)
    except news_search.SearchUnavailable as exc:
        log.warning("[news] viral scoring unavailable: %s", exc)
        return 0

    done = 0
    for i, r in enumerate(rows):
        got = rated.get(i)
        if got is None:
            continue
        try:
            await store.set_virality(int(r["id"]), got[0], got[1])
            done += 1
        except Exception:  # noqa: BLE001 - one row is not the batch
            log.exception("[news] could not record the viral score for post %s",
                          r.get("id"))
    if done:
        log.info("[news] viral potential scored for %d older stor%s", done,
                 "y" if done == 1 else "ies")
    return done
