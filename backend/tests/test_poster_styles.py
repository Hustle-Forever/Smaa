"""
Tests for the Create-Poster style registry (poster_styles).

The registry is pure string assembly with no DB/network/credentials at import
time, so this suite runs standalone — no pytest, no .env, no Postgres:

    cd backend && python tests/test_poster_styles.py

It is ALSO pytest-compatible (every check is a ``test_*`` function). The
``__main__`` block runs all checks, prints PASS/FAIL per test, and exits non-zero
on any failure.

What it guards:
  • The registry exposes exactly voxel / voxel_classic / pattern / popup,
    default = voxel.
  • VOXEL IS BY IDENTITY: its preset references the live poster_prompts objects,
    so whatever that module says is what Create Poster renders.
  • VOXEL CLASSIC IS THE FROZEN v1 TEXT: it still carries the scene recipe (the
    prop universe and the staged diorama) that the live prompt dropped, which is
    the whole point of keeping it pickable next to voxel.
  • get_poster_style falls back to voxel for unknown / None / legacy keys and is
    case-insensitive.
  • Pattern & Pop-up carry the diversity mandate, the strict-JSON contract, the
    full brand palette, no-people, and English-only.
  • THE CORE ASK: a poster carries the FULL message — Pop-up's text contract
    references CTA / DATE_TIME / DETAILS (more than a title-card headline) and
    puts the CTA in a button inside the window.
  • No voxel bleed: the pattern/popup prompts never say "voxel" / "toy diorama".
  • ONE PATTERN, ONE COPY: the Arabic-letters pattern is gone from every preset,
    and the map is described as the city (and as a single object that never
    repeats), which is what stopped two maps crossing in one poster.
"""
import sys
from pathlib import Path

# Make ``import app.services...`` resolve regardless of the current directory:
# tests/ -> backend/ is parents[1].
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.poster_prompts import (  # noqa: E402
    POSTER_CONCEPTS_SYSTEM,
    GEMINI_STAGE2_PROMPT,
    VARIATION_NUDGES,
)
from app.services.poster_prompts_classic import (  # noqa: E402
    CLASSIC_CONCEPTS_SYSTEM,
    CLASSIC_GEMINI_STAGE2_PROMPT,
)
from app.services.poster_styles import (  # noqa: E402
    PosterStyle,
    POSTER_STYLES,
    POSTER_STYLE_KEYS,
    DEFAULT_POSTER_STYLE,
    POSTER_BRAND_LAW,
    POSTER_TEXT_CONTRACT,
    get_poster_style,
)

_PALETTE_HEXES = ("#00EBFF", "#730FFF", "#FF5A3C", "#414141", "#FFFFFF")
_NON_VOXEL = ("pattern", "popup")


def _all_text(style: PosterStyle) -> str:
    """Every prompt surface of a preset, concatenated (for content scans)."""
    return "\n".join((
        style.concepts_system,
        style.gemini_stage2_system,
    ))


# ---------------------------------------------------------------------------
# 1. Registry shape
# ---------------------------------------------------------------------------

def test_registry_has_four_styles():
    assert set(POSTER_STYLES) == {"voxel", "voxel_classic", "pattern", "popup"}
    assert POSTER_STYLE_KEYS == tuple(POSTER_STYLES.keys())
    assert DEFAULT_POSTER_STYLE == "voxel"


def test_every_preset_is_well_formed():
    for key, style in POSTER_STYLES.items():
        assert isinstance(style, PosterStyle)
        assert style.key == key
        assert style.label and style.description
        assert style.concepts_system and style.gemini_stage2_system
        assert len(style.variation_nudges) >= 4
        assert len(set(style.variation_nudges)) == len(style.variation_nudges), \
            f"{key}: variation nudges must be distinct"


# ---------------------------------------------------------------------------
# 2. Voxel byte-identity — guarantees zero regression to the v1 pipeline
# ---------------------------------------------------------------------------

