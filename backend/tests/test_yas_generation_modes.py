"""The `yas_generations.mode` vocabulary, pinned.

WHY THIS FILE EXISTS. `yas_generations` is not created by this codebase — there
is no `CREATE TABLE` for it anywhere in backend/ — so its
`yas_generations_mode_check` CHECK constraint lives ONLY in the database. That
means the set of modes the Python can write and the set the database will accept
are two lists maintained in two places, with nothing keeping them in step.

They drifted, and the way it surfaced was this: Poster + Human "style inspired"
returned a 500 on every single attempt, while "template + portrait" worked
perfectly. The two go through different endpoints —

    template + portrait -> /poster-human/template-fill -> mode='template_fill'
    style inspired      -> /poster-human/generate      -> mode='poster_human'

— and `template_fill` was in the constraint while `poster_human` never had been.
The table had 68 template_fill rows and 0 poster_human rows: that path had never
once succeeded. `human_character` was missing too, silently, for the same reason.

WHAT THIS TEST BUYS. It cannot reach the database (these are pure unit tests), so
it cannot compare the two lists directly. What it CAN do is fail the moment
somebody adds a new mode constant, and say — in the failure message — that the
constraint has to be widened by hand. That converts a 500 nobody sees until a
user clicks the button into a red test at the moment the constant is written.

IF YOU ARE HERE BECAUSE THIS TEST FAILED: add your new mode to `WRITTEN_MODES`
below, and then widen the constraint on the real database:

    ALTER TABLE yas_generations DROP CONSTRAINT yas_generations_mode_check;
    ALTER TABLE yas_generations ADD CONSTRAINT yas_generations_mode_check
      CHECK (mode = ANY (ARRAY[ ...every mode, including yours... ]::text[]));

Run it as ONE transaction so the table is never left unguarded, and check it
first by executing it inside a transaction you then ROLL BACK.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.routers import yas_producer as yp  # noqa: E402
from app.services import yas_talking_head as th1  # noqa: E402
from app.services import yas_talking_head_v2 as th2  # noqa: E402
from app.services import yas_video_editor as ve  # noqa: E402


# Every value this codebase can put in `yas_generations.mode`, and therefore
# every value the database's CHECK must accept. Applied to the live database on
# 2026-08-26; keep the two in step by hand, because nothing else does.
WRITTEN_MODES = {
    # Posters
    "style_inspired",     # Create Poster            (poster_pipeline.POSTER_MODE)
    "template_fill",      # Poster + Human, template + portrait
    "poster_human",       # Poster + Human, style inspired
    "human_character",    # Human Character
    # Talking Head v1
    "talking_head_preview",
    "talking_head_intro",
    "talking_head_outro",
    "talking_head_scene",
    # Talking Head v2
    "talking_head_v2_preview",
    "talking_head_v2_intro",
    "talking_head_v2_outro",
    "talking_head_v2_scene",
    # Video editor
    "video_edit_preview",
}


def _constants() -> dict[str, str]:
    """The mode constants as the modules actually define them, so this test
    tracks the code rather than a copy of it."""
    return {
        "poster_pipeline.POSTER_MODE": yp.POSTER_MODE,
        "yas_producer.HUMAN_CHARACTER_MODE": yp.HUMAN_CHARACTER_MODE,
        "th1.MODE_PREVIEW": th1.MODE_PREVIEW,
        "th1.MODE_INTRO": th1.MODE_INTRO,
        "th1.MODE_OUTRO": th1.MODE_OUTRO,
        "th1.MODE_SCENE": th1.MODE_SCENE,
        "th2.MODE_PREVIEW": th2.MODE_PREVIEW,
        "th2.MODE_INTRO": th2.MODE_INTRO,
        "th2.MODE_OUTRO": th2.MODE_OUTRO,
        "th2.MODE_SCENE": th2.MODE_SCENE,
        "ve.MODE_PREVIEW": ve.MODE_PREVIEW,
    }


def test_every_mode_constant_is_one_the_database_accepts():
    """The guard. A new constant that nobody added to the constraint fails HERE,
    loudly, instead of as a 500 the first time somebody clicks the button."""
    unknown = {
        name: value for name, value in _constants().items()
        if value not in WRITTEN_MODES
    }
    assert not unknown, (
        f"mode constant(s) not in WRITTEN_MODES: {unknown}. "
        "Add them here AND widen yas_generations_mode_check on the database — "
        "read this file's docstring before you do."
    )


def test_the_two_poster_human_paths_write_different_modes():
    """The specific drift that caused the outage, pinned so it cannot silently
    come back: the two Poster + Human styles are separate endpoints writing
    separate modes, and BOTH have to be in the constraint. Template + portrait
    worked for months purely because its mode happened to be listed."""
    assert "poster_human" in WRITTEN_MODES     # style inspired
    assert "template_fill" in WRITTEN_MODES    # template + portrait
    assert yp.POSTER_MODE == "style_inspired"  # the plain Create Poster tool
    # …and the three are genuinely distinct, so none of them can be collapsed
    # into another without changing what the Library and the Dashboard count.
    assert len({"poster_human", "template_fill", yp.POSTER_MODE}) == 3


def test_the_poster_human_insert_still_writes_the_mode_this_file_claims():
    """Reads the source rather than the database, because the value that broke
    production was a string literal inside an INSERT — not a constant anybody
    could have grepped for from here."""
    import inspect

    src = inspect.getsource(yp._render_ph_variant)
    assert '"poster_human"' in src, (
        "_render_ph_variant no longer writes 'poster_human'. If the mode was "
        "renamed, update WRITTEN_MODES and the database constraint together."
    )


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
