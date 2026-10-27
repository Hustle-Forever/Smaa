"""
Tests for the "Enhance" prompt rules (the /enhance endpoint's system prompts).

Every Enhance button in Yas Producer (Create Poster, Poster + Human, Animation,
both Talking Head editors) hits ONE endpoint — POST /api/yas-producer/enhance —
which dispatches to topic / feedback / dialogue helpers. These prompts used to
COMPRESS the user's input into a ≤25-word headline+subtitle and drop the concrete
details they typed (dates, times, locations, CTAs, links, prices, names).

This suite guards the fix: a single shared _ENHANCE_PRESERVE_RULE is embedded in
ALL THREE system prompts, and the rigid word-caps that forced information loss are
gone. It is pure string assembly — no DB/network needed at import — so it runs
standalone:

    cd backend && python tests/test_enhance_prompts.py

It is ALSO pytest-compatible (every check is a ``test_*`` function).
"""
import sys
from pathlib import Path

# tests/ -> backend/ is parents[1]; make ``import app...`` resolve from anywhere.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.routers.yas_producer import (  # noqa: E402
    _ENHANCE_PRESERVE_RULE,
    _ENHANCE_TOPIC_SYSTEM,
    _ENHANCE_POSTER_HUMAN_SYSTEM,
    _ENHANCE_FEEDBACK_SYSTEM,
    _ENHANCE_DIALOGUE_SYSTEM,
    _ENHANCE_CHARACTER_SYSTEM,
)

# NOTE: the "character" kind is deliberately NOT in this dict — it is a person
# casting brief, and the PRESERVE_RULE (dates/CTAs/links) is poster-fact
# oriented, so character intentionally does not embed it (tested below).
# poster_human IS here — its copy carries poster facts, so it embeds the rule.
_ALL_SYSTEMS = {
    "topic": _ENHANCE_TOPIC_SYSTEM,
    "poster_human": _ENHANCE_POSTER_HUMAN_SYSTEM,
    "feedback": _ENHANCE_FEEDBACK_SYSTEM,
    "dialogue": _ENHANCE_DIALOGUE_SYSTEM,
}


# ---------------------------------------------------------------------------
# 1. The shared rule is reused in EVERY poster-fact mode (the "reuse everywhere" ask)
# ---------------------------------------------------------------------------

def test_preserve_rule_is_embedded_in_all_poster_copy_modes():
    for kind, sys_prompt in _ALL_SYSTEMS.items():
        assert _ENHANCE_PRESERVE_RULE in sys_prompt, \
            f"{kind}: shared preserve-facts rule is not embedded"


# ---------------------------------------------------------------------------
# 2. The rule actually names the categories the user cares about
# ---------------------------------------------------------------------------

def test_preserve_rule_names_protected_categories():
    low = _ENHANCE_PRESERVE_RULE.lower()
    # The exact things the user said must never be removed.
    for token in ("date", "time", "location", "url", "link", "name"):
        assert token in low, f"preserve rule never mentions '{token}'"
    # A call-to-action, by either spelling.
    assert "call-to-action" in low or "cta" in low or "rsvp" in low, \
        "preserve rule never mentions a call-to-action"
    # And the core mandate: keep the fact, never drop/invent it.
    assert "verbatim" in low
    assert "never drop" in low or "do not drop" in low
    assert "not invent" in low or "never" in low and "invent" in low


# ---------------------------------------------------------------------------
# 3. The info-destroying constraints are GONE from the topic enhancer
# ---------------------------------------------------------------------------

def test_topic_no_longer_forces_lossy_headline_subtitle_format():
    s = _ENHANCE_TOPIC_SYSTEM
    # The rigid template that couldn't carry a date/CTA is gone.
    assert "<headline>. <subtitle>" not in s
    # The blanket hashtag ban is gone (hashtags the user typed are facts now).
    assert "No hashtags" not in s
    # Multi-line, detail-carrying output is explicitly allowed.
    assert "Multi-line" in s or "multi-line" in s
    # Still brand-on-tone copy for 42.
    assert "42 Abu Dhabi" in s


def test_topic_user_facing_caps_relaxed():
    # The "under 25 words" / "one line" hard caps lived in the prompt text; the
    # system prompt must not reintroduce them.
    s = _ENHANCE_TOPIC_SYSTEM
    assert "25 words" not in s
    assert "on one line" not in s


# ---------------------------------------------------------------------------
# 4. Feedback enhancer no longer hard-caps at one short sentence
# ---------------------------------------------------------------------------

def test_feedback_word_cap_relaxed():
    s = _ENHANCE_FEEDBACK_SYSTEM
    assert "under 30 words" not in s
    # Still sharpens intent and stays concrete.
    assert "do not change it" in s.lower() or "do not change" in s.lower() \
        or "don't change" in s.lower()


