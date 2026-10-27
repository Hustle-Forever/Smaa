"""TH v2 framing (#56): the prompt side.

How close the camera is used to be one weak "Camera: Medium close-up" line,
written by the script model before any reference was picked, and the image
model copied the framing of whatever was attached: a 1:1 head-and-shoulders
character photo gave an extreme close-up, a wide room photo a tiny person.
Now the user picks Close-up / Above the hip / Full body and:

- every generated still prompt (Cases B, C, D) has that SHOT SIZE block with
  its crop point, lens and head / foot room, a pose for that shot, and an
  attached person reference is told it sets identity, NOT the framing;
- the script's `camera` field is movement only and never sets the shot size;
- Veo gets the same framing: a camera line per framing, full body (and a
  photo's own framing) locked off, with matching negatives;
- a framing change on an existing still is a canned re-shoot instruction
  for the refine engine.

Pure string assembly: no model, no database.
"""
from __future__ import annotations

import inspect

import pytest

from app.services import yas_talking_head_v2_prompts as thp

EM_DASH = chr(0x2014)
HEADINGS = {
    "close": "SHOT SIZE: CLOSE-UP",
    "above_hip": "SHOT SIZE: MEDIUM SHOT",
    "full_body": "SHOT SIZE: FULL-BODY SHOT",
}
SCRIPT = {
    "subject": "an Emirati woman in a black abaya and shayla",
    "action": "calm and composed, facing the camera directly",
    "camera": "wide establishing shot, slow push-in",
    "lighting": "soft daylight",
    "dialogue": "Learning by building real projects changed how I think.",
}


def _prompt(case: str, framing: str | None = None, **kw) -> str:
    args = dict(script=SCRIPT, character_brief=None, bg_brief=None, lock_character=True)
    if case == "B":
        args.update(has_character_ref=True)
    elif case == "B+bg":
        args.update(has_character_ref=True, has_bg_ref=True, bg_brief="A bright lab")
    elif case == "C":
        args.update(has_bg_ref=True, bg_brief="A bright lab")
    elif case == "A":
        args.update(keep_subject=True)
    args.update(kw)
    if framing is not None:
        args["framing"] = framing
    return thp.build_main_keyframe_prompt(**args)


# ---------------------------------------------------------------------------
# The three framings and their names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,want", [
    (None, "close"), ("", "close"), ("garbage", "close"), (42, "close"),
    ("close", "close"), ("Close-up", "close"), ("closeup", "close"), ("CLOSE UP", "close"),
    ("above_hip", "above_hip"), ("Above the hip", "above_hip"), ("waist", "above_hip"),
    ("medium", "above_hip"), ("full_body", "full_body"), ("Full body", "full_body"),
    ("full", "full_body"), ("fullbody", "full_body"), ("head to toe", "full_body"),
])
def test_norm_framing(value, want):
    assert thp.norm_framing(value) == want


def test_unknown_framing_is_none_not_a_guess():
    assert thp.framing_or_none("garbage") is None and thp.framing_or_none(None) is None
    assert thp.veo_framing("as_photo") == "as_photo" and thp.veo_framing("nope") is None


def test_defaults_new_runs_above_the_hip_legacy_close():
    assert thp.DEFAULT_FRAMING == "above_hip"
    assert thp.LEGACY_FRAMING == "close"
    assert set(thp.FRAMING_LABELS) == set(thp.FRAMINGS)
    assert thp.FRAMING_LABELS == {"close": "Close-up", "above_hip": "Above the hip",
                                  "full_body": "Full body"}


# ---------------------------------------------------------------------------
# Still prompts: one SHOT SIZE block, in Cases B, C and D
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["B", "B+bg", "C", "D"])
@pytest.mark.parametrize("framing", ["close", "above_hip", "full_body"])
def test_each_case_has_only_its_own_shot_size(case, framing):
    p = _prompt(case, framing)
    for f, heading in HEADINGS.items():
        assert (heading in p) == (f == framing), f"{case}/{framing}: {heading}"
    assert thp.SHOT_SIZE_BLOCKS[framing] in p
    assert thp.FRAMING_STILL_POSE[framing] in p
    assert "REQUIRED: the SHOT SIZE above exactly" in p


