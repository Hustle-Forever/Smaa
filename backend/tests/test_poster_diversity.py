"""
Tests for the Create-Poster diversity pipeline (poster_prompts).

The prompt module is pure string/JSON assembly with no DB/network/credentials at
import time, so this suite runs standalone — no pytest, no .env, no Postgres:

    cd backend && python tests/test_poster_diversity.py

It is ALSO pytest-compatible (every check is a ``test_*`` function). The
``__main__`` block runs all checks, prints PASS/FAIL per test, and exits non-zero
on any failure.

What it guards:
  • The hardcoded literal examples that made every "Piscine" poster the same pool
    are GONE (regression guard — they must never come back).
  • The brand guardrails the user wants kept ARE retained (what voxel art is, the
    strict 5-colour palette, the [TEXT TO RENDER] contract).
  • The SCENE RECIPE stays gone: no named prop, no house camera, no staging
    device in either stage's system prompt (the joystick-on-a-slab regression).
  • The diversity mandate + strict-JSON contract are present in the system prompt.
  • build_concepts_user_message embeds the topic, the count, and each recent
    summary (the cross-batch anti-repeat avoid-list).
  • parse_concepts is robust: valid JSON, fenced JSON, prose-wrapped JSON, a bare
    list, alt key names — and NEVER raises (garbage -> []). Short lists pass
    through unchanged so the router can top up.
  • dedupe_concepts drops near-duplicate summaries (case/punctuation-insensitive).
  • The Explore variation surface: ≥4 distinct nudges, a parent-image instruction,
    and a builder that preserves the base prompt + injects exactly one nudge.
"""
import sys
from pathlib import Path

# Make ``import app.services...`` resolve regardless of the current directory:
# tests/ -> backend/ is parents[1].
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.poster_prompts import (  # noqa: E402
    POSTER_CONCEPT_COUNT,
    POSTER_CONCEPTS_SYSTEM,
    VOXEL_SCENE_RULES,
    GEMINI_STAGE2_PROMPT,
    VARIATION_REF_INSTRUCTION,
    VARIATION_NUDGES,
    build_concepts_user_message,
    build_variation_user_message,
    parse_concepts,
    dedupe_concepts,
)
from app.services.poster_logo import composite_logo_png  # noqa: E402

_PALETTE_HEXES = ("#00EBFF", "#730FFF", "#FF5A3C", "#414141", "#FFFFFF")

# Round one: the literal examples that anchored the model to one idea per topic.
_BANNED_HARDCODES = (
    "piscine",
    "swimming pool",
    "arcade cabinet",
    "logic game",
    "floating cursor",
    "question mark",
    "rubber duck",
)

# Round two: the scene RECIPE. Naming a universe of props ("arcade machines,
# mechanical keyboards, retro controllers") and a staging device (a "toy diorama
# photographed in a studio" on a 3/4 isometric camera) meant every topic came back
# as a joystick or a keyboard on a raised slab in an empty frame. The prompts now
# define voxel art and the palette and leave the picture to Stage 1, so none of
# these words belongs in either system prompt.
_BANNED_RECIPE = (
    "joystick",
    "arcade",
    "keyboard",
    "controller",
    "crt",
    "diorama",
    "isometric",
    "gaming",
    "pixel-art",
    "terminal",
    "plinth",
    "pedestal",
)


def _concepts_json(*pairs: tuple[str, str]) -> str:
    import json
    return json.dumps({"concepts": [{"summary": s, "prompt": p} for s, p in pairs]})


# ---------------------------------------------------------------------------
# 1. De-hardcoding regression guard — the whole reason this change exists
# ---------------------------------------------------------------------------

def test_no_hardcoded_literal_examples():
    blob = (POSTER_CONCEPTS_SYSTEM + "\n" + VOXEL_SCENE_RULES).lower()
    for term in _BANNED_HARDCODES:
        assert term not in blob, f"hardcoded literal leaked back into the prompt: {term!r}"


def test_user_message_has_no_hardcoded_examples():
    msg = build_concepts_user_message("Piscine applications open", 4, []).lower()
    # The topic word itself is allowed (it's the user's topic); the BAKED examples are not.
    for term in ("swimming pool", "arcade cabinet", "floating cursor", "question mark"):
        assert term not in msg


# ---------------------------------------------------------------------------
# 2. Brand guardrails retained
# ---------------------------------------------------------------------------

