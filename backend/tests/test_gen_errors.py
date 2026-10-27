"""Tests for ``app/services/gen_errors.py`` — the sentences a failure shows.

These are pure: no model, no database, no network.

WHY THE STRINGS ARE ASSERTED AND NOT JUST THE BRANCHES. What matters about this
module is not that it picks a branch, it is that the branch it picks tells
somebody the right thing to go and do. A test that only checked "returns a
non-empty string" would pass while the OpenAI-billing case advised checking a
Google Cloud project. So each test asserts the ACTION the message points at.

The classification is by TEXT, on purpose — these exceptions come from three
SDKs whose class hierarchies get reorganised between minor versions, and a match
on class would silently stop firing and regress the cards back to raw
tracebacks. That makes the ORDER of the checks load-bearing, which is what the
ordering tests below exist to pin.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services import gen_errors as ge  # noqa: E402


# Real shapes, copied from what these providers actually return.
OPENAI_BILLING = Exception(
    "Error code: 400 - {'error': {'message': 'Billing hard limit has been "
    "reached', 'type': 'invalid_request_error', 'code': 'billing_hard_limit'}}"
)
OPENAI_QUOTA = Exception(
    "RateLimitError: Error code: 429 - {'error': {'message': 'You exceeded your "
    "current quota, please check your plan and billing details.', 'code': "
    "'insufficient_quota'}}"
)
OPENAI_BAD_KEY = Exception(
    "AuthenticationError: Error code: 401 - {'error': {'message': 'Incorrect API "
    "key provided: sk-abc***', 'code': 'invalid_api_key'}}"
)
VERTEX_BILLING = Exception(
    "403 PERMISSION_DENIED. Cloud AI Platform API has not been used in project "
    "1234 before or billing is not enabled."
)
RAI_BLOCK = Exception("Response blocked by RESPONSIBLE AI filter: prohibited_content")
PLAIN_RATE_LIMIT = Exception("429 RESOURCE_EXHAUSTED: too many requests, retry shortly")


def _friendly(exc, what="this poster"):
    return ge.explain(exc, what=what)[0]


# ---------------------------------------------------------------------------
# 1. Nothing is thrown away
# ---------------------------------------------------------------------------

def test_the_raw_text_always_survives_alongside_the_readable_one():
    """The tempting fix for an unreadable error is to replace it. That loses the
    only diagnostic there was, so `explain` returns BOTH and the caller keeps
    both — friendly on the card, technical in the log."""
    friendly, technical = ge.explain(OPENAI_BILLING)
    assert "billing_hard_limit" in technical      # verbatim, for whoever debugs
    assert "billing_hard_limit" not in friendly   # never, for whoever is working
    assert len(friendly) > 40


def test_technical_text_is_capped_so_it_cannot_flood_a_log_line():
    huge = Exception("x" * 5000)
    _, technical = ge.explain(huge)
    assert len(technical) <= ge.MAX_TECHNICAL


# ---------------------------------------------------------------------------
# 2. Each message points at the right action
# ---------------------------------------------------------------------------

def test_openai_billing_says_gemini_carries_on_rather_than_sounding_fatal():
    """This is the case the owner cares most about: OpenAI running out must not
    read as the tool being broken, because stage 1 now falls back to Gemini."""
    msg = _friendly(OPENAI_BILLING).lower()
    assert "openai" in msg and "gemini" in msg
    # Must NOT send them to the Google Cloud console — that is the other message.
    assert "google cloud" not in msg


def test_an_exhausted_openai_quota_is_billing_advice_not_slow_down_advice():
    """A 429 for `insufficient_quota` mentions BOTH quota and rate limiting. It
    is a billing state somebody has to go and fix; telling them to wait a moment
    is advice that will never come true, which is why the checks are ordered."""
    msg = _friendly(OPENAI_QUOTA).lower()
    assert "billing" in msg
    assert "wait a moment" not in msg


def test_a_bad_openai_key_names_the_setting_to_change():
    msg = _friendly(OPENAI_BAD_KEY)
    assert "OPENAI_API_KEY" in msg


def test_a_google_billing_failure_is_not_reported_as_an_openai_one():
    """Both say "billing" and the fixes are in different consoles."""
    msg = _friendly(VERTEX_BILLING).lower()
    assert "google cloud" in msg
    assert "openai" not in msg


def test_a_refusal_tells_the_user_to_change_the_prompt_or_photo():
    """The one failure whose fix belongs to the person looking at the screen
    rather than to an operator — so it must not read as an outage."""
    msg = _friendly(RAI_BLOCK).lower()
    assert "refused" in msg
    assert "reword" in msg or "photo" in msg


def test_a_transient_rate_limit_says_wait_rather_than_go_fix_something():
    msg = _friendly(PLAIN_RATE_LIMIT).lower()
    assert "wait" in msg or "slow down" in msg
    assert "billing" not in msg


def test_a_timeout_reads_as_a_timeout():
    msg = _friendly(asyncio.TimeoutError()).lower()
    assert "too long" in msg


def test_an_unrecognised_failure_admits_it_instead_of_guessing():
    """The honest default. A wrong diagnosis is worse than none: it sends
    somebody to check a key that was never the problem."""
    msg = _friendly(ValueError("something nobody predicted")).lower()
    assert "try again" in msg
    for wrong in ("billing", "api key", "refused", "too long"):
        assert wrong not in msg


def test_the_subject_of_the_sentence_follows_the_caller():
    """One helper serves a poster, a variant and an animation frame without any
    of them reading like a filled-in template."""
    assert "this poster" in _friendly(asyncio.TimeoutError(), what="this poster")
    assert "the intro card" in _friendly(asyncio.TimeoutError(), what="the intro card")


# ---------------------------------------------------------------------------
# 3. Ordering, which is where a text-matching classifier goes wrong
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc,predicate", [
    (OPENAI_BILLING, ge.is_openai_billing),
    (OPENAI_QUOTA, ge.is_openai_billing),
    (OPENAI_BAD_KEY, ge.is_openai_auth),
    (VERTEX_BILLING, ge.is_vertex_unavailable),
    (RAI_BLOCK, ge.is_safety_block),
    (PLAIN_RATE_LIMIT, ge.is_rate_limit),
])
def test_each_error_is_recognised_by_the_predicate_that_owns_it(exc, predicate):
    assert predicate(exc) is True


def test_a_vertex_billing_error_is_not_claimed_by_the_openai_billing_check():
    """The overlap that decides the whole ordering: both contain "billing"."""
    assert ge.is_openai_billing(VERTEX_BILLING) is False


def test_budget_message_reads_as_a_choice_somebody_made():
    """A spend cap is neither an outage nor a bug, and saying "failed" about one
    sends people looking for a fault that does not exist."""
    msg = ge.budget_message(Exception("cap $5.00 reached")).lower()
    assert "spend cap" in msg
    assert "wrong" not in msg and "error" not in msg


def test_log_and_explain_returns_the_friendly_half_and_logs_the_other():
    class _Log:
        def __init__(self): self.lines = []
        def warning(self, *a): self.lines.append(a)

    logger = _Log()
    out = ge.log_and_explain(logger, "poster", 42, OPENAI_BILLING, what="this poster")
    assert out == _friendly(OPENAI_BILLING)
    assert logger.lines, "the technical half must reach the log"
    assert "billing_hard_limit" in str(logger.lines[0])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
