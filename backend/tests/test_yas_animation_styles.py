"""
Tests for the Create-Animation style-preset registry (yas_animation_styles).

The prompt modules are pure string assembly with no DB/network/credentials at
import time, so this suite runs standalone — no pytest, no .env, no Postgres:

    cd backend && python tests/test_yas_animation_styles.py

It is ALSO pytest-compatible (every check is a ``test_*`` function), so if a
runner is ever added it picks these up automatically. The ``__main__`` block
runs all checks, prints a PASS/FAIL line per test, and exits non-zero on any
failure.

What it guards:
  • The voxel preset is BYTE-IDENTICAL to v1 (same objects + same output).
  • Every non-voxel preset embeds the shared ANIMATION_COMMON_POLICY.
  • The common policy actually encodes the 42 brand law (palette + DON'Ts).
  • The Stage-1 user-message builders carry the brief + aspect + layout directive.
  • The motion dispatcher routes voxel to the original fn and never leaks the
    word "voxel" into the other presets.
  • Unknown / None / mixed-case keys fall back to voxel safely.
  • The three new presets are genuinely distinct (can't silently collapse).
"""
import sys
from pathlib import Path

# Make ``import app.services...`` resolve regardless of the current directory:
# tests/ -> backend/ is parents[1].
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import yas_animation_prompts as vox  # the untouched voxel module
from app.services.yas_animation_styles import (
    ANIMATION_COMMON_POLICY,
    ANIMATION_STYLES,
    ANIMATION_STYLE_KEYS,
    DEFAULT_STYLE,
    build_motion_prompt_for_style,
    get_animation_style,
)

_NEW_KEYS = ("popup", "pattern")
_PALETTE_HEXES = ("#00EBFF", "#730FFF", "#FF5A3C", "#414141", "#FFFFFF")


# ---------------------------------------------------------------------------
# 1. Registry shape
# ---------------------------------------------------------------------------

def test_registry_keys_and_default():
    # "asis" is the fourth and is NOT a look: it is stamped on pictures the user
    # brought in, where we refuse to assume one. See test_animate_any_image.py.
    assert ANIMATION_STYLE_KEYS == ("voxel", "popup", "pattern", "asis")
    assert set(ANIMATION_STYLES) == {"voxel", "popup", "pattern", "asis"}
    assert len(ANIMATION_STYLES) == 4
    assert DEFAULT_STYLE == "voxel"
    # Every row's .key matches its dict key.
    for key, style in ANIMATION_STYLES.items():
        assert style.key == key
        assert style.label and style.description


# ---------------------------------------------------------------------------
# 2. Voxel byte-identity vs the original v1 symbols
# ---------------------------------------------------------------------------

def test_voxel_is_byte_identical_to_v1():
    voxel = ANIMATION_STYLES["voxel"]
    # Same object identity — not a copy.
    assert voxel.stage1_system is vox.ANIMATION_IMAGE_SYSTEM
    assert voxel.stage2_system is vox.ANIMATION_RENDER_SYSTEM
    assert voxel.user_message_builder is vox.build_animation_image_user_message
    assert voxel.layout_directive is None
    assert voxel.motion_clause is None
    # Output equality through the registry, for both aspects.
    for aspect in ("9:16", "16:9"):
        assert voxel.user_message_builder("Career launch", aspect) == \
            vox.build_animation_image_user_message("Career launch", aspect)


# ---------------------------------------------------------------------------
# 3. Common policy is embedded in every non-voxel preset (and NOT in voxel)
# ---------------------------------------------------------------------------

def test_common_policy_embedded_in_new_presets():
    for key in _NEW_KEYS:
        style = ANIMATION_STYLES[key]
        assert ANIMATION_COMMON_POLICY in style.stage1_system, f"{key} stage1 missing common policy"
        assert ANIMATION_COMMON_POLICY in style.stage2_system, f"{key} stage2 missing common policy"


def test_voxel_does_not_embed_common_policy():
    # Voxel predates the shared block and must remain untouched.
    voxel = ANIMATION_STYLES["voxel"]
    assert ANIMATION_COMMON_POLICY not in voxel.stage1_system
    assert ANIMATION_COMMON_POLICY not in voxel.stage2_system


# ---------------------------------------------------------------------------
# 4. Common policy content encodes the brand law
# ---------------------------------------------------------------------------