def test_brand_guardrails_retained():
    rules = VOXEL_SCENE_RULES
    # What survives is the style itself (cubes), the palette, and the text contract.
    assert "voxel" in rules.lower()
    assert "cub" in rules.lower(), "the rules must still say what voxel art is made of"
    assert "[TEXT TO RENDER]" in rules
    for hexv in _PALETTE_HEXES:
        assert hexv in rules, f"palette colour {hexv} missing from scene rules"


def test_no_scene_recipe_in_either_stage():
    """Neither system prompt may name a prop, a camera or a staging device.

    This is the joystick-on-a-slab guard. A word added back here is a rule every
    future poster obeys, which is exactly what made every topic look the same."""
    blob = "\n".join((POSTER_CONCEPTS_SYSTEM, VOXEL_SCENE_RULES, GEMINI_STAGE2_PROMPT)).lower()
    for term in _BANNED_RECIPE:
        assert term not in blob, f"scene recipe leaked back into the prompt: {term!r}"


def test_stage2_defers_to_the_scene_description():
    """Stage 2 renders what Stage 1 invented; it does not bring a look of its own."""
    low = GEMINI_STAGE2_PROMPT.lower()
    assert "scene description" in low
    assert "voxel" in low
    for hexv in _PALETTE_HEXES:
        assert hexv in GEMINI_STAGE2_PROMPT, f"palette colour {hexv} missing from stage 2"


def test_stage2_prompt_intact():
    # Stage 2 renders an Instagram poster, English-only, logo firewall. Aspect is
    # now set per-call by the render directive (so the Create-Animation frame can
    # reuse this same system prompt at 9:16 / 16:9), not hardcoded in the prompt.
    assert "poster" in GEMINI_STAGE2_PROMPT.lower()
    assert "[TEXT TO RENDER]" in GEMINI_STAGE2_PROMPT
    assert "42_logo_white.png" in GEMINI_STAGE2_PROMPT


# ---------------------------------------------------------------------------
# 3. Diversity mandate + JSON contract
# ---------------------------------------------------------------------------

def test_diversity_mandate_present():
    sysp = POSTER_CONCEPTS_SYSTEM
    assert "DIVERSITY MANDATE" in sysp
    # The explicit axes the N concepts must differ on.
    for axis in ("HERO SUBJECT", "INTERPRETATION ANGLE", "COMPOSITION", "PALETTE", "CAMERA"):
        assert axis in sysp, f"diversity axis missing: {axis}"


def test_json_contract_present():
    sysp = POSTER_CONCEPTS_SYSTEM
    assert '"concepts"' in sysp
    assert '"summary"' in sysp and '"prompt"' in sysp
    assert "STRICT JSON" in sysp


def test_concept_count_default():
    assert POSTER_CONCEPT_COUNT == 4


# ---------------------------------------------------------------------------
# 4. build_concepts_user_message — topic + count + avoid-list
# ---------------------------------------------------------------------------

def test_user_message_embeds_topic_count_and_avoidlist():
    recents = ["voxel rocket launch, coral/charcoal", "isometric campus map, cyan"]
    msg = build_concepts_user_message("Career Launch night", 4, recents)
    assert "Career Launch night" in msg
    assert "4" in msg
    for r in recents:
        assert r in msg, "recent summary not fed into the avoid-list"


def test_user_message_handles_empty_history():
    msg = build_concepts_user_message("Hackathon", 4, [])
    assert "Hackathon" in msg
    assert "none" in msg.lower()  # "(none yet)" placeholder


# ---------------------------------------------------------------------------
# 5. parse_concepts — robust + never raises
# ---------------------------------------------------------------------------

def test_parse_valid_object():
    raw = _concepts_json(("a hero", "P1\n[TEXT TO RENDER]"), ("b hero", "P2"))
    out = parse_concepts(raw, 4)
    assert len(out) == 2
    assert out[0] == {"summary": "a hero", "prompt": "P1\n[TEXT TO RENDER]"}


def test_parse_caps_at_n():
    raw = _concepts_json(*[(f"s{i}", f"p{i}") for i in range(10)])
    assert len(parse_concepts(raw, 4)) == 4


def test_parse_fenced_json():
    raw = "```json\n" + _concepts_json(("x", "PX")) + "\n```"
    out = parse_concepts(raw, 4)
    assert len(out) == 1 and out[0]["prompt"] == "PX"


def test_parse_prose_wrapped_json():
    raw = "Sure! Here are your concepts:\n" + _concepts_json(("y", "PY")) + "\nHope that helps."
    out = parse_concepts(raw, 4)
    assert len(out) == 1 and out[0]["summary"] == "y"


def test_parse_bare_list():
    import json
    raw = json.dumps([{"summary": "z", "prompt": "PZ"}])
    out = parse_concepts(raw, 4)
    assert len(out) == 1 and out[0]["prompt"] == "PZ"