def test_voxel_is_byte_identical():
    v = POSTER_STYLES["voxel"]
    assert v.concepts_system is POSTER_CONCEPTS_SYSTEM
    assert v.gemini_stage2_system is GEMINI_STAGE2_PROMPT
    assert tuple(v.variation_nudges) == tuple(VARIATION_NUDGES)


def test_voxel_classic_is_the_frozen_v1_text():
    c = POSTER_STYLES["voxel_classic"]
    assert c.concepts_system is CLASSIC_CONCEPTS_SYSTEM
    assert c.gemini_stage2_system is CLASSIC_GEMINI_STAGE2_PROMPT
    assert tuple(c.variation_nudges) == tuple(VARIATION_NUDGES)


def test_the_two_voxel_presets_are_actually_different():
    """The recipe the live prompt dropped is exactly what classic still has.

    If this ever passes trivially (both carrying the recipe, or neither), the
    toggle in Create Poster has nothing to toggle."""
    live = (POSTER_CONCEPTS_SYSTEM + GEMINI_STAGE2_PROMPT).lower()
    classic = (CLASSIC_CONCEPTS_SYSTEM + CLASSIC_GEMINI_STAGE2_PROMPT).lower()
    assert live != classic
    for term in ("arcade", "keyboard", "diorama", "isometric"):
        assert term in classic, f"classic lost the v1 recipe: {term!r}"
        assert term not in live, f"the recipe came back into the live prompt: {term!r}"
    # Both are still voxel, in the same palette.
    for blob in (live, classic):
        assert "voxel" in blob
    for hexv in _PALETTE_HEXES:
        assert hexv in CLASSIC_CONCEPTS_SYSTEM and hexv in POSTER_CONCEPTS_SYSTEM


# ---------------------------------------------------------------------------
# 3. get_poster_style — safe fallback + case-insensitive
# ---------------------------------------------------------------------------

def test_get_style_fallback_to_voxel():
    for bad in (None, "", "   ", "wireframe", "nonsense", "VOXELLY"):
        assert get_poster_style(bad).key == "voxel"


def test_get_style_is_case_insensitive_and_trimmed():
    assert get_poster_style("VOXEL").key == "voxel"
    assert get_poster_style("Pattern").key == "pattern"
    assert get_poster_style("  PopUp  ").key == "popup"


# ---------------------------------------------------------------------------
# 4. Pattern & Pop-up concepts systems — diversity + JSON + brand
# ---------------------------------------------------------------------------

def test_non_voxel_concepts_have_diversity_and_json_contract():
    for key in _NON_VOXEL:
        sys_prompt = POSTER_STYLES[key].concepts_system
        assert "[DIVERSITY MANDATE" in sys_prompt, f"{key}: missing diversity mandate"
        assert '"concepts"' in sys_prompt, f"{key}: missing JSON concepts contract"
        assert '"summary"' in sys_prompt and '"prompt"' in sys_prompt
        assert "[TEXT TO RENDER]" in sys_prompt, f"{key}: missing text-to-render block"


def test_non_voxel_brand_palette_and_rules_present():
    for key in _NON_VOXEL:
        blob = _all_text(POSTER_STYLES[key])
        low = blob.lower()
        for hexv in _PALETTE_HEXES:
            assert hexv in blob, f"{key}: palette colour {hexv} missing"
        assert "no people" in low, f"{key}: missing no-people rule"
        assert "english only" in low, f"{key}: missing English-only rule"
        assert "one pattern" in low or "never combine" in low, f"{key}: missing one-pattern rule"


# ---------------------------------------------------------------------------
# 5. THE CORE ASK — a poster carries the full message (info-rich text)
# ---------------------------------------------------------------------------