@pytest.mark.parametrize("case", ["B", "C", "D"])
def test_full_body_shows_the_feet_and_close_is_head_and_shoulders(case):
    full = _prompt(case, "full_body")
    assert "soles of the shoes" in full and "Never crop the feet" in full
    close = _prompt(case, "close")
    assert "head and shoulders" in close and "head to toe" not in close
    hip = _prompt(case, "above_hip")
    assert "just above the hips" in hip and "head to toe" not in hip


@pytest.mark.parametrize("case", ["B", "B+bg", "C", "D"])
def test_without_a_framing_the_prompt_is_the_close_one(case):
    assert _prompt(case) == _prompt(case, "close")
    assert _prompt(case, "garbage") == _prompt(case, "close")


@pytest.mark.parametrize("case", ["B", "C", "D"])
@pytest.mark.parametrize("framing", ["close", "above_hip", "full_body"])
def test_the_script_camera_never_sets_the_shot_size(case, framing):
    p = _prompt(case, framing)
    assert "wide establishing shot" not in p
    assert "Camera:" not in p
    other = _prompt(case, framing, script={**SCRIPT, "camera": "Medium close-up / head-and-shoulders"})
    assert other == p


def test_the_person_reference_sets_identity_not_framing_only_when_attached():
    for f in thp.FRAMINGS:
        assert "NOT the framing" in _prompt("B", f)
        assert "NOT the framing" in _prompt("B+bg", f)
        assert "NOT the framing" not in _prompt("C", f)
        assert "NOT the framing" not in _prompt("D", f)
        assert "NOT the framing" not in _prompt("D", f, character_brief="BRIEF: a student")


def test_an_attached_background_sets_the_location_not_the_distance():
    for f in thp.FRAMINGS:
        assert "location only, not the camera" in _prompt("B+bg", f)
        assert "location only, not the camera" in _prompt("C", f)
        assert "location only, not the camera" not in _prompt("B", f)
        assert "location only, not the camera" not in _prompt("D", f)


def test_wider_shots_continue_the_outfit_from_a_person_reference():
    assert "continue the SAME garments" not in _prompt("B", "close")
    assert "continue the SAME garments" in _prompt("B", "above_hip")
    assert "continue the SAME garments" in _prompt("B", "full_body")
    assert "continue the SAME garments" not in _prompt("D", "full_body"), "no reference: nothing to continue"


def test_the_identity_anchor_keeps_its_clause_and_gets_the_shot_size():
    p = _prompt("B", "full_body", lock_character=False, identity_anchor=True)
    assert "IDENTITY ANCHOR" in p and HEADINGS["full_body"] in p and "NOT the framing" in p


def test_keep_subject_keeps_its_canvas_framing():
    base = _prompt("A")
    for f in thp.FRAMINGS:
        p = _prompt("A", f)
        assert p == base, "Case A has no framing: the canvas governs it"
        assert "SHOT SIZE" not in p


def test_the_phantom_fix_still_holds_with_a_framing():
    for f in thp.FRAMINGS:
        p = _prompt("D", f)
        assert "FACE-AND-OUTFIT LOCK" not in p and "attached PERSON reference" not in p
        assert SCRIPT["subject"] in p


# ---------------------------------------------------------------------------
# The script's `camera` field: movement only
# ---------------------------------------------------------------------------

def test_the_camera_field_is_movement_only():
    desc = thp.TALKING_HEAD_SCRIPT_SCHEMA["properties"]["camera"]["description"]
    assert "MOVEMENT only" in desc
    assert "Medium close-up" not in desc and "avoid extreme" not in desc
    assert "Never the shot size" in desc
    assert "camera" in thp.TALKING_HEAD_SCRIPT_SCHEMA["required"]


# ---------------------------------------------------------------------------
# Veo: the camera line, anchor and negatives follow the still's framing
# ---------------------------------------------------------------------------

def _veo(framing=None, **script) -> str:
    return thp.build_main_veo_prompt(
        script={**SCRIPT, **script}, brand_req={}, has_character_ref=True, has_bg_ref=False,
        dialogue_language="en", framing=framing,
    )


def test_full_body_clip_is_locked_off_with_no_push_in():
    p = _veo("full_body")
    assert thp.VEO_CAMERA_LINES["full_body"] in p
    assert "locked off" in p and "including the feet" in p
    assert "push-in (" not in p and "dolly-in" not in p and "slow, steady push-in" not in p
    assert "the camera holds still" in p
    assert "MOVEMENT in the shot comes from the CAMERA" not in p


