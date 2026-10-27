"""One place that turns a generation failure into a sentence a person can act on.

WHY THIS EXISTS. Every failure in the poster pipeline used to reach the screen
exactly as Python spelled it. A card in the grid would read:

    stage2_failed: BadRequestError: Error code: 400 - {'error': {'message':
    'Billing hard limit has been reached', 'type': 'invalid_request_error', ...

which tells a marketer nothing they can do, and buries the one useful word
("billing") in the middle of a dict. The failures worth distinguishing are few
and they are almost all CONFIGURATION — a key, a spend cap, a disabled project —
so they have real answers, and the answer is what belongs on the card.

THE CONTRACT: `explain()` returns TWO strings, and both are kept.

    friendly   goes in `yas_generations.error`, which is what the UI renders
    technical  goes in `metadata.error_technical` and into the log

Nothing is thrown away. That is the whole reason this is a pair and not a
replacement: the previous behaviour was diagnostic and unreadable, and the
tempting fix — swap it for "Something went wrong" — is readable and useless.
Whoever is debugging still needs the 400 body verbatim.

MATCHING IS ON TEXT, not on exception classes, and deliberately. These come from
three SDKs (openai, google-genai, httpx) whose exception hierarchies get
reorganised between minor versions, and a match that silently stops firing would
regress this file back to raw tracebacks without anything failing. The words
"billing", "quota" and "api key" are far more stable than the classes carrying
them.
"""
from __future__ import annotations

import asyncio
from typing import Optional

# Kept short: this lands in a `text` column that the grid renders inside a
# 200px-wide tile, and a paragraph there is its own kind of unreadable.
MAX_TECHNICAL = 500


def _text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _lower(exc: BaseException) -> str:
    return f"{type(exc).__name__} {exc}".lower()


# --- the checks, most specific first ---------------------------------------
#
# Order matters. An OpenAI 429 for `insufficient_quota` mentions both "quota"
# and "rate limit"; the first is a billing state somebody has to go and fix and
# the second resolves itself in a minute, so the wrong order gives exactly the
# wrong advice.

def is_openai_billing(exc: BaseException) -> bool:
    m = _lower(exc)
    return any(s in m for s in (
        "billing_hard_limit", "billing hard limit", "insufficient_quota",
        "exceeded your current quota", "billing details",
    ))


def is_openai_auth(exc: BaseException) -> bool:
    m = _lower(exc)
    return any(s in m for s in (
        "invalid_api_key", "incorrect api key", "authenticationerror",
        "no api key", "openai_api_key",
    ))


def is_rate_limit(exc: BaseException) -> bool:
    m = _lower(exc)
    return ("rate limit" in m or "ratelimiterror" in m or "429" in m
            or "resource_exhausted" in m)


def is_vertex_unavailable(exc: BaseException) -> bool:
    """Billing off / no credentials / project not enabled — the Google side of
    the same class of problem. Mirrors trend_scoring._is_unavailable, which is
    the older copy of this test and stays where it is because the trends feed
    degrades rather than fails."""
    m = _lower(exc)
    return any(s in m for s in (
        "permission_denied", "billing", "not enabled", "unauthenticated",
        "credential", "403", "401",
    ))


def is_safety_block(exc: BaseException) -> bool:
    """The model refused the prompt or the reference image. Common enough on a
    tool whose whole job is putting real students on posters that it earns its
    own sentence — and the fix is the user's, not an operator's."""
    m = _lower(exc)
    return any(s in m for s in (
        "safety", "blocked", "prohibited_content", "responsible ai",
        "rai_filter", "29310472", "15236754", "person/face",
    ))


def is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in _lower(exc)


def explain(exc: BaseException, *, what: str = "this image") -> tuple[str, str]:
    """(friendly, technical). `what` names the thing that failed, so one helper
    serves a poster, a variant and an animation frame without any of them
    reading like a template."""
    technical = _text(exc)[:MAX_TECHNICAL]

    if is_openai_billing(exc):
        friendly = (
            "OpenAI has stopped accepting requests — its billing limit was "
            "reached. The written ideas now come from Gemini instead, so this "
            "usually just works on the next try; if it keeps happening, top up "
            "the OpenAI account or leave it and Gemini will carry on."
        )
    elif is_openai_auth(exc):
        friendly = (
            "The OpenAI key isn't being accepted. Gemini is used instead for the "
            "written part, so generation should still work — fix OPENAI_API_KEY "
            "in Settings when you get a chance."
        )
    elif is_vertex_unavailable(exc):
        friendly = (
            "The image model can't be reached — the Google Cloud project is "
            "missing billing or credentials. Nothing will generate until that is "
            "sorted; everything already made is untouched."
        )
    elif is_safety_block(exc):
        friendly = (
            f"The image model refused to make {what}. It usually means the prompt "
            "or a reference photo tripped its people-and-likeness rules — try "
            "rewording the topic, or swap the student photo."
        )
    elif is_rate_limit(exc):
        friendly = (
            "Too many requests at once — the model asked us to slow down. Wait a "
            "moment and generate again; nothing was charged for this one."
        )
    elif is_timeout(exc):
        friendly = (
            f"Making {what} took too long and was given up on. The models are "
            "slower than usual right now — try again."
        )
    else:
        # The honest default. It says what is known (it failed, nothing was
        # kept) and does NOT pretend to a diagnosis, because a wrong diagnosis
        # sends somebody to check a key that was never the problem.
        friendly = (
            f"Couldn't make {what}. Something on the model's side went wrong — "
            "try again, and if it keeps failing send this to whoever runs the "
            "backend."
        )

    return friendly, technical


def budget_message(exc: BaseException) -> str:
    """The spend cap, which is neither an outage nor a bug: somebody chose it."""
    return (
        "The AI spend cap for this deployment has been reached, so nothing "
        f"further will generate until it is raised or the meter resets. ({exc})"
    )


def log_and_explain(logger, where: str, gen_id: object,
                    exc: BaseException, *, what: str = "this image") -> str:
    """`explain()`, with the technical half sent to the log and the friendly half
    returned for the row.

    The pair is split across two DESTINATIONS rather than two columns because
    the generations tables have no field for it — `yas_local_store._COLUMNS`
    carries `error` and nothing else free — and a log line is where somebody
    debugging looks anyway. What matters is that the raw text is never simply
    dropped in the act of making the card readable.
    """
    friendly, technical = explain(exc, what=what)
    logger.warning("[%s] generation %s failed: %s", where, gen_id, technical)
    return friendly
