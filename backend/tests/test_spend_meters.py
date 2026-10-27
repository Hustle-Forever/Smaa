"""The spend meters count, they do not police.

WHAT CHANGED AND WHY. Both meters used to raise once estimated spend crossed a
number from `.env`, and every gate in the app compared `spent_usd` to
`budget_usd` by hand. The owner had it removed, and the reasons are worth
keeping because "add a budget cap" is a permanently tempting idea:

  - ONE POT, MANY FEATURES. `VEO_BUDGET_USD` reads like a cap on video and was
    really a cap on every Google call in the app. Generating a few videos could
    leave the news search recording `no_budget` on a day nobody had touched a
    video, so the message on screen was "we could not afford to look" for a
    reason with nothing to do with the news.
  - IT WAS NEVER A CAP. `spent_usd` lives in the process and resets on restart,
    so three restarts spend three times the "budget". It reliably stopped honest
    work and did not stop the runaway it was aimed at.
  - IT WAS NOT THE PROVIDER'S LIMIT. Google and OpenAI both have real caps, in
    their own dashboards, which actually hold. This was a homemade one on top.

So these tests pin two things: that nothing refuses work while the meters are
unenforced, and that the machinery to turn a cap back on is still wired, so
`enforced = True` is a one-line change rather than an excavation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.openai_client import BudgetExceededError, CostMeter  # noqa: E402
from app.services.vertex_client import (  # noqa: E402
    VertexBudgetExceededError,
    VertexCostMeter,
)

METERS = [
    pytest.param(VertexCostMeter, VertexBudgetExceededError, id="vertex"),
    pytest.param(CostMeter, BudgetExceededError, id="openai"),
]


# --------------------------------------------------------------------------- #
# Off by default
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("meter_cls,exc", METERS)
def test_a_new_meter_is_not_enforcing(meter_cls, exc):
    """The default is what every caller gets, so the default is the behaviour."""
    assert meter_cls().enforced is False


@pytest.mark.parametrize("meter_cls,exc", METERS)
def test_spending_far_past_the_budget_never_raises(meter_cls, exc):
    m = meter_cls(budget_usd=10.0)
    for _ in range(100):
        m.add("some-model", 5.0)          # $500 against a $10 "budget"
    assert m.spent_usd == pytest.approx(500.0)


@pytest.mark.parametrize("meter_cls,exc", METERS)
def test_over_budget_is_false_however_much_is_spent(meter_cls, exc):
    m = meter_cls(spent_usd=1_000_000.0, budget_usd=1.0)
    assert m.over_budget() is False
    assert m.over_budget(0.5) is False
    assert m.over_budget(0.95) is False


# --------------------------------------------------------------------------- #
# Still counting, which is the whole point of keeping it
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("meter_cls,exc", METERS)
def test_it_still_adds_up_what_was_spent(meter_cls, exc):
    m = meter_cls(budget_usd=1.0)
    m.add("veo-3.1-fast", 2.50)
    m.add("veo-3.1-fast", 1.25)
    m.add("gemini-2.5-pro", 0.10)
    assert m.spent_usd == pytest.approx(3.85)
    assert m.by_model["veo-3.1-fast"] == pytest.approx(3.75)
    assert m.by_model["gemini-2.5-pro"] == pytest.approx(0.10)


def test_the_producer_status_endpoint_can_still_read_a_number():
    """`routers/agents_producer` puts `budget_usd` straight into JSON. Turning
    the cap off by setting it to infinity would have been the shorter change and
    would have broken that endpoint, since JSON has no infinity."""
    import json
    m = VertexCostMeter()
    json.dumps({"budget_usd": m.budget_usd, "spent_usd": m.spent_usd})


# --------------------------------------------------------------------------- #
# The switch back
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("meter_cls,exc", METERS)
def test_enforcing_restores_the_ceiling(meter_cls, exc):
    m = meter_cls(budget_usd=10.0, enforced=True)
    m.add("model", 9.0)                   # under, fine
    with pytest.raises(exc):
        m.add("model", 2.0)               # crosses


@pytest.mark.parametrize("meter_cls,exc", METERS)
def test_enforcing_restores_the_fractional_gates(meter_cls, exc):
    """Four gates across the app ask at a fraction: 0.95 twice, 0.8 once, 1.0
    once. One method answers for all of them now."""
    m = meter_cls(spent_usd=96.0, budget_usd=100.0, enforced=True)
    assert m.over_budget(0.95) is True
    assert m.over_budget() is False       # 96 is not yet 100
    assert m.over_budget(0.5) is True


@pytest.mark.parametrize("meter_cls,exc", METERS)
def test_a_zero_budget_never_gates_even_when_enforced(meter_cls, exc):
    """`budget_usd` of 0 means "no cap set", not "no money". Without this guard
    the comparison is `spent >= 0`, which is true immediately and would refuse
    the very first call of the day."""
    m = meter_cls(spent_usd=0.0, budget_usd=0.0, enforced=True)
    assert m.over_budget() is False
    assert m.over_budget(0.95) is False


# --------------------------------------------------------------------------- #
# The gate that was actually hit
# --------------------------------------------------------------------------- #

def test_the_news_run_can_no_longer_report_no_budget():
    """A search used to be abandoned before it started when the SHARED meter had
    under $0.50 of headroom, which is how video generation came to silence the
    news feed. The gate is gone; the status string stays because rows in
    `news_runs` still carry it."""
    from app.workers import news_poller
    from app.services import news_store

    assert not hasattr(news_poller, "MIN_BUDGET_HEADROOM_USD")
    assert not hasattr(news_poller, "_budget_headroom")
    assert "no_budget" in news_store.RUN_STATUSES