def test_close_clip_is_a_close_up_with_at_most_a_slight_push_in():
    p = _veo("close")
    assert "Camera: a close-up, head and shoulders" in p
    assert "very slight" in p and "never cutting off the top of the head" in p


def test_above_hip_clip_mentions_the_hips_and_a_gentle_push_in():
    p = _veo("above_hip")
    assert "from just above the hips up" in p and "no tighter than chest-up" in p


def test_a_photo_framing_clip_holds_the_first_frame():
    p = _veo("as_photo")
    assert thp.VEO_CAMERA_LINES["as_photo"] in p and "holds still" in p


@pytest.mark.parametrize("framing", ["close", "above_hip", "full_body", "as_photo"])
def test_the_veo_camera_line_ignores_the_script_camera(framing):
    p = _veo(framing)
    assert "wide establishing shot" not in p
    assert p == _veo(framing, camera="Medium close-up")
    assert thp.scrub_rai_terms(p) == p
    assert p.count("Camera:") == 1


def test_without_a_framing_the_veo_prompt_is_unchanged():
    p = _veo(None)
    assert "Camera: wide establishing shot, slow push-in framing, with a slow, smooth" in p
    assert "Locked-off camera or a slow, steady push-in" in p
    assert "may hold or push in slowly" in p
    assert "MOVEMENT in the shot comes from the CAMERA" in p
    assert inspect.signature(thp.build_main_veo_prompt).parameters["framing"].default is None


def test_the_subject_line_is_kept():
    assert "Subject: the person shown in the first frame" in _veo("full_body")


def test_negatives_per_framing_stay_under_the_cap():
    base = thp.build_talking_head_negative_prompt({})
    assert base == thp.build_talking_head_negative_prompt({}, framing=None)
    assert base == thp.build_talking_head_negative_prompt({}, framing="above_hip")
    full = thp.build_talking_head_negative_prompt({}, framing="full_body")
    assert full.startswith(base) and "cropped feet" in full
    close = thp.build_talking_head_negative_prompt({}, framing="close")
    assert "cropped forehead" in close and "cropped feet" not in close
    long_donts = {"brand_donts": [f"dont number {n} with plenty of words" for n in range(40)]}
    for f in (None, "close", "above_hip", "full_body", "as_photo"):
        neg = thp.build_talking_head_negative_prompt(long_donts, framing=f)
        assert len(neg) <= 960
        assert "dont number 0 with plenty of words" in neg, "the brand list keeps its place"


# ---------------------------------------------------------------------------
# The framing re-shoot instruction (the refine engine's change)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("framing", ["close", "above_hip", "full_body"])
def test_the_reshoot_change_fits_the_refine_and_keeps_the_mouth_closed(framing):
    change = thp.framing_reshoot_change(framing)
    assert change == thp.FRAMING_RESHOOT_CHANGES[framing]
    assert len(change) <= thp.STILL_REFINE_CHANGE_MAX
    assert thp.clean_still_change(change) == change, "nothing is cut"
    assert not thp.still_refine_changes_expression(change)
    assert "Same person, same outfit" in change and "only the camera distance changes" in change
    directive = thp.build_still_refine_directive(change, aspect="9:16", has_identity_ref=True)
    assert change in directive and "mouth closed" in directive


def test_each_reshoot_names_its_own_shot():
    assert "CLOSE-UP" in thp.framing_reshoot_change("close")
    assert "just above the hips" in thp.framing_reshoot_change("above_hip")
    assert "soles of the shoes" in thp.framing_reshoot_change("full_body")
    assert thp.framing_reshoot_change("garbage") == thp.framing_reshoot_change("close")


def test_no_em_dash_in_the_new_framing_text():
    texts = [
        *thp.SHOT_SIZE_BLOCKS.values(), *thp.FRAMING_STILL_POSE.values(),
        *thp.VEO_CAMERA_LINES.values(), *thp.FRAMING_RESHOOT_CHANGES.values(),
        thp._FRAMING_PERSON_CLAUSE, thp._FRAMING_BG_CLAUSE, thp._FRAMING_OUTFIT_CLAUSE,
        thp.TALKING_HEAD_SCRIPT_SCHEMA["properties"]["camera"]["description"],
        *(t for v in thp.VEO_FRAMING_NEGATIVES.values() for t in v),
    ]
    for t in texts:
        assert EM_DASH not in t, t[:60]