def test_text_contract_is_info_rich():
    # Shared contract must enumerate the full poster fieldset, not just a headline.
    for field in ("HEADLINE", "SUBHEAD", "DATE_TIME", "LOCATION", "DETAILS", "CTA"):
        assert field in POSTER_TEXT_CONTRACT, f"text contract missing field {field}"
    assert "POSTER" in POSTER_TEXT_CONTRACT.upper()


def test_popup_carries_more_than_a_headline():
    sys_prompt = POSTER_STYLES["popup"].concepts_system
    for field in ("CTA", "DATE_TIME", "DETAILS"):
        assert field in sys_prompt, f"popup concepts missing {field}"
    low = sys_prompt.lower()
    # The CTA must be an actual button inside/around the OS window — the user's ask.
    assert "cta button" in low or "button" in low, "popup must place the CTA in a button"
    assert "●●●" in sys_prompt and "<tag/>" in sys_prompt, "popup must use the OS-window device"


def test_pattern_is_full_bleed_pattern_style():
    sys_prompt = POSTER_STYLES["pattern"].concepts_system.lower()
    assert "full-bleed" in sys_prompt
    assert "ghutra" in sys_prompt and "map" in sys_prompt
    # Info still rendered — pattern posters carry the message on blocks.
    assert "cta" in sys_prompt


# ---------------------------------------------------------------------------
# 6. Stage-2 Gemini render system — logo firewall
# ---------------------------------------------------------------------------

def test_gemini_stage2_has_logo_firewall():
    for key in _NON_VOXEL:
        g = POSTER_STYLES[key].gemini_stage2_system
        assert "42_logo_white.png" in g, f"{key}: Gemini system must replicate the real logo"
        assert "LOGO FIREWALL" in g
        assert "only the strings inside the quotes" in g.lower(), \
            f"{key}: must print only quoted strings, never labels"
        assert "poster" in g.lower(), f"{key}: must be an Instagram poster prompt"


# ---------------------------------------------------------------------------
# 7. No voxel bleed — pattern/popup are flat graphic styles
# ---------------------------------------------------------------------------

def test_non_voxel_styles_have_no_voxel_bleed():
    for key in _NON_VOXEL:
        low = _all_text(POSTER_STYLES[key]).lower()
        assert "voxel" not in low, f"{key}: 'voxel' leaked into a flat-graphic style"
        assert "toy diorama" not in low, f"{key}: 'toy diorama' leaked into a flat-graphic style"


# ---------------------------------------------------------------------------
# 8. One pattern, one copy of it
# ---------------------------------------------------------------------------

def test_arabic_letter_pattern_is_gone_from_every_preset():
    """The brand's third statement pattern is no longer offered anywhere.

    Only the pattern is gone: the ENGLISH-ONLY rule (never RENDER Arabic script,
    because image models mangle it) must survive, so this checks the pattern
    wording, not the word "arabic"."""
    for key, style in POSTER_STYLES.items():
        low = _all_text(style).lower()
        for term in ("arabic letter", "arabic-letter", "arabic letters"):
            assert term not in low, f"{key}: the Arabic-letters pattern came back"


def test_pattern_preset_allows_one_pattern_and_one_copy():
    """Two maps crossing in one poster was never banned: "NEVER combine patterns"
    forbids mixing Ghutra WITH Map, and says nothing about a second map. Both the
    concept brain and the renderer now have to say one copy."""
    for surface in (POSTER_STYLES["pattern"].concepts_system,
                    POSTER_STYLES["pattern"].gemini_stage2_system):
        low = surface.lower()
        assert "one copy" in low, "the one-copy rule is missing"
        assert "ghutra" in low and "map" in low


def test_the_map_is_a_map_of_abu_dhabi():
    """It is the city, not a texture of lines, which is the other half of the ask."""
    low = (POSTER_BRAND_LAW + POSTER_STYLES["pattern"].gemini_stage2_system).lower()
    assert "abu dhabi" in low
    assert sum(w in low for w in ("coastline", "corniche", "island", "streets")) >= 3, \
        "the map must be described as the real geography, not as abstract line-work"


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
