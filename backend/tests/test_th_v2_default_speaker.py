"""TH v2 default speaker (#56): an Emirati speaker when no character is picked.

- The script context has no CHARACTER LIBRARY roster: it asked for
  '#<id> <slug>' subjects nothing resolves, and its rows are real students'
  interview frames.
- The script prompts make an Emirati national the default speaker, and a
  deterministic guard (one _finalize_script for generate_script and the
  re-script) sets `subject` to a default cast unless the topic fixes the
  speaker. The default alternates man / woman across runs, respects a gender
  the topic fixes, and is persisted as metadata.speaker.
- The re-script falls back to Gemini (OpenAI has no credits on prod).
- Same person across scenes: a scene >= 1 with no pick takes scene 0's
  current still as its identity anchor (identity only, not framing or
  location), and scene 0 is made first.
- The Veo subject line never carries a library tag.
- The Look step's Speaker field (edit_run speaker_subject) sets the speaker
  and re-shoots the scenes showing it once the run has stills.
- Human Character casts Emirati for open briefs and falls back to Gemini.

No database and no model calls: the run store is the in-memory FakePg of
test_th_v2_scene_stills, and every model call is a fake.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import random
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agents.producer import memory
from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp
from app.services.openai_client import BudgetExceededError
from test_th_v2_bookends import _bookend_md, benv  # noqa: F401
from test_th_v2_scene_stills import PHANTOM, SUBJECT, _md, _norm, _scene, env  # noqa: F401

# Real script subjects from production runs 799, 827, 821/824 and 814.
PROD_TAGS = (
    "#234 person_a_2c28f4",
    "#198 yao_7dc766 (Yao) the person in the attached reference image",
    "#106 person_d_a1e8c3 the person in the attached reference image",
    "#238 person_a_1b8c5e",
)
BRAZIL = "a Brazilian exchange student in his early 20s wearing a grey hoodie"


def _casts(gender: str) -> list[str]:
    return [c["subject"] for c in thp.EMIRATI_DEFAULT_CASTS if c["gender"] == gender]


# ---------------------------------------------------------------------------
# The run store: FakePg plus the two statements this step adds
# ---------------------------------------------------------------------------

@pytest.fixture
def senv(env, monkeypatch):
    """env, plus the SQL of the speaker lookup and the re-script's topic
    write, and no embedding of a new script."""
    db = env.db
    base = db.run_sql

    def run_sql(sql, p):
        s = _norm(sql)
        if s.startswith("SELECT metadata->'speaker'->>'gender' AS gender FROM yas_generations"):
            mode, source = p
            rows = [r for r in db.rows.values()
                    if r["mode"] == mode and ((r["metadata"] or {}).get("speaker") or {}).get("source") == source]
            rows.sort(key=lambda r: r["id"], reverse=True)
            return {"gender": rows[0]["metadata"]["speaker"].get("gender")} if rows else None
        if s.startswith("UPDATE yas_generations SET topic = %s, updated_at = NOW() WHERE id = %s"):
            topic, rid = p
            db.rows[rid]["topic"] = topic
            return None
        return base(sql, p)

    monkeypatch.setattr(db, "run_sql", run_sql)

    async def no_embed(*a, **k):
        return None

    monkeypatch.setattr(th, "_embed_and_store_script", no_embed)
    return env


def _llm(monkeypatch, *, openai=None, gemini=None):
    """Fake the script models. `openai` / `gemini`: a script dict to return,
    or an exception to raise. Returns the list of calls made."""
    calls: list[tuple[str, dict]] = []

    async def openai_chat(prompt, **kw):
        calls.append(("openai", {"prompt": prompt, **kw}))
        if isinstance(openai, BaseException):
            raise openai
        return json.dumps(openai)

    async def gemini_multimodal(prompt, **kw):
        calls.append(("gemini", {"prompt": prompt, **kw}))
        if isinstance(gemini, BaseException):
            raise gemini
        return copy.deepcopy(gemini)

    monkeypatch.setattr(th, "openai_chat", openai_chat)
    monkeypatch.setattr(th, "gemini_multimodal", gemini_multimodal)
    return calls


def _script(subject: str, *, user_specified: bool = False, **kw) -> dict:
    s = {
        "hook": "Peer learning works", "dialogue": "Peer learning works because we teach each other daily.",
        "subject": subject, "speaker_user_specified": user_specified,
        "action": "eyes widen", "camera": "Medium close-up", "lighting": "soft daylight",
        "palette": ["#111111"], "style": "documentary-realism", "mood": "warm sincere",
        "dialogue_language": "en", "intro_headline": "Why peers?", "intro_subtitle": "",
        "outro_headline": "Join us", "outro_subtitle": "",
        "scenes": [{"dialogue": "Peer learning works because we teach each other daily.",
                    "hook": "Peer learning works"}],
    }
    s.update(kw)
    return s


async def _no_inputs(topic, user_dialogue=None):
    return "CONTEXT", None, []


def _gen_script(monkeypatch, topic="Why peer learning works", **kw):
    monkeypatch.setattr(th, "_build_script_inputs", _no_inputs)
    return asyncio.run(th.generate_script(
        topic=topic, refs=th.TalkingHeadRefs(), lock_character=True,
        image_models={}, **kw,
    ))


# ---------------------------------------------------------------------------
# Roster: the CHARACTER LIBRARY never reaches a TH v2 script
# ---------------------------------------------------------------------------

def _roster_row():
    return {"id": 234, "slug": "person_a_2c28f4", "display_name": "Person A",
            "archetype": "emirati_female", "attributes": {}, "usage_count": 0,
            "last_used_at": None, "short_desc": "interview frame"}


def test_context_block_can_leave_the_character_library_out(monkeypatch):
    listed: list[int] = []

    async def chars():
        listed.append(1)
        return [_roster_row()]

    async def brand():
        return {}

    monkeypatch.setattr(memory, "_list_enabled_characters", chars)
    monkeypatch.setattr(memory, "_latest_brand_payload", brand)
    with_roster = asyncio.run(memory.build_context_block(None))
    without = asyncio.run(memory.build_context_block(None, include_character_library=False))
    assert "CHARACTER LIBRARY" in with_roster and "#234 person_a_2c28f4" in with_roster
    assert "CHARACTER LIBRARY" not in without and "#234" not in without
    assert listed == [1], "the roster is not even looked up when it is left out"
    sig = inspect.signature(memory.build_context_block)
    assert sig.parameters["include_character_library"].default is True, "the producer keeps its roster"


def test_th_v2_script_inputs_ask_for_no_roster(monkeypatch):
    seen: list[dict] = []

    async def ctx(*a, **kw):
        seen.append(kw)
        return "CTX"

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    monkeypatch.setattr(th, "build_context_block", ctx)
    monkeypatch.setattr(th, "gemini_embed", empty)
    monkeypatch.setattr(th, "_recent_th_bookends", empty)
    monkeypatch.setattr(th, "_commit_angle", none)
    block, angle, recent = asyncio.run(th._build_script_inputs("Why peer learning works"))
    assert block == "CTX" and seen[0]["include_character_library"] is False


# ---------------------------------------------------------------------------
# Script prompts and schema
# ---------------------------------------------------------------------------

def test_schema_subject_has_the_default_and_no_tag_instruction():
    schema = thp.TALKING_HEAD_SCRIPT_SCHEMA
    desc = schema["properties"]["subject"]["description"]
    assert "Emirati" in desc and "abaya" in desc and "kandura" in desc
    assert "#<id>" not in desc
    assert "the person in the attached reference image'" not in desc
    assert "Never use gendered defaults" not in desc
    flag = schema["properties"]["speaker_user_specified"]
    assert flag["type"] == "boolean"
    # OpenAI strict mode: every property is required, nothing extra.
    assert set(schema["required"]) == set(schema["properties"])
    assert "speaker_user_specified" in schema["required"]


def test_gemini_schema_keeps_the_flag():
    g = th._gemini_safe_schema(thp.TALKING_HEAD_SCRIPT_SCHEMA)
    assert "additionalProperties" not in json.dumps(g)
    assert g["properties"]["speaker_user_specified"]["type"] == "boolean"
    assert "speaker_user_specified" in g["required"]


def test_system_prompt_has_the_default_speaker_rule():
    s = thp.TALKING_HEAD_SCRIPT_SYSTEM
    assert "DEFAULT SPEAKER" in s and "Emirati national" in s
    assert "shayla" in s and "fully visible face" in s and "no niqab" in s
    assert "speaker_user_specified" in s
    assert "'#<id> ' tag" not in s and "Veo's reference pipeline" not in s
    assert "Never use gendered defaults" not in s


def test_user_prompt_default_speaker_block_only_without_a_character():
    base = dict(topic="Why peer learning works", context_block="CTX", bg_brief=None,
                intro_brief=None, outro_brief=None)
    no_char = thp.build_script_user_prompt(character_brief=None, **base)
    assert thp.DEFAULT_SPEAKER_BLOCK in no_char and "CHARACTER LOCK" not in no_char
    with_char = thp.build_script_user_prompt(character_brief="#220 person_b, archetype: emirati_male", **base)
    assert "CHARACTER LOCK IS ACTIVE" in with_char and thp.DEFAULT_SPEAKER_BLOCK not in with_char


def test_new_prompt_text_has_no_em_dash():
    dash = chr(0x2014)
    texts = [thp.DEFAULT_SPEAKER_BLOCK, thp.DEFAULT_SPEAKER_FALLBACK, thp.PERSON_IN_FIRST_FRAME,
             thp.TALKING_HEAD_SCRIPT_SCHEMA["properties"]["subject"]["description"],
             thp.TALKING_HEAD_SCRIPT_SCHEMA["properties"]["speaker_user_specified"]["description"]]
    texts += [c["subject"] for c in thp.EMIRATI_DEFAULT_CASTS]
    start = thp.TALKING_HEAD_SCRIPT_SYSTEM.index("DEFAULT SPEAKER")
    texts.append(thp.TALKING_HEAD_SCRIPT_SYSTEM[start:start + 1200])
    assert not [t for t in texts if dash in t]


# ---------------------------------------------------------------------------
# The casts and the pure helpers
# ---------------------------------------------------------------------------

def test_emirati_default_casts_are_respectful_and_balanced():
    casts = thp.EMIRATI_DEFAULT_CASTS
    assert len(casts) == 8
    assert len({c["key"] for c in casts}) == 8 and len({c["subject"] for c in casts}) == 8
    assert len(_casts("man")) == 4 and len(_casts("woman")) == 4
    for c in casts:
        s = c["subject"].lower()
        assert "emirati" in s
        if c["gender"] == "man":
            assert "kandura" in s and "ghutra" in s
        else:
            assert "abaya" in s and "shayla" in s and "fully visible" in s
        for banned in ("niqab", "sunglasses", "veil over", "falcon", "camel"):
            assert banned not in s
        ages = [int(n) for n in re.findall(r"\b(\d{2})s?\b", s)]
        assert ages and all(18 <= a <= 30 for a in ages), c["subject"]
        assert thp.subject_gender(c["subject"]) == c["gender"]


def test_default_emirati_cast_is_deterministic_and_honours_gender():
    a = thp.default_emirati_cast(random.Random(7), "woman")
    b = thp.default_emirati_cast(random.Random(7), "woman")
    assert a == b and a["gender"] == "woman" and a["subject"] in _casts("woman")
    assert thp.default_emirati_cast(random.Random(3), "man")["subject"] in _casts("man")
    either = thp.default_emirati_cast(random.Random(1))
    assert either["gender"] in ("man", "woman") and thp.is_default_cast(either["subject"])
    # A copy: changing it never changes the cast list.
    either["subject"] = "x"
    assert not thp.is_default_cast("x")


@pytest.mark.parametrize("tag", PROD_TAGS)
def test_clean_subject_strips_production_tags(tag):
    assert thp.clean_subject(tag) == ""


def test_clean_subject_keeps_a_description():
    assert thp.clean_subject(SUBJECT) == SUBJECT
    assert thp.clean_subject("#198 yao_7dc766 (Yao), a young man in a hoodie") == "a young man in a hoodie"
    assert thp.clean_subject("#12 the person in the attached reference image") == ""
    assert thp.clean_subject(None) == "" and thp.clean_subject(42) == ""


def test_subject_and_topic_gender():
    assert thp.subject_gender(BRAZIL) == "man"
    assert thp.subject_gender("a student in her first year") == "woman"
    assert thp.subject_gender("a student") is None
    assert thp.subject_gender("a man and a woman") is None
    assert thp.topic_speaker_gender("A female student on her first week at 42") == "woman"
    assert thp.topic_speaker_gender("Why I joined 42 as a young mother") == "woman"
    assert thp.topic_speaker_gender("", "I'm an Emirati man who learned to code at 30") == "man"
    # Pronouns and plurals there usually mean someone else: not fixed.
    assert thp.topic_speaker_gender("How my mentor changed my life: he never gave answers") is None
    assert thp.topic_speaker_gender("Why women should learn to code") is None
    assert thp.topic_speaker_gender("Why peer learning works") is None
    assert thp.topic_speaker_gender("a manager's guide", "a person who codes") is None
    assert thp.topic_speaker_gender("a woman and a man build an app") is None


def test_speaker_chip_gender():
    assert thp.speaker_chip_gender("Emirati man") == "man"
    assert thp.speaker_chip_gender("  an emirati WOMAN. ") == "woman"
    assert thp.speaker_chip_gender("an Emirati man in a white kandura") is None
    assert thp.speaker_chip_gender(None) is None


def test_effective_subject():
    sp = {"subject": _casts("woman")[0], "gender": "woman", "source": "default_emirati"}
    script = {"subject": "#234 person_a_2c28f4"}
    assert thp.effective_subject(script, sp) == sp["subject"]
    assert thp.effective_subject(script, sp, has_person_ref=True) == thp.PERSON_IN_FIRST_FRAME
    assert thp.effective_subject(script, None, fallback="FB") == "FB"
    assert thp.effective_subject({"subject": SUBJECT}) == SUBJECT
    assert thp.effective_subject(None, {"subject": "#1 a_b"}, fallback="FB") == "FB"


# ---------------------------------------------------------------------------
# Keyframe and Veo prompts
# ---------------------------------------------------------------------------

def test_no_character_keyframe_is_free_gen_with_the_subject():
    script = {"subject": _casts("man")[0], "action": "speaking"}
    p = thp.build_main_keyframe_prompt(script=script, character_brief=None, bg_brief=None,
                                       lock_character=True)
    assert "FACE-AND-OUTFIT LOCK" not in p and _casts("man")[0] in p
    b = thp.build_main_keyframe_prompt(script=script, character_brief=None, bg_brief=None,
                                       lock_character=True, has_character_ref=True)
    assert b.startswith("FACE-AND-OUTFIT LOCK") and "IDENTITY ANCHOR" not in b


def test_identity_anchor_prompt_fixes_identity_only():
    script = {"subject": SUBJECT, "action": "speaking"}
    p = thp.build_main_keyframe_prompt(script=script, character_brief=None, bg_brief=None,
                                       lock_character=False, has_character_ref=True,
                                       identity_anchor=True)
    assert p.startswith("FACE-AND-OUTFIT LOCK"), "the anchor is a real person image: compose"
    assert "IDENTITY ANCHOR" in p and "does NOT fix the framing" in p
    assert "not the background of the person reference" in p
    with_bg = thp.build_main_keyframe_prompt(script=script, character_brief=None, bg_brief="Lab",
                                             lock_character=False, has_character_ref=True,
                                             has_bg_ref=True, identity_anchor=True)
    assert "IDENTITY ANCHOR" in with_bg and "attached BACKGROUND reference" in with_bg
    # Without the anchor flag, an unlocked person image is not a face lock.
    plain = thp.build_main_keyframe_prompt(script=script, character_brief=None, bg_brief=None,
                                           lock_character=False, has_character_ref=True)
    assert not plain.startswith("FACE-AND-OUTFIT LOCK")


def _veo(subject, has_ref):
    return thp.build_main_veo_prompt(script={"subject": subject, "dialogue": "Hi there."},
                                     brand_req={}, has_character_ref=has_ref, has_bg_ref=False,
                                     dialogue_language="en")


@pytest.mark.parametrize("tag", PROD_TAGS)
def test_veo_subject_never_carries_a_tag(tag):
    for has_ref in (False, True):
        p = _veo(tag, has_ref)
        assert not re.search(r"#\d+ ", p) and "attached reference image" not in p
        assert f"Subject: {thp.PERSON_IN_FIRST_FRAME}" in p


def test_veo_subject_line():
    man = _casts("man")[1]
    assert f"Subject: {man}" in _veo(man, False)
    assert f"Subject: {thp.PERSON_IN_FIRST_FRAME}" in _veo(man, True)


def test_veo_call_sites_treat_an_anchored_scene_as_a_person_image():
    for fn in (th.start_videos, th.start_videos_multi, th.retry_video, th.resubmit_animation_if_transient):
        assert "_still_shows_a_pick(md," in inspect.getsource(fn), fn.__name__
    md = _speaker_md(scenes=2)
    assert th._still_shows_a_pick(md, 1) and not th._still_shows_a_pick(md, 0)


def _picked_run(env, run_id: int, monkeypatch, *, lock: bool, real_person: bool, scenes: int = 1) -> str:
    """A run whose script still names the default man (the script was
    written at Topic with no pick) and whose still is made from character
    238, picked later on the Look step. Returns that default subject."""
    man = _casts("man")[0]
    env.pick("asset", 238)

    async def is_real(asset_id):
        return real_person and int(asset_id or 0) == 238

    monkeypatch.setattr(th, "_asset_is_real_person_photo", is_real)
    md = _bookend_md(lock_character=lock, refs={"character_asset_id": 238},
                     intro_excluded=True, outro_excluded=True)
    md["script"]["subject"] = man
    md["speaker"] = {"subject": man, "gender": "man", "source": "default_emirati"}
    if scenes > 1:
        md["scene_count"] = scenes
        md["scenes"] = [_scene(i, keyframe_id=None) for i in range(scenes)]
    env.db.add_run(run_id, md, local_path=env.main_still(run_id))
    return man


@pytest.mark.parametrize("lock, real_person", [(True, True), (False, False), (False, True)])
def test_a_picked_character_is_the_veo_subject_even_when_veo_gets_no_photo(benv, monkeypatch,
                                                                         lock, real_person):
    """The still shows character 238 (its photo, or its description with the
    lock off), but Veo gets no photo of it (a real person, or no lock): the
    subject line still names the first frame, never the default man."""
    man = _picked_run(benv, 890, monkeypatch, lock=lock, real_person=real_person)
    asyncio.run(th.start_videos(890))
    sent, = benv.veo["scenes"]
    assert f"Subject: {thp.PERSON_IN_FIRST_FRAME}" in sent["prompt"] and man not in sent["prompt"]
    benv.anims[benv.db.md(890)["main_animation_id"]]["status"] = "ready"
    asyncio.run(th.retry_video(run_id=890, which="main", feedback_text=""))
    again = benv.veo["scenes"][-1]["prompt"]
    assert f"Subject: {thp.PERSON_IN_FIRST_FRAME}" in again and man not in again


def test_no_pick_keeps_the_default_speaker_as_the_veo_subject(benv, monkeypatch):
    man = _picked_run(benv, 891, monkeypatch, lock=True, real_person=False)
    benv.db.md(891)["refs"] = {}
    asyncio.run(th.start_videos(891))
    sent, = benv.veo["scenes"]
    assert f"Subject: {man}" in sent["prompt"]


def test_a_multi_scene_pick_is_the_veo_subject_of_every_scene(benv, monkeypatch):
    man = _picked_run(benv, 892, monkeypatch, lock=True, real_person=True, scenes=2)

    async def keyframe(*, run_id, scene_index, md):
        return Path(benv.main_still(run_id))

    monkeypatch.setattr(th, "_resolve_scene_keyframe_path", keyframe)
    asyncio.run(th.start_videos_multi(892))
    assert len(benv.veo["scenes"]) == 2
    for sent in benv.veo["scenes"]:
        assert f"Subject: {thp.PERSON_IN_FIRST_FRAME}" in sent["prompt"] and man not in sent["prompt"]


# ---------------------------------------------------------------------------
# The guard: _finalize_script and the speaker
# ---------------------------------------------------------------------------

def _finalize(monkeypatch, script, *, last=None, topic="Why peer learning works", dlgs=None,
              has_character=False, prev=None, scene_count=1):
    async def last_gender():
        return last

    monkeypatch.setattr(th, "_last_default_speaker_gender", last_gender)
    return asyncio.run(th._finalize_script(
        script, topic=topic, scene_count=scene_count, dlgs=dlgs or [None] * scene_count,
        has_character=has_character, prev_speaker=prev,
    ))


@pytest.mark.parametrize("subject", PROD_TAGS + ("a young professional in a blazer",))
def test_an_open_speaker_becomes_a_default_cast(monkeypatch, subject):
    script, sp = _finalize(monkeypatch, _script(subject), last="woman")
    assert sp["source"] == "default_emirati" and sp["gender"] == "man"
    assert sp["subject"] in _casts("man") and script["subject"] == sp["subject"]


def test_defaults_alternate_man_and_woman(monkeypatch):
    for last, want in ((None, "man"), ("man", "woman"), ("woman", "man")):
        _, sp = _finalize(monkeypatch, _script("#234 person_a_2c28f4"), last=last)
        assert sp["gender"] == want, last


def test_a_topic_gender_beats_the_alternation(monkeypatch):
    _, sp = _finalize(monkeypatch, _script("a student"), last="woman",
                      topic="A female student on her first week at 42")
    assert sp["gender"] == "woman" and sp["subject"] in _casts("woman")
    _, sp = _finalize(monkeypatch, _script("a student"), last="man",
                      dlgs=["As a young father, I code at night after work."])
    assert sp["gender"] == "man"


def test_the_same_topic_gets_the_same_default(monkeypatch):
    _, a = _finalize(monkeypatch, _script("x"), last="woman")
    _, b = _finalize(monkeypatch, _script("y"), last="woman")
    assert a == b


def test_a_topic_fixed_speaker_is_kept(monkeypatch):
    script, sp = _finalize(monkeypatch, _script(BRAZIL, user_specified=True), last=None)
    assert sp == {"subject": BRAZIL, "gender": "man", "source": "topic"}
    assert script["subject"] == BRAZIL
    # 'true' as a string counts; a tag-only subject does not survive.
    _, sp = _finalize(monkeypatch, _script(BRAZIL, user_specified="true"))
    assert sp["source"] == "topic"
    _, sp = _finalize(monkeypatch, _script("#238 person_a_1b8c5e", user_specified=True))
    assert sp["source"] == "default_emirati"


def test_a_picked_character_keeps_its_subject(monkeypatch):
    tagged = "#220 the person in the attached reference image"
    script, sp = _finalize(monkeypatch, _script(tagged), has_character=True)
    assert script["subject"] == tagged, "the CHARACTER LOCK subject is left to the prompts"
    assert sp["source"] == "default_emirati", "a speaker is still kept for when the pick is cleared"


def test_a_rescript_keeps_the_users_and_the_default_speaker(monkeypatch):
    mine = {"subject": "a woman in a navy abaya and a grey shayla", "gender": "woman", "source": "user"}
    script, sp = _finalize(monkeypatch, _script(BRAZIL, user_specified=True), prev=mine)
    assert sp == mine and script["subject"] == mine["subject"]
    dflt = {"subject": _casts("man")[2], "gender": "man", "source": "default_emirati"}
    _, sp = _finalize(monkeypatch, _script("x"), prev=dflt, last="man")
    assert sp == dflt, "the run keeps its person, the alternation is for new runs"
    # The new topic fixes the other gender: a new default of that gender.
    _, sp = _finalize(monkeypatch, _script("x"), prev=dflt, topic="A day as a young mother at 42")
    assert sp["gender"] == "woman" and sp["source"] == "default_emirati"
    # A topic-fixed speaker the new topic no longer names: back to a default.
    topic_sp = {"subject": BRAZIL, "gender": "man", "source": "topic"}
    _, sp = _finalize(monkeypatch, _script("x"), prev=topic_sp, last="man")
    assert sp["source"] == "default_emirati" and sp["gender"] == "woman"


def test_finalize_keeps_the_existing_guards(monkeypatch):
    s = _script("x", scenes=[{"dialogue": "model line", "hook": "model"}])
    script, _ = _finalize(monkeypatch, s, scene_count=2, dlgs=[None, "My own words, kept exactly."])
    assert script["action"] == th._solo_front_facing_action("eyes widen")
    assert len(script["scenes"]) == 2
    assert script["scenes"][1]["dialogue"] == "My own words, kept exactly."
    assert script["scenes"][1]["hook"] == "My own words, kept exactly."
    assert script["dialogue"] == "model line"


def test_last_default_speaker_gender_reads_the_newest_default(senv):
    db = senv.db
    db.add_run(5, _md(speaker={"subject": "a", "gender": "woman", "source": "default_emirati"}))
    db.add_run(6, _md(speaker={"subject": "b", "gender": "man", "source": "default_emirati"}))
    db.add_run(7, _md(speaker={"subject": "c", "gender": "woman", "source": "user"}))
    assert asyncio.run(th._last_default_speaker_gender()) == "man"


def test_last_default_speaker_gender_survives_a_db_error(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(th, "get_conn", broken)
    assert asyncio.run(th._last_default_speaker_gender()) is None


# ---------------------------------------------------------------------------
# generate_script and the re-script, end to end on the fake store
# ---------------------------------------------------------------------------

def test_generate_script_falls_back_to_gemini_and_persists_the_speaker(senv, monkeypatch):
    calls = _llm(monkeypatch, openai=BudgetExceededError("no credits"),
                 gemini=_script("#198 yao_7dc766 (Yao) the person in the attached reference image"))
    out = _gen_script(monkeypatch)
    run = senv.db.rows[out["id"]]
    md = run["metadata"]
    assert [c[0] for c in calls] == ["openai", "gemini"]
    gem = calls[1][1]
    assert gem["system"] == thp.TALKING_HEAD_SCRIPT_SYSTEM
    assert "speaker_user_specified" in gem["json_schema"]["required"]
    assert thp.DEFAULT_SPEAKER_BLOCK in calls[0][1]["prompt"]
    assert md["speaker"]["source"] == "default_emirati" and md["speaker"]["gender"] == "man"
    assert md["script"]["subject"] == md["speaker"]["subject"]
    assert md["script"]["subject"] in _casts("man")


def test_successive_runs_alternate(senv, monkeypatch):
    _llm(monkeypatch, openai=_script("an Emirati man in a kandura"))
    genders = []
    for _ in range(4):
        out = _gen_script(monkeypatch)
        genders.append(senv.db.rows[out["id"]]["metadata"]["speaker"]["gender"])
    assert genders == ["man", "woman", "man", "woman"]


def test_generate_script_fails_cleanly_when_both_models_fail(senv, monkeypatch):
    _llm(monkeypatch, openai=RuntimeError("429"), gemini=RuntimeError("down"))
    with pytest.raises(HTTPException) as e:
        _gen_script(monkeypatch)
    assert e.value.status_code == 502


def test_both_script_paths_share_the_call_and_the_guard():
    for fn in (th.generate_script, th._gpt_script_dict):
        src = inspect.getsource(fn)
        assert "_script_llm(" in src and "_finalize_script(" in src, fn.__name__
        assert "openai_chat(" not in src, f"{fn.__name__} calls OpenAI directly"


def _rescript_env(senv, monkeypatch, run_id, md, **cols):
    monkeypatch.setattr(th, "_build_script_inputs", _no_inputs)
    senv.db.add_run(run_id, md, **cols)


def test_rescript_falls_back_to_gemini(senv, monkeypatch):
    dflt = {"subject": _casts("woman")[1], "gender": "woman", "source": "default_emirati"}
    _rescript_env(senv, monkeypatch, 60, _md(speaker=dflt))
    calls = _llm(monkeypatch, openai=BudgetExceededError("no credits"), gemini=_script("x"))
    asyncio.run(th.rescript_run(60, new_topic="Why debugging together is faster"))
    md = senv.db.md(60)
    assert [c[0] for c in calls] == ["openai", "gemini"]
    assert senv.db.rows[60]["topic"] == "Why debugging together is faster"
    assert md["speaker"] == dflt and md["script"]["subject"] == dflt["subject"]
    assert md["main_video_stale"] is True


def test_rescript_fails_cleanly_when_both_models_fail(senv, monkeypatch):
    _rescript_env(senv, monkeypatch, 61, _md())
    _llm(monkeypatch, openai=BudgetExceededError("no credits"), gemini=RuntimeError("down"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.rescript_run(61, new_topic="Another topic"))
    assert e.value.status_code == 502


# ---------------------------------------------------------------------------
# Same person across scenes: scene 0's still is the identity anchor
# ---------------------------------------------------------------------------

def _speaker_md(scenes=3, **kw):
    sp = {"subject": _casts("woman")[0], "gender": "woman", "source": "default_emirati"}
    md = _md(scenes=scenes, speaker=sp, **kw)
    md["script"]["subject"] = "#234 person_a_2c28f4"  # an older tag: never used
    return md


def test_person_ref_for_scene_falls_back_to_scene_0s_still(env):
    char = env.ref_file("asset", 9)
    md = _speaker_md()
    md["scenes"][2].update(character_asset_id=9)
    env.db.add_run(70, md)
    assert asyncio.run(th._person_ref_for_scene(md, 1, run_id=70)) is None, "scene 0 has no still yet"
    main = env.tmp / "main0.png"
    main.write_bytes(b"x")
    env.db.rows[70]["local_path"] = str(main)
    assert asyncio.run(th._person_ref_for_scene(md, 1, run_id=70)) == main
    assert asyncio.run(th._person_ref_for_scene(md, 1)) is None, "no run id, no anchor"
    assert asyncio.run(th._person_ref_for_scene(md, 0, run_id=70)) is None, "scene 0 never anchors"
    assert asyncio.run(th._person_ref_for_scene(md, 2, run_id=70)) == char, "a pick wins"
    assert th._scene_anchored(md, 1) and not th._scene_anchored(md, 0) and not th._scene_anchored(md, 2)
    md["refs"]["character_upload_id"] = 5
    assert not th._scene_anchored(md, 1), "a run-wide pick: no anchor"


def test_compose_sources_attach_the_anchor_whatever_the_lock(env):
    md = _speaker_md(lock=False)
    env.db.add_run(71, md)
    main = env.tmp / "main1.png"
    main.write_bytes(b"x")
    env.db.rows[71]["local_path"] = str(main)
    sources, has_char, has_bg = asyncio.run(th._compose_sources(md, 1, run_id=71))
    assert sources == [main, main] and has_char and not has_bg
    sources, has_char, _ = asyncio.run(th._compose_sources(md, 0, run_id=71))
    assert sources == [] and not has_char


def test_scene_prompt_uses_the_speaker_and_marks_the_anchor():
    md = _speaker_md()
    p0 = th._build_scene_prompt(md, 0, character_brief=None, bg_brief=None,
                                has_character_ref=False, has_bg_ref=False)
    assert md["speaker"]["subject"] in p0 and "#234" not in p0 and "IDENTITY ANCHOR" not in p0
    p1 = th._build_scene_prompt(md, 1, character_brief=None, bg_brief=None,
                                has_character_ref=True, has_bg_ref=False)
    assert "IDENTITY ANCHOR" in p1
    legacy = _md()
    legacy["script"]["subject"] = "#198 yao_7dc766 (Yao) the person in the attached reference image"
    p = th._build_scene_prompt(legacy, 0, character_brief=None, bg_brief=None,
                               has_character_ref=False, has_bg_ref=False)
    assert thp.DEFAULT_SPEAKER_FALLBACK in p and "#198" not in p
    picked = th._build_scene_prompt(md, 0, character_brief="#9 Sara: archetype emirati_female",
                                    bg_brief=None, has_character_ref=False, has_bg_ref=False)
    assert md["speaker"]["subject"] not in picked, "a picked character is never contradicted"
    assert "the person in the character description below" in picked


def test_generate_previews_anchors_every_scene_on_scene_0(env):
    env.db.add_run(72, _speaker_md(scenes=3))
    asyncio.run(th.generate_previews(72, look={"refs": {}}))
    calls = env.gen["calls"]
    assert len(calls) == 3
    first = calls[0]
    assert first["sources"] is None, "scene 0 is made first, from text"
    assert _casts("woman")[0] in first["prompt"]
    for bad in PHANTOM:
        assert bad not in first["prompt"]
    anchor = th._abs_from_rel(env.db.rows[72]["local_path"])
    for c in calls[1:]:
        assert c["sources"] == [anchor, anchor]
        assert c["prompt"].startswith("FACE-AND-OUTFIT LOCK") and "IDENTITY ANCHOR" in c["prompt"]
    sc = env.db.md(72)["scenes"]
    assert sc[1]["keyframe_id"] and sc[2]["keyframe_id"]


def test_a_scene_pick_beats_the_anchor(env):
    char = env.ref_file("asset", 9)
    bg = env.ref_file("asset", 4, size=(1500, 1000))
    md = _speaker_md(scenes=2, lock=True)
    md["scenes"][1].update(character_asset_id=9, bg_asset_id=4)
    env.db.add_run(73, md)
    asyncio.run(th.generate_previews(73, look={"refs": {}}))
    s1 = [c for c in env.gen["calls"] if c["sources"]][0]
    assert s1["sources"] == [char, char, bg] and "IDENTITY ANCHOR" not in s1["prompt"]


def test_retry_and_added_scenes_use_the_anchor(env):
    md = _speaker_md(scenes=3)
    env.db.add_run(74, md)
    main = env.tmp / "th74-main.png"
    main.write_bytes(b"x")
    env.db.rows[74]["local_path"] = str(main)
    # No feedback: Regenerate (E4) composes scene 1 on the anchor too.
    asyncio.run(th.retry_preview(run_id=74, which="scene_1", feedback_text=""))
    asyncio.run(th.generate_previews_for_scenes(74, [2]))
    retry, added = env.gen["calls"]
    assert retry["sources"] == [main, main] and "IDENTITY ANCHOR" in retry["prompt"]
    assert added["sources"] == [main, main] and "IDENTITY ANCHOR" in added["prompt"]


def test_keyframe_regen_reshoots_scene_0_first_and_anchors_on_the_new_still(env):
    md = _speaker_md(scenes=2)
    md["scenes"][1].update(keyframe_id=600)
    env.db.add_run(75, md)
    old = env.tmp / "th75-old.png"
    old.write_bytes(b"x")
    env.db.rows[75]["local_path"] = str(old)
    spec = {"keep_subject": False, "has_char_ref": False, "has_bg_ref": False}
    asyncio.run(th._run_keyframe_regen(75, [{"scene_index": 1, **spec}, {"scene_index": 0, **spec}]))
    s0, s1 = env.gen["calls"]
    assert s0["sources"] is None and _casts("woman")[0] in s0["prompt"]
    new_main = th._abs_from_rel(env.db.rows[75]["local_path"])
    assert new_main != old and s1["sources"] == [new_main, new_main]
    assert "IDENTITY ANCHOR" in s1["prompt"]


# ---------------------------------------------------------------------------
# The Speaker field: edit_run(speaker_subject)
# ---------------------------------------------------------------------------

@pytest.fixture
def dispatched(monkeypatch):
    got: list[list[dict]] = []
    monkeypatch.setattr(th, "_dispatch_keyframe_regen", lambda run_id, specs: got.append(specs))
    return got


def test_a_speaker_edit_before_stills_is_only_saved(env, dispatched):
    env.db.add_run(80, _speaker_md(scenes=1))
    asyncio.run(th.edit_run(run_id=80, speaker_subject="a woman in a beige abaya and a white shayla"))
    md = env.db.md(80)
    assert md["speaker"] == {"subject": "a woman in a beige abaya and a white shayla",
                             "gender": "woman", "source": "user"}
    assert md["script"]["subject"] == md["speaker"]["subject"]
    assert dispatched == [] and not md.get("main_video_stale")


def test_a_chip_becomes_a_default_cast_and_reshoots_the_still(env, dispatched):
    env.db.add_run(81, _speaker_md(scenes=1), local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=81, speaker_subject="Emirati man"))
    md = env.db.md(81)
    assert md["speaker"]["source"] == "user" and md["speaker"]["gender"] == "man"
    assert md["speaker"]["subject"] in _casts("man")
    assert md["main_video_stale"] is True and md["main_keyframe_generating"] is True
    assert dispatched == [[{"scene_index": 0, "keep_subject": False,
                            "has_char_ref": False, "has_bg_ref": False}]]


def test_the_chip_of_the_current_gender_keeps_the_person(env, dispatched):
    md = _speaker_md(scenes=1)
    env.db.add_run(82, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=82, speaker_subject="Emirati woman"))
    out = env.db.md(82)["speaker"]
    assert out["subject"] == md["speaker"]["subject"] and out["source"] == "user"
    assert dispatched == [], "same person: nothing to re-shoot"


def test_a_speaker_edit_reshoots_only_the_scenes_showing_it(env, dispatched):
    md = _speaker_md(scenes=3)
    md["scenes"][2].update(character_asset_id=9, keyframe_id=702)
    md["scenes"][1].update(keyframe_id=701, animation_id=55)
    env.db.add_run(83, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=83, speaker_subject="a woman in a navy abaya"))
    specs, = dispatched
    assert [s["scene_index"] for s in specs] == [0, 1]
    sc = env.db.md(83)["scenes"]
    assert sc[0]["video_stale"] and sc[1]["video_stale"] and not sc[2]["video_stale"]
    assert sc[1]["keyframe_generating"] and not sc[2].get("keyframe_generating")


def test_no_speaker_reshoot_when_scene_0_or_the_run_has_a_character(env, dispatched):
    md = _speaker_md(scenes=2)
    md["scenes"][0].update(character_asset_id=9)
    env.db.add_run(84, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=84, speaker_subject="Emirati man"))
    assert dispatched == [] and env.db.md(84)["speaker"]["gender"] == "man"
    md = _speaker_md(scenes=1, refs={"character_asset_id": 9})
    md["script"]["subject"] = "#9 the person in the attached reference image"
    env.db.add_run(85, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=85, speaker_subject="Emirati man"))
    assert dispatched == []
    assert env.db.md(85)["script"]["subject"].startswith("#9"), "the picked character's subject stays"


def test_an_empty_or_tag_only_speaker_is_ignored(env, dispatched):
    md = _speaker_md(scenes=1)
    env.db.add_run(86, md, local_path="x/main.png")
    for text in ("   ", "#234 person_a_2c28f4"):
        asyncio.run(th.edit_run(run_id=86, speaker_subject=text))
    assert env.db.md(86)["speaker"] == md["speaker"] and dispatched == []


def test_a_rescript_that_changes_the_speaker_reshoots(senv, dispatched, monkeypatch):
    md = _speaker_md(scenes=2)
    md["scenes"][1].update(keyframe_id=701)
    senv.db.add_run(87, md, local_path="x/main.png")
    monkeypatch.setattr(th, "_build_script_inputs", _no_inputs)
    _llm(monkeypatch, openai=_script(BRAZIL, user_specified=True,
                                     scenes=[{"dialogue": "a", "hook": "a"}, {"dialogue": "b", "hook": "b"}]))
    asyncio.run(th.edit_run(run_id=87, topic="A Brazilian exchange student's first week at 42"))
    out = senv.db.md(87)
    assert out["speaker"] == {"subject": BRAZIL, "gender": "man", "source": "topic"}
    specs, = dispatched
    assert sorted(s["scene_index"] for s in specs) == [0, 1]
    assert out["main_keyframe_generating"] is True and out["scenes"][1]["keyframe_generating"] is True


def test_a_rescript_that_keeps_the_speaker_reshoots_nothing(senv, dispatched, monkeypatch):
    md = _speaker_md(scenes=1)
    senv.db.add_run(88, md, local_path="x/main.png")
    monkeypatch.setattr(th, "_build_script_inputs", _no_inputs)
    _llm(monkeypatch, openai=_script("an Emirati man"))
    asyncio.run(th.edit_run(run_id=88, topic="Why debugging together is faster"))
    assert senv.db.md(88)["speaker"] == md["speaker"] and dispatched == []


def test_user_speaker_helper():
    cur = {"subject": _casts("man")[0], "gender": "man", "source": "default_emirati"}
    assert th._user_speaker("Emirati man", cur, run_id=1)["subject"] == cur["subject"]
    w1 = th._user_speaker("an Emirati woman", cur, run_id=1)
    assert w1 == th._user_speaker("Emirati woman", None, run_id=1), "seeded by the run"
    assert w1["subject"] in _casts("woman") and w1["source"] == "user"
    long = th._user_speaker("x" * 900, None, run_id=1)
    assert len(long["subject"]) == th._SPEAKER_SUBJECT_MAX and long["gender"] is None
    assert th._user_speaker("", cur, run_id=1) is None


def test_fetch_run_returns_the_speaker():
    assert '"speaker": md.get("speaker")' in inspect.getsource(th.fetch_run)


def test_the_edit_route_passes_the_speaker(monkeypatch):
    from app.routers import yas_producer as yp

    seen: list[dict] = []

    async def edit_run(**kw):
        seen.append(kw)
        return {}

    monkeypatch.setattr(yp.th_v2_service, "edit_run", edit_run)
    asyncio.run(yp.th_v2_edit(5, yp.THV2EditRequest(speaker_subject="Emirati woman")))
    asyncio.run(yp.th_v2_edit(5, yp.THV2EditRequest(topic="t")))
    assert seen[0]["speaker_subject"] == "Emirati woman" and seen[1]["speaker_subject"] is None
    with pytest.raises(Exception):
        yp.THV2EditRequest(speaker_subject="x" * 1001)


# ---------------------------------------------------------------------------
# Human Character: Emirati for open briefs, Gemini when OpenAI is out
# ---------------------------------------------------------------------------

def test_human_character_casting_defaults_to_emirati():
    from app.services.human_character_prompts import HUMAN_CHARACTER_CONCEPTS_SYSTEM as s

    assert "cast Emirati nationals by default" in s
    assert "kandura" in s and "abaya" in s and "fully visible face" in s
    assert "no niqab" in s
    assert "FAITHFUL to anything the brief fixes" in s, "fixed traits still win"
    assert "international mix, cast exactly that" in s


def test_character_enhancer_enriches_open_nationality_as_emirati():
    from app.routers.yas_producer import _ENHANCE_CHARACTER_SYSTEM as s

    assert "left nationality and ethnicity open" in s and "Emirati national" in s
    assert "KEEP every trait the user fixed" in s


def _hc(monkeypatch, *, openai, gemini):
    from app.routers import yas_producer as yp

    calls: list[tuple[str, dict]] = []

    async def openai_chat(prompt, **kw):
        calls.append(("openai", kw))
        if isinstance(openai, BaseException):
            raise openai
        return openai

    async def gemini_text(prompt, **kw):
        calls.append(("gemini", kw))
        if isinstance(gemini, BaseException):
            raise gemini
        return gemini

    async def recent():
        return []

    monkeypatch.setattr(yp, "openai_chat", openai_chat)
    monkeypatch.setattr(yp, "gemini_text", gemini_text)
    monkeypatch.setattr(yp, "_human_character_recent_summaries", recent)
    return yp, calls


def _people(n):
    return {"concepts": [{"summary": f"person {i}", "prompt": f"A portrait of person number {i}, "
                          "an Emirati woman in a black abaya and shayla, face fully visible."}
                         for i in range(n)]}


def test_casting_falls_back_to_gemini(monkeypatch):
    yp, calls = _hc(monkeypatch, openai=BudgetExceededError("no credits"), gemini=_people(4))
    concepts, cost = asyncio.run(yp._human_character_concepts("a coding student", 4))
    assert len(concepts) == 4 and cost == 0.0
    assert [c[0] for c in calls] == ["openai", "gemini"]
    kw = calls[1][1]
    from app.services.human_character_prompts import (HUMAN_CHARACTER_GEMINI_CONCEPTS_SCHEMA,
                                                      HUMAN_CHARACTER_STYLE)
    assert kw["json_schema"] is HUMAN_CHARACTER_GEMINI_CONCEPTS_SCHEMA
    assert kw["system"] is HUMAN_CHARACTER_STYLE.concepts_system
    assert kw["model"] == yp.config.vertex_gemini_fast_model and kw["thinking_budget"] == 0


def test_casting_uses_openai_when_it_works(monkeypatch):
    yp, calls = _hc(monkeypatch, openai=json.dumps(_people(4)), gemini=RuntimeError("unused"))
    concepts, _ = asyncio.run(yp._human_character_concepts("a coding student", 4))
    assert len(concepts) == 4 and [c[0] for c in calls] == ["openai"]


def test_casting_errors_when_both_fail(monkeypatch):
    yp, _ = _hc(monkeypatch, openai=RuntimeError("429 insufficient_quota"),
                gemini={"_raw": "not json"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(yp._human_character_concepts("a coding student", 4))
    assert e.value.status_code == 502
    yp, _ = _hc(monkeypatch, openai=asyncio.TimeoutError(), gemini=asyncio.TimeoutError())
    with pytest.raises(HTTPException) as e:
        asyncio.run(yp._human_character_concepts("a coding student", 4))
    assert e.value.status_code == 504


def test_gemini_casting_schema_matches_the_contract():
    from app.services.human_character_prompts import HUMAN_CHARACTER_GEMINI_CONCEPTS_SCHEMA as g

    item = g["properties"]["concepts"]["items"]
    assert g["required"] == ["concepts"] and item["required"] == ["summary", "prompt"]
    assert "TEXT TO RENDER" not in json.dumps(g), "a portrait carries no text"
