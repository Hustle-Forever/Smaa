"""Industry Signal - the search, and the housekeeping around it.

NOTHING HERE IS ON A TIMER ANY MORE. This used to run itself at 7am Dubai. It
was removed by request, and the reasoning is worth keeping: a search nobody
asked for spends money on mornings nobody reads, the person who wants stories
knows when they want them, and the screen had to carry a whole sentence
explaining a cadence just to stop a quiet feed from looking broken. A person
presses a button, says how many stories they want at most, and that is it.

WHAT IS LEFT ON THE TICK IS HOUSEKEEPING, and each piece is there because it
must happen whether or not anybody presses anything:

  prune             stories past the retention window, so the table stays bounded
  fail_stale_runs   runs killed mid-flight by a restart, which otherwise sit at
                    `running` for ever and leave the button on screen disabled
                    behind a spinner that never stops
  cover backfill    a picture for stories that have none yet
  viral scoring     a viral potential for stories judged before it existed

DELIBERATELY NOT ON THE ``trend_scans`` QUEUE. That queue is serialised so
competitor scans cannot collide on the Instagram rate limit. Putting this behind
it would mean a search stops working whenever somebody is scanning competitors.

EVERY STAGE IS GUARDED SEPARATELY. The stock ``_loop`` exits permanently if
anything escapes a tick, so one transient error would otherwise stop the
housekeeping until somebody noticed and restarted the backend.
"""
from __future__ import annotations

import asyncio
import logging

from ..services import news_ingest, news_search, news_store, news_thumbs, news_virality
from ..services import trend_store as store

log = logging.getLogger("archivist.news_poller")

# Housekeeping only, so it is cheap and stays out of the way.
POLL_INTERVAL_SEC = 300

_task: asyncio.Task | None = None
# The run the button started. Held so the task is not collected while it is
# still working: a bare create_task reference is not enough.
_manual_task: asyncio.Task | None = None
# One run at a time. Two overlapping runs would pay for the same search twice
# and race each other writing the same rows.
_running = False


async def _run_once(cfg: dict, *, limit: int | None = None,
                    trigger: str = "manual") -> None:
    """One search: find, judge, write. Records a run row whatever happens.

    `limit` is the cap the person set on the button. None falls back to the
    remembered default, which is only ever a starting position for that control.
    """
    # NO BUDGET CHECK. There used to be one here: if the shared Vertex meter had
    # less than $0.50 of headroom the run was recorded `no_budget` and never
    # attempted. It was removed with the cap itself (see
    # `vertex_client.VertexCostMeter`), and this was the gate that made the
    # problem visible: the meter is one pot shared with Veo, so generating video
    # could leave the news search reporting "we could not afford to look" on a
    # day nobody had been near the news. `no_budget` stays in `RUN_STATUSES`
    # because old rows in `news_runs` still carry it.
    run_id = await news_store.start_run(trigger=trigger)

    try:
        recent = await store.recent_news_headlines(limit=news_store.RECENT_HEADLINES)
    except Exception:  # noqa: BLE001 - the model just gets no skip-list
        log.exception("[news] could not read recent headlines")
        recent = []

    # WHAT WE LOOK FOR IS THE FOLLOW LIST NOW, rendered into the same block of
    # text the search and the judge always took. `news_config.brief` is the
    # fallback and nothing else: it holds whatever the box last said, and it is
    # what the list was seeded from, so an empty list still searches for
    # something rather than asking the model to find "AI news" with no test.
    try:
        watching = await news_store.list_watch()
    except Exception:  # noqa: BLE001 - a search with the old brief beats no search
        log.exception("[news] could not read the follow list")
        watching = []
    brief = news_store.watch_text(watching) or str(cfg.get("brief") or "")
    # The websites we always check get a search of their own, next to the open
    # one, so a person who added a site sees something from it.
    sites = news_store.watch_sites(watching)
    if limit is None:
        limit = int(cfg.get("stories_per_day") or news_store.DEFAULT_STORIES)
    limit = max(news_store.MIN_STORIES, min(int(limit), news_store.MAX_STORIES))

    try:
        result = await news_search.find_stories(
            brief=brief, limit=limit, recent_headlines=recent, sites=sites,
        )
    except news_search.SearchUnavailable as exc:
        # Credentials, billing or the budget cap. True on every run until a
        # person fixes it, so it is a warning with a readable reason rather than
        # a stack trace every morning.
        log.warning("[news] search unavailable: %s", exc)
        await news_store.finish_run(run_id, status="unavailable", error=str(exc)[:500])
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("[news] run failed")
        await news_store.finish_run(run_id, status="search_failed", error=str(exc)[:500])
        return

    stories = result.get("stories") or []
    drops = dict(result.get("drops") or {})
    status = str(result.get("status") or "empty")

    if stories:
        try:
            written = await news_ingest.ingest(stories, brief=brief)
            if written["duplicate"]:
                drops["we already had it"] = (
                    drops.get("we already had it", 0) + written["duplicate"])
            if written["failed"]:
                drops["could not be saved"] = (
                    drops.get("could not be saved", 0) + written["failed"])
            log.info("[news] %d found, %d kept, %d new",
                     result.get("found", 0), len(stories), written["new"])
            status = "ok" if written["new"] else "empty"
        except Exception as exc:  # noqa: BLE001
            log.exception("[news] could not store the morning's stories")
            await news_store.finish_run(
                run_id, status="search_failed", found=result.get("found", 0),
                drops=drops, error=str(exc)[:500])
            return

    await news_store.finish_run(
        run_id, status=status, found=int(result.get("found") or 0),
        kept=len(stories), drops=drops, error=result.get("error"),
    )

    # Pictures, here, before anybody looks. The tick also runs this, but the
    # tick is every five minutes AND skips itself while a search is in flight,
    # so relying on it alone meant somebody pressed Find stories, watched the
    # cards arrive, and sat in front of blank tiles for up to five minutes.
    # Guarded separately: a run that found stories succeeded, whatever the
    # publishers do about their images.
    if status == "ok":
        try:
            await news_thumbs.backfill()
        except Exception:  # noqa: BLE001 - a missing picture is not a failed run
            log.exception("[news] covers failed after the search")


