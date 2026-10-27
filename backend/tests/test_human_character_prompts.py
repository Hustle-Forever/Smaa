"""
Tests for the Human Character generator's prompt surfaces
(``app.services.human_character_prompts``).

The Human Character tool reuses the Create-Poster two-stage pipeline but MUST
differ in two load-bearing ways, both of which this suite locks down:

  1. NO BRANDING ON A PERSON — the Stage-2 render system explicitly forbids any
     logo, text, or graphic overlay (the poster logo firewall is skipped via
     ``include_logo=False``). A regression that reintroduced poster/logo language
     here would bake the 42 mark onto a human face.
  2. CASTING, NOT SCENES — Stage 1 produces N DISTINCT PEOPLE who fit the brief
     (front-facing photoreal portraits), not radically different poster scenes.

Pure string assembly — no DB / network / credentials at import — so it runs
standalone:

    cd backend && python tests/test_human_character_prompts.py

It is ALSO pytest-compatible (every check is a ``test_*`` function).
"""
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.human_character_prompts import (  # noqa: E402
    HUMAN_CHARACTER_STYLE,
    HUMAN_CHARACTER_CONCEPTS_SYSTEM,
    HUMAN_CHARACTER_STAGE2_SYSTEM,
    HUMAN_CHARACTER_DESCRIBE_SYSTEM,
    HUMAN_CHARACTER_VARIATION_NUDGES,
    build_human_character_user_message,
)
from app.services.poster_styles import PosterStyle, POSTER_STYLES  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The style object is well-formed and NOT polluting the poster registry
# ---------------------------------------------------------------------------

def test_style_object_is_well_formed():
    s = HUMAN_CHARACTER_STYLE
    assert isinstance(s, PosterStyle)
    assert s.key == "human_character"
    assert s.label and s.description
    assert s.concepts_system is HUMAN_CHARACTER_CONCEPTS_SYSTEM
    assert s.gemini_stage2_system is HUMAN_CHARACTER_STAGE2_SYSTEM
    assert len(s.variation_nudges) >= 4
    assert len(set(s.variation_nudges)) == len(s.variation_nudges)


def test_not_registered_in_poster_styles():
    # The poster registry is asserted elsewhere to be exactly the four pickable
    # looks; the human-character style must stay OUT of it (it is passed directly
    # to the routes).
    assert "human_character" not in POSTER_STYLES
    assert set(POSTER_STYLES) == {"voxel", "voxel_classic", "pattern", "popup"}


# ---------------------------------------------------------------------------
# 2. THE CORE INVARIANT — a human carries NO logo and NO text
# ---------------------------------------------------------------------------

def test_stage2_forbids_logo_and_text():
    low = HUMAN_CHARACTER_STAGE2_SYSTEM.lower()
    assert "no logo" in low, "Stage-2 must explicitly forbid logos"
    assert "no text" in low, "Stage-2 must explicitly forbid text"
    assert "watermark" in low
    # The poster logo asset must never be referenced for a human portrait.
    assert "42_logo" not in low
    assert "logo firewall" not in low


def test_concepts_forbid_logo_and_text():
    low = HUMAN_CHARACTER_CONCEPTS_SYSTEM.lower()
    assert "no logo" in low and "no text" in low
    assert "42_logo" not in low
    # No poster/voxel BLEED — the word "voxel" may appear ONLY in a prohibition
    # ("never voxel"); it must never REQUEST the voxel/diorama look.
    assert "voxel diorama" not in low and "toy diorama" not in low
    assert "[text to render]" not in low


# ---------------------------------------------------------------------------
# 3. Stage 2 renders a realistic FRONT-FACING person (talking-head ready)
# ---------------------------------------------------------------------------

def test_stage2_is_front_facing_photoreal_portrait():
    low = HUMAN_CHARACTER_STAGE2_SYSTEM.lower()
    assert "front-facing" in low or "front facing" in low
    assert "portrait" in low
    assert "photo" in low  # photograph / photorealistic
    # Exactly one person — no collage / grid / crowd.
    assert "one person" in low or "single real human" in low


# ---------------------------------------------------------------------------
# 4. Stage 1 is a CASTING system (N distinct people, strict JSON)
# ---------------------------------------------------------------------------

def test_concepts_is_distinct_people_casting_json():
    s = HUMAN_CHARACTER_CONCEPTS_SYSTEM
    low = s.lower()
    assert "distinct" in low
    assert "casting" in low or "candidate" in low
    # Strict JSON contract, same parser as posters (parse_concepts/dedupe_concepts).
    assert '"concepts"' in s and '"summary"' in s and '"prompt"' in s


def test_user_message_requests_n_distinct_candidates():
    msg = build_human_character_user_message("a young emirati engineer", 4, ["older man, grey beard"])
    assert "a young emirati engineer" in msg
    assert "4" in msg                       # exactly N
    assert "older man, grey beard" in msg   # avoid-list threaded through
    low = msg.lower()
    assert "distinct" in low and "json" in low


def test_user_message_handles_empty_avoid_list():
    msg = build_human_character_user_message("a teacher", 4, None)
    assert "(none yet)" in msg


# ---------------------------------------------------------------------------
# 5. Describe system is person-focused (asset retrieval)
# ---------------------------------------------------------------------------

def test_describe_system_is_person_focused():
    low = HUMAN_CHARACTER_DESCRIBE_SYSTEM.lower()
    assert "person" in low
    assert "age" in low and "hair" in low
    # Variation nudges keep the SAME person (no identity swap language).
    assert all("same person" in n.lower() or "warmer" in n.lower() or "light" in n.lower()
               or "angle" in n.lower() or "wardrobe" in n.lower()
               for n in HUMAN_CHARACTER_VARIATION_NUDGES)


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
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