# ---------------------------------------------------------------------------
# 5. Dialogue enhancer keeps a SOFT spoken-word target (not a hard trim)
# ---------------------------------------------------------------------------

def test_dialogue_target_is_soft_not_a_hard_trim():
    s = _ENHANCE_DIALOGUE_SYSTEM
    assert "Trim to" not in s, "dialogue still hard-trims (can drop facts)"
    low = s.lower()
    assert "aim for" in low, "dialogue must use a soft 'aim for' target"
    # Still a spoken 8-second reel line.
    assert "8-second" in s or "8 s" in s


# ---------------------------------------------------------------------------
# 6. Character enhancer (Human Character) — a casting brief, NOT poster copy
# ---------------------------------------------------------------------------

def test_character_prompt_is_person_trait_oriented():
    low = _ENHANCE_CHARACTER_SYSTEM.lower()
    # The traits Stage-1 casting consumes must be named.
    for token in ("age", "gender", "wardrobe"):
        assert token in low, f"character prompt never mentions '{token}'"
    assert "ethnicity" in low or "nationality" in low
    assert "expression" in low or "vibe" in low
    # One singular person — not a crowd.
    assert "one" in low and "person" in low
    # Fixed traits must be honoured (Stage-1's contract).
    assert "keep every trait" in low or "never change" in low


def test_character_prompt_forbids_poster_scaffolding():
    s = _ENHANCE_CHARACTER_SYSTEM
    # These words appear ON the prohibition list — assert they're named as bans.
    for banned in ("headline", "hashtag", "emoji", "logo"):
        assert banned.lower() in s.lower(), \
            f"character prompt should explicitly ban '{banned}'"
    assert "NO headline" in s or "no headline" in s.lower()
    # Output shape: short prose brief.
    assert "1-2 sentences" in s


def test_character_does_not_reuse_poster_preserve_rule():
    # The preserve rule protects poster facts (dates/CTAs/links) — wrong frame
    # for a person brief; the character prompt must NOT embed it.
    assert _ENHANCE_PRESERVE_RULE not in _ENHANCE_CHARACTER_SYSTEM


def test_enhance_dispatch_wires_character_kind():
    import inspect
    from app.routers import yas_producer as yp
    src = inspect.getsource(yp.enhance)
    assert '"character"' in src, "enhance() dispatch has no character branch"
    assert "_enhance_character" in src
    # And the helper targets the character system prompt.
    helper_src = inspect.getsource(yp._enhance_character)
    assert "_ENHANCE_CHARACTER_SYSTEM" in helper_src


# ---------------------------------------------------------------------------
# 7. Poster+Human enhancer — labelled information-poster copy, NOT the shared
#    'topic' enhancer. Emits HEADLINE / SUBTITLE / CARD / DETAILS placement
#    slots that the image prompt's _poster_content_block reads.
# ---------------------------------------------------------------------------

def test_poster_human_emits_labelled_slots():
    s = _ENHANCE_POSTER_HUMAN_SYSTEM
    for label in ("HEADLINE:", "SUBTITLE:", "CARD:", "DETAILS:"):
        assert label in s, f"poster_human prompt never defines the '{label}' slot"


def test_poster_human_is_information_poster_with_a_human():
    low = _ENHANCE_POSTER_HUMAN_SYSTEM.lower()
    assert "information poster" in low
    assert "real person" in low or "person as the hero" in low


def test_poster_human_forbids_drawn_logo_and_non_latin():
    low = _ENHANCE_POSTER_HUMAN_SYSTEM.lower()
    # The logo is composited onto the poster later — the copy must never emit a
    # 42 mark or wordmark.
    assert "do not write a logo" in low
    assert "42 abu dhabi" in low  # named as a banned wordmark
    # Latin-only, matching the image prompt's text-fidelity rule.
    assert "latin script only" in low
    assert "arabic" in low


def test_poster_human_does_not_invent_facts():
    low = _ENHANCE_POSTER_HUMAN_SYSTEM.lower()
    assert "never invent" in low
    # It is a distinct system prompt, not the shared generic topic enhancer.
    assert _ENHANCE_POSTER_HUMAN_SYSTEM != _ENHANCE_TOPIC_SYSTEM


def test_enhance_dispatch_wires_poster_human_kind():
    import inspect
    from app.routers import yas_producer as yp
    src = inspect.getsource(yp.enhance)
    assert '"poster_human"' in src, "enhance() dispatch has no poster_human branch"
    assert "_enhance_poster_human" in src
    # And the helper targets the poster_human system prompt.
    helper_src = inspect.getsource(yp._enhance_poster_human)
    assert "_ENHANCE_POSTER_HUMAN_SYSTEM" in helper_src


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = sorted(
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — report every failure, keep going
            failures += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