async def _tick() -> None:
    """Housekeeping. Never starts a search: only the button does that."""
    try:
        removed = await news_ingest.prune()
        if removed:
            log.info("[news] pruned %d stale stor%s", removed,
                     "y" if removed == 1 else "ies")
        await news_store.prune_runs()
    except Exception:  # noqa: BLE001
        log.exception("[news] prune failed")

    try:
        stranded = await news_store.fail_stale_runs()
        if stranded:
            log.info("[news] closed %d run(s) left open by a restart", stranded)
    except Exception:  # noqa: BLE001
        log.exception("[news] could not close stranded runs")

    # Skipped while a search is in flight: that run is about to add stories of
    # its own, and a cover pass racing its inserts would ask the publisher for
    # half of them and then be asked again on the next tick anyway.
    if _running:
        return
    try:
        await news_thumbs.backfill()
    except Exception:  # noqa: BLE001
        log.exception("[news] cover backfill raised")

    # Competitor cards get the same guarantee: a picture on every card.
    try:
        await news_thumbs.backfill_competitors()
    except Exception:  # noqa: BLE001
        log.exception("[trends] competitor cover backfill raised")

    # Viral potential for the stories judged before the score existed. One
    # model call per tick until there are none left, then nothing.
    try:
        await news_virality.backfill()
    except Exception:  # noqa: BLE001
        log.exception("[news] viral scoring raised")


async def _loop() -> None:
    log.info("news_poller started (housekeeping every %ds; searches are "
             "started by a person, never on a timer)", POLL_INTERVAL_SEC)
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop outlives any one tick
            log.exception("[news] tick raised")
        await asyncio.sleep(POLL_INTERVAL_SEC)


def trigger_now(limit: int | None = None) -> bool:
    """The button. Starts one search, capped at `limit` stories.

    Returns False when one is already in flight, which the route turns into a
    sentence rather than an error: two people pressing the button a second apart
    should not buy the same search twice.
    """
    global _manual_task
    if _running or (_manual_task and not _manual_task.done()):
        return False

    async def _go() -> None:
        global _running
        if _running:
            return
        _running = True
        try:
            cfg = await news_store.get_config()
            await _run_once(cfg, limit=limit, trigger="manual")
        except Exception:  # noqa: BLE001
            log.exception("[news] the search failed")
        finally:
            _running = False

    _manual_task = asyncio.create_task(_go())
    return True


async def cancel_now() -> bool:
    """Stop the search that is running. True if anything was actually stopped.

    TWO HALVES, AND BOTH ARE NEEDED. Cancelling the task stops the work and the
    spend; closing the row is what the screen reads, and a task that dies
    without closing its row is exactly the ghost this button exists to clear.
    The row is closed even when there is no task to kill, because the common
    case is a row stranded by a restart that happened days ago.
    """
    global _manual_task, _running

    task, _manual_task = _manual_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _running = False

    try:
        closed = await news_store.cancel_running()
    except Exception:  # noqa: BLE001 - the task is already stopped either way
        log.exception("[news] could not close the cancelled run")
        return task is not None

    if closed:
        log.info("[news] cancelled %d run(s)", closed)
    return bool(closed) or task is not None


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
        log.info("news_poller stopped")