def test_parse_alt_keys_and_missing_summary():
    import json
    raw = json.dumps({"concepts": [{"scene": "only a scene, no summary"}]})
    out = parse_concepts(raw, 4)
    assert len(out) == 1
    assert out[0]["prompt"] == "only a scene, no summary"
    assert out[0]["summary"]  # derived from the prompt's first line


def test_parse_skips_entries_without_prompt():
    import json
    raw = json.dumps({"concepts": [{"summary": "no prompt here"}, {"summary": "ok", "prompt": "P"}]})
    out = parse_concepts(raw, 4)
    assert len(out) == 1 and out[0]["prompt"] == "P"


def test_parse_garbage_returns_empty_never_raises():
    for bad in ("", "   ", "not json at all", "{broken", "[", "null", "42"):
        assert parse_concepts(bad, 4) == [], f"expected [] for {bad!r}"


# ---------------------------------------------------------------------------
# 6. dedupe_concepts — drop near-duplicate summaries
# ---------------------------------------------------------------------------

def test_dedupe_exact_and_normalized():
    concepts = [
        {"summary": "Voxel Rocket Launch", "prompt": "P1"},
        {"summary": "voxel rocket launch!!", "prompt": "P2"},   # same after normalize
        {"summary": "Isometric Campus Map", "prompt": "P3"},
    ]
    out = dedupe_concepts(concepts)
    assert len(out) == 2
    assert [c["prompt"] for c in out] == ["P1", "P3"]  # first of each kept


def test_dedupe_keeps_distinct():
    concepts = [{"summary": f"distinct {i}", "prompt": f"P{i}"} for i in range(4)]
    assert len(dedupe_concepts(concepts)) == 4


# ---------------------------------------------------------------------------
# 7. Explore — image-anchored variations
# ---------------------------------------------------------------------------

def test_variation_nudges_distinct_and_enough():
    assert len(VARIATION_NUDGES) >= POSTER_CONCEPT_COUNT
    assert len(set(VARIATION_NUDGES)) == len(VARIATION_NUDGES), "duplicate nudge"


def test_variation_ref_instruction_anchors_on_chosen():
    txt = VARIATION_REF_INSTRUCTION.lower()
    assert "chosen" in txt
    assert "close variation" in txt
    assert "do not redesign" in txt


def test_build_variation_message_preserves_base_and_injects_one_nudge():
    base = "FULL CONCEPT TEXT\n[TEXT TO RENDER]\nTEXT_LINE_1: \"HELLO\""
    nudge = VARIATION_NUDGES[0]
    msg = build_variation_user_message(base, nudge)
    assert base in msg                       # parent concept preserved verbatim
    assert nudge in msg                       # the one directed change
    assert "VARIATION DIRECTIVE" in msg
    # Only the chosen nudge appears, not the others.
    for other in VARIATION_NUDGES[1:]:
        assert other not in msg


# ---------------------------------------------------------------------------
# 8. Logo composite (post-render overlay)
# ---------------------------------------------------------------------------

def _png_bytes(color, size=(256, 256)):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_logo_composite_stamps_top_right():
    import tempfile
    from io import BytesIO
    from pathlib import Path
    from PIL import Image

    scene = _png_bytes((115, 15, 255), (512, 512))  # brand purple
    logo_path = Path(tempfile.mktemp(suffix=".png"))
    Image.new("RGBA", (200, 80), (255, 255, 255, 255)).save(logo_path)  # white logo

    out = composite_logo_png(scene, logo_path, width_frac=0.18, margin_frac=0.04)
    im = Image.open(BytesIO(out)).convert("RGB")
    assert im.size == (512, 512)
    w, _ = im.size
    # A point inside the stamped top-right logo region is white; center untouched.
    logo_w = int(w * 0.18)
    margin = int(w * 0.04)
    inside = (w - margin - logo_w // 2, margin + 10)
    assert im.getpixel(inside) == (255, 255, 255), "logo not stamped top-right"
    assert im.getpixel((w // 2, w // 2)) == (115, 15, 255), "center changed"


def test_logo_composite_missing_logo_returns_scene_png():
    from io import BytesIO
    from pathlib import Path
    from PIL import Image

    scene = _png_bytes((65, 65, 65), (128, 128))
    out = composite_logo_png(scene, Path("/does/not/exist.png"))
    im = Image.open(BytesIO(out)).convert("RGB")  # still a valid PNG
    assert im.size == (128, 128)
    assert im.getpixel((64, 64)) == (65, 65, 65)


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