def test_common_policy_has_full_palette():
    for hex_code in _PALETTE_HEXES:
        assert hex_code in ANIMATION_COMMON_POLICY, f"missing {hex_code}"


def test_common_policy_has_key_donts():
    for needle in (
        "NEVER outline text",
        "NEVER combine two",
        "NO people",
        "ENGLISH ONLY",
        "glitch",
        "MAXIMUM of TWO dominant colors",
    ):
        assert needle in ANIMATION_COMMON_POLICY, f"common policy missing: {needle!r}"


# ---------------------------------------------------------------------------
# 5. [TEXT TO RENDER] trailer preserved on every preset's Stage-1
# ---------------------------------------------------------------------------

def test_text_to_render_block_present():
    for key, style in ANIMATION_STYLES.items():
        assert "[TEXT TO RENDER]" in style.stage1_system, f"{key} missing [TEXT TO RENDER]"
        assert "TEXT_LINE_1:" in style.stage1_system, f"{key} missing TEXT_LINE_1"


# ---------------------------------------------------------------------------
# 6. Stage-1 user builders include brief + aspect + layout directive
# ---------------------------------------------------------------------------

def test_user_builder_includes_brief_aspect_and_directive():
    brief = "MY UNIQUE BRIEF 12345"
    for key in _NEW_KEYS:
        style = ANIMATION_STYLES[key]
        for aspect in ("9:16", "16:9"):
            out = style.user_message_builder(brief, aspect)
            assert brief in out, f"{key}/{aspect}: brief dropped"
            assert aspect in out, f"{key}/{aspect}: aspect missing"
            assert style.layout_directive in out, f"{key}/{aspect}: layout directive missing"


# ---------------------------------------------------------------------------
# 7. Motion dispatcher
# ---------------------------------------------------------------------------

def test_motion_voxel_identical_to_original():
    brief = "career launch series intro"
    for with_audio in (True, False):
        assert build_motion_prompt_for_style("voxel", brief, with_audio=with_audio) == \
            vox.build_animation_motion_prompt(brief, with_audio=with_audio)


def test_motion_new_presets_are_clean_and_audio_aware():
    brief = "career launch series intro"
    for key in _NEW_KEYS:
        with_music = build_motion_prompt_for_style(key, brief, with_audio=True)
        silent = build_motion_prompt_for_style(key, brief, with_audio=False)
        assert with_music.strip(), f"{key}: empty motion prompt"
        assert brief in with_music, f"{key}: brief context missing"
        # Non-voxel motion must never mention the voxel look.
        assert "voxel" not in with_music.lower(), f"{key}: leaked 'voxel'"
        assert "voxel" not in silent.lower(), f"{key}: leaked 'voxel' (silent)"
        # Audio toggle is honoured.
        assert "INSTRUMENTAL" in with_music, f"{key}: audio block missing"
        assert "SILENT" in silent, f"{key}: silent block missing"


# ---------------------------------------------------------------------------
# 8. Safe fallback for unknown / None / mixed-case keys
# ---------------------------------------------------------------------------

def test_get_style_fallback():
    assert get_animation_style("nonsense").key == "voxel"
    assert get_animation_style(None).key == "voxel"
    assert get_animation_style("").key == "voxel"
    assert get_animation_style("VOXEL").key == "voxel"      # case-insensitive
    assert get_animation_style("  Pattern ").key == "pattern"  # trims + lowercases
    # The dispatcher inherits the same fallback.
    brief = "x"
    assert build_motion_prompt_for_style("bogus", brief) == \
        vox.build_animation_motion_prompt(brief)


# ---------------------------------------------------------------------------
# 9. The two new presets are genuinely distinct
# ---------------------------------------------------------------------------

def test_new_presets_distinct_with_signatures():
    s1 = {k: ANIMATION_STYLES[k].stage1_system for k in _NEW_KEYS}
    # Pairwise distinct Stage-1 systems.
    seen = list(s1.values())
    assert len(set(seen)) == len(seen), "two presets share a Stage-1 system"
    # Each carries its signature token.
    assert "OS-style windows" in s1["popup"] and "●●●" in s1["popup"]
    assert "full-bleed" in s1["pattern"]
    # Motion clauses are distinct too.
    clauses = [ANIMATION_STYLES[k].motion_clause for k in _NEW_KEYS]
    assert len(set(clauses)) == len(clauses), "two presets share a motion clause"


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
