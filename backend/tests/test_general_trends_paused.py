"""Tests for the PAUSE — `GENERAL_TRENDS_ENABLED` switched off.

Viral Social Trends ships but does not yet do what it was built for: too many
cards are things that HAPPENED rather than formats people are RECREATING. That
is a research problem in source selection, not a bug with a patch, so the whole
stream is paused — the collector does not run, and the social-listening report
is a two-stream document.

WHY THIS FILE EXISTS SEPARATELY. `test_social_report.py` describes the
three-stream report and turns the stream back ON for its own duration, because
that report still works and must keep working for the day the research lands.
Nothing there would notice if the pause silently stopped applying. So the paused
world is asserted here, once, against the module's real configured state.

THE THREE THINGS THAT MUST HOLD, and each has bitten a feature flag before:

  * Nothing collects. Not the poller's loop, not an auto-queue, and not a
    manual "Refresh now" — a pause that only stops the timer still collects
    every time someone clicks.
  * The report does not carry a frozen snapshot. With the collector off, a
    Viral Social Trends number could only be a shrinking count of rows ageing
    out of the window: it would look measured and mean nothing.
  * Nothing is DELETED. Every constant, builder, section and table survives, and
    `set_viral_stream(True)` restores all of it — this is the test that the
    revert really is a config change.

No network, no database, no model.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import config  # noqa: E402
from app.services import general_trends_pipeline as pipeline  # noqa: E402
from app.services import social_report as sr  # noqa: E402
from app.workers import general_trends_poller as poller  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_viral_stream():
    """Every test here mutates a module-level switch. Put it back, or the next
    file to run inherits whatever this one left behind."""
    yield
    sr.set_viral_stream(config.general_trends_enabled)


# ---------------------------------------------------------------------------
# 1. The switch itself
# ---------------------------------------------------------------------------

def test_collection_is_off_by_default():
    """The default is FALSE, not "false in this checkout's .env".

    A default of True with an env override would restart the collector on every
    fresh clone, every CI run and every new VPS — which is precisely the pause
    failing to hold."""
    assert config.general_trends_enabled is False
    assert sr.VIRAL_STREAM is False


def test_the_report_module_follows_the_config_at_import():
    assert sr.STREAM_ORDER == (sr.STREAM_COMPETITOR, sr.STREAM_NEWS)
    assert sr.STREAM_VIRAL not in sr.STREAM_ORDER


# ---------------------------------------------------------------------------
# 2. Nothing collects
# ---------------------------------------------------------------------------

def test_the_poller_does_not_start():
    """No task means no tick, no auto-queue and no claim of a queued run."""
    assert poller.start() is None


def test_a_manual_refresh_is_refused_not_queued():
    """`start_refresh` is the single funnel every caller goes through — the HTTP
    endpoint, a cron, a REPL. Queueing a run the (stopped) poller can never
    claim would leave a row stuck `pending` and the UI waiting forever."""
    import asyncio

    with pytest.raises(pipeline.RefreshPaused) as exc:
        asyncio.run(pipeline.start_refresh(trigger="manual"))
    assert "GENERAL_TRENDS_ENABLED" in str(exc.value)


def test_paused_is_not_a_kind_of_busy():
    """Distinct classes on purpose: "come back in a minute" and "this stream is
    switched off" are different answers, and a client that retries the first
    would retry the second forever."""
    assert not issubclass(pipeline.RefreshPaused, pipeline.RefreshBusy)
    assert not issubclass(pipeline.RefreshBusy, pipeline.RefreshPaused)


def test_the_refresh_route_answers_423_not_409():
    import inspect

    from app.routers import general_trends as router_mod

    src = inspect.getsource(router_mod.general_refresh)
    assert "RefreshPaused" in src and "423" in src
    # The busy case must survive alongside it — the pause did not replace it.
    assert "RefreshBusy" in src and "409" in src


# ---------------------------------------------------------------------------
# 3. The report is a two-stream document
# ---------------------------------------------------------------------------

def test_viral_is_not_an_offered_scope():
    assert sr.REPORT_SCOPES == ("all", "competitors", "news")
    assert "viral" not in sr.SCOPE_STREAMS
    assert "viral" not in sr.SCOPE_LABELS


def test_an_all_streams_report_covers_two_streams():
    assert sr.SCOPE_STREAMS["all"] == (sr.STREAM_COMPETITOR, sr.STREAM_NEWS)
    # The label is counted, not hardcoded: "All three streams" printed on the
    # cover of a two-stream report is the exact class of number-shaped untruth
    # this whole module exists to prevent.
    assert sr.SCOPE_LABELS["all"] == "Both streams"


def test_emerging_trends_is_not_a_section_at_all():
    """Dropped, NOT rendered as "not applicable".

    That treatment is for a section a READER narrowed away by choosing a scope,
    and it reads as "you could have had this". Nobody chose this, and right now
    there is no such measure — so the heading does not belong in the document."""
    assert "emerging_trends" not in sr.FULL_SECTION_IDS
    assert len(sr.FULL_SECTION_IDS) == len(sr.ALL_SECTION_IDS) - 1
    # Every OTHER section keeps its id and its position.
    assert list(sr.FULL_SECTION_IDS) == [s for s in sr.ALL_SECTION_IDS
                                         if s != "emerging_trends"]


def test_a_trend_is_not_an_item_kind():
    """An item report about one trend is reached from a trend card, and there
    are no trend cards while the stream is paused. A bookmarked `?kind=trend`
    gets a 422 naming the real kinds rather than a report built from a row
    nothing is refreshing."""
    assert sr.ITEM_KINDS == ("post",)
    assert sr.clean_kind("trend") == ""
    assert sr.clean_kind("post") == "post"


def test_the_basis_omits_the_count_rather_than_nulling_it():
    """ABSENT and None mean different things and both are used deliberately.

    None renders as a dash labelled "not in scope", which invites "then where do
    I get it?" — and while collection is paused the answer is nowhere. So the
    key is not written at all, and both surfaces already skip what they are not
    given (the PDF filters `is not None`; the panel checks `!== undefined`)."""
    data = sr.ReportData(**_empty_window())
    basis = sr.build_basis(data)
    assert "viral_trends" not in basis
    # The streams that ARE in the product still report their counts.
    assert basis["competitor_posts"] == 0
    assert basis["news_items"] == 0


def test_a_scoped_report_never_mentions_the_paused_stream():
    """`scope_sentence` names what a narrowed report leaves out. It must list
    only streams the reader could have chosen — naming a paused one would
    advertise a report that cannot be built."""
    data = sr.ReportData(scope="competitors", **_empty_window())
    sentence = data.scope_sentence()
    assert "Industry Signal" in sentence
    assert "Viral" not in sentence


# ---------------------------------------------------------------------------
# 4. Nothing was deleted
# ---------------------------------------------------------------------------

def test_the_full_vocabulary_survives_the_pause():
    """The `ALL_*` names are the whole language this module can speak, and none
    of it narrows with the switch."""
    assert sr.ALL_STREAMS == (sr.STREAM_COMPETITOR, sr.STREAM_NEWS, sr.STREAM_VIRAL)
    assert sr.ALL_ITEM_KINDS == ("post", "trend")
    assert "emerging_trends" in sr.ALL_SECTION_IDS
    assert sr.STREAM_LABELS[sr.STREAM_VIRAL] == "Viral Social Trends"
    # The builder for the dropped section is still wired up, so re-enabling
    # does not land on a KeyError.
    assert "emerging_trends" in sr.FULL_BUILDERS


def test_turning_it_back_on_restores_every_derived_constant():
    """THE REVERT TEST. If this passes, re-enabling the stream is a config
    change; if it fails, it is a code change nobody has written yet."""
    sr.set_viral_stream(True)
    assert sr.VIRAL_STREAM is True
    assert sr.STREAM_ORDER == sr.ALL_STREAMS
    assert sr.REPORT_SCOPES == ("all", "competitors", "news", "viral")
    assert sr.SCOPE_STREAMS["all"] == sr.ALL_STREAMS
    assert sr.SCOPE_STREAMS["viral"] == (sr.STREAM_VIRAL,)
    assert sr.SCOPE_LABELS["all"] == "All three streams"
    assert sr.ITEM_KINDS == ("post", "trend")
    assert sr.FULL_SECTION_IDS == sr.ALL_SECTION_IDS

    sr.set_viral_stream(False)
    assert sr.REPORT_SCOPES == ("all", "competitors", "news")
    assert "emerging_trends" not in sr.FULL_SECTION_IDS


def test_the_switch_is_idempotent():
    """Called twice with the same value it must not accumulate anything — the
    derivations rebuild from `ALL_*` rather than editing what is already there."""
    sr.set_viral_stream(False)
    once = (sr.STREAM_ORDER, sr.REPORT_SCOPES, sr.ITEM_KINDS, sr.FULL_SECTION_IDS)
    sr.set_viral_stream(False)
    assert (sr.STREAM_ORDER, sr.REPORT_SCOPES, sr.ITEM_KINDS,
            sr.FULL_SECTION_IDS) == once


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _empty_window() -> dict:
    """The window fields `ReportData` requires, with no rows in them. The dates
    are fixed rather than `now()`-derived so this file can never become a
    time-bomb that passes on the day it is written."""
    from datetime import datetime, timedelta, timezone

    to = datetime(2026, 8, 25, tzinfo=timezone.utc)
    frm = to - timedelta(days=30)
    return {
        "now": to, "days": 30,
        "win_from": frm, "win_to": to,
        "prev_from": frm - timedelta(days=30), "prev_to": frm,
    }


if __name__ == "__main__":  # pragma: no cover — standalone run
    raise SystemExit(pytest.main([__file__, "-q"]))
