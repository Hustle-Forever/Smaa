"""Rebrand studio icons, the director in the studio and the finishing touches
(I15): every icon's "Why?" (elements/icon.why_of, idea_why), Keep on the
AI's picks, the team's icons that are said offered as suggestions (never
placed on their own), an added team icon landing where its words are said
(and those words in the timing key), hopeful words ("your future") as
one-tap suggestions — the prompt for new calls, the lists for answers given
before, no key moved — the log of kept / removed / added at each Generate
(icon_feedback) and its report (icon_director_report), a chip's grow
(icon_render), and the parity report page (icon_parity).

No model is ever called (an autouse guard makes every paid client raise),
no database is touched (the feedback log and the team's icons are in
memory), the director's key is never moved for a scene already answered."""
from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import vertex_client
from app.services.rebrand_harness import director as D
from app.services.rebrand_harness import icon_director_report as REP
from app.services.rebrand_harness import icon_feedback as FB
from app.services.rebrand_harness import icon_parity as PAR
from app.services.rebrand_harness import icon_render as ir
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import icon as I
from test_icons_plan import FREE_CODE, ctx_for, director_of, icon_el, idea_of, item, judged, onset, shown, track
from test_icons_plan import wi, words

TEAM_ID = "c-0123456789ab"
PISCINE = {"id": TEAM_ID, "name": "Piscine", "label": "Piscine", "tags": ["bootcamp", "school"]}


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    """No paid call can run; the feedback log is in memory."""
    calls: list[str] = []

    def refuse(name):
        async def _refuse(*a, **k):
            calls.append(name)
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    for name in ("gemini_multimodal", "gemini_image", "gemini_text"):
        if hasattr(vertex_client, name):
            monkeypatch.setattr(vertex_client, name, refuse(name))
    log = FB.MemoryStore()
    old = FB.set_store(log)
    yield {"calls": calls, "log": log}
    FB.set_store(old)
    assert not calls, f"unexpected paid calls: {calls}"


def team_terms(rows):
    """The studio's words lookup over these team rows (studio.icon_terms)."""
    return studio.icon_terms(list(rows))


# ---------------------------------------------------------------------------
# "Why?" on every icon
# ---------------------------------------------------------------------------

def test_every_shown_icon_says_who_picked_it_what_was_said_where_and_what_colour():
    w = words(815)
    p = I.plan(icon_el(style="circle"), ctx_for(w, judged(w, FREE_CODE), track=track()))
    assert shown(p) == ["Free", "Code"]
    for label, word, prio in (("Free", "tuition", 5), ("Code", "coding", 4)):
        it = item(p, label)
        why = it["why"]
        assert (why["by"], why["priority"], why["time"], why["purpose"]) == ("ai", prio, "word", "Why it helps")
        assert why["said"] == {"word": word, "at": round(onset(w, it["w"]), 3)}
        assert why["place"] == {"by": p.home["by"], "text": p.home["why"]}, "it sits at the scene's home spot"
        assert why["colour"] == {"by": "scene", "text": p.colours["why"]}
        assert why["text"] == (f"Picked by the AI · key idea {prio} of 5 · said '{word}' at "
                               f"{why['said']['at']:.1f} s · Why it helps")
    # Only in the studio's list: the frozen icons are as they always were.
    assert all("why" not in r for r in p.resolved())


def test_why_says_what_the_user_did():
    w = words(815)
    d = judged(w, FREE_CODE)

    def only(add: dict, **kw) -> dict:
        p = I.plan(icon_el(style="circle", added=[add]), ctx_for(w, d, track=track(), **kw))
        return {it["label"]: it["why"] for it in p.public()["items"]}

    # Kept: the AI's pick, pressed Keep (added as the user's own).
    code = only({"glyph": {"source": "library", "id": "code"}, "label": "Code"})["Code"]
    assert (code["by"], code["priority"], code["time"]) == ("kept", 4, "word")
    assert code["text"].startswith("The AI's pick, kept by you · key idea 4 of 5 · said 'coding'")
    # Added from the library: the user's, no AI priority.
    school = only({"glyph": {"source": "library", "id": "graduation-cap"}, "label": "School"})["School"]
    assert (school["by"], school["priority"], school["said"]["word"]) == ("you", None, "school")
    assert school["text"].startswith("Added by you · said 'school'")
    # One of the team's icons.
    pis = only({"glyph": {"source": "custom", "id": TEAM_ID}, "label": "Piscine"},
               terms=team_terms([PISCINE]), team=[PISCINE])["Piscine"]
    assert (pis["by"], pis["said"]["word"]) == ("team", "school")
    assert pis["text"].startswith("Your team's icon 'Piscine' · said 'school'")
    # A time set by hand, a box of the user's, a colour of their own.
    box = {"x": 0.62, "y": 0.05, "w": 0.2, "h": 0.1125}
    el = icon_el(style="circle", tweaks={"code": {"off_word": True, "sec": 5.6, "box": box}},
                 looks={"code": {"colour": "#303030"}})
    code = item(I.plan(el, ctx_for(w, d, track=track())), "Code")["why"]
    assert (code["time"], code["place"], code["colour"]) == ("hand", {"by": "you", "text": ""},
                                                            {"by": "you", "text": ""})
    assert "timed by you at 5.6 s" in code["text"]


def test_a_kept_versions_icons_say_where_they_came_from():
    w = words(815)
    p = I.plan(icon_el(style="circle"), ctx_for(w, judged(w, FREE_CODE), track=track()))
    kept = I.kept_plan(p.resolved(), version_id=7, width=720, height=1280, words=w)
    for it in kept.public()["items"]:
        assert it["why"]["by"] == "version" and it["why"]["text"] == "Kept from v7"
        assert it["why"]["place"]["by"] == it["why"]["colour"]["by"] == "version"


def _answer(w: dict, verdicts: dict, *, extra=()) -> tuple[dict, dict]:
    """(the director's body with its raw answer, the answer): each keyword
    candidate judged as `verdicts` {icon: (verdict, reason, priority)} says;
    one left out is given no verdict."""
    ideas = []
    for c in D.candidates(w):
        v = verdicts.get(c["icon"])
        if v is None:
            continue
        verdict, reason, prio = v
        ideas.append({"cand_id": c["id"], "concept": c["icon"], "quote": c["quote"], "w": c["w"],
                      "w_end": c["w_end"], "mentions": [], "icon": c["icon"], "label": c["icon"][:16],
                      "purpose": "Why it helps", "priority": prio, "verdict": verdict, "refuse_reason": reason,
                      "list_span": False})
    answer = {"ideas": ideas + list(extra), "room": {"spots": [], "home": 0, "home_why": ""}}
    cands = D.candidates(w)
    got, room = D.validate(answer, w, cands, D.keyword_catalog())
    return {"version": D.DIRECTOR_VERSION, "key": "k", "source": "ai", "ideas": got, "room": room,
            "candidates": cands, "note": "", "raw": answer, "inputs": {"model": "gemini-2.5-flash"}}, answer


def test_each_idea_says_who_left_it_out_and_why():
    w = words(815)
    teachers = {"cand_id": "new", "concept": "teachers", "quote": "teachers", "w": wi(w, "teachers"),
                "w_end": wi(w, "teachers"), "mentions": [], "icon": "chalkboard-teacher", "label": "Teachers",
                "purpose": "x", "priority": 5, "verdict": "keep", "refuse_reason": "", "list_span": False}
    body, _answer_ = _answer(w, {"piggy-bank": ("keep", "", 5), "code": ("keep", "", 4),
                                 "puzzle-piece": ("keep", "", 4), "book-open": ("refuse", "too generic", 2),
                                 "rocket-launch": ("refuse", "filler word", 3)}, extra=[teachers])
    p = I.plan(icon_el(style="circle", dismissed=["code"]), ctx_for(w, body, track=track()))
    why = {i["glyph"]["id"]: i["why"] for i in p.ideas}
    assert why["piggy-bank"] == {"by": "on", "text": ""}
    assert why["code"] == {"by": "you", "text": I.REMOVED}
    assert why["book-open"] == {"by": "ai", "text": "too generic"}, "the AI's own words"
    assert why["chalkboard-teacher"] == {"by": "check", "text": D.WHY_NEGATED}, "kept by the AI, a rule refused it"
    assert why["users-three"] == {"by": "silent", "text": D.WHY_SILENT}
    assert why["rocket-launch"] == {"by": "hopeful", "text": "filler word"}
    assert why["puzzle-piece"]["by"] in ("on", "room")
    # Keywords only (the AI off or down): suggestions, said so.
    cands = D.candidates(w)
    down = director_of(*D.fallback(w, cands, D.keyword_catalog(), "AI unavailable"), source="keywords")
    p = I.plan(icon_el(style="circle"), ctx_for(w, down, track=track()))
    assert {i["why"]["by"] for i in p.ideas} == {"keyword"}
    # A suggestion the AI made itself (the new prompt), not a hopeful word.
    body, _a = _answer(w, {"piggy-bank": ("keep", "", 5), "book-open": ("suggest", "general", 2)})
    lrn = next(i for i in I.plan(icon_el(style="circle"), ctx_for(w, body, track=track())).ideas
               if i["glyph"]["id"] == "book-open")
    assert (lrn["state"], lrn["why"]) == ("available", {"by": "suggest", "text": "general"})


# ---------------------------------------------------------------------------
# Keep on the AI's picks
# ---------------------------------------------------------------------------

def test_keep_holds_an_ai_pick_whatever_the_ai_picks_next():
    """✓ Keep adds the AI's pick as the user's own (rebrandIcons.addIcon): a
    new take that refuses it, or no AI at all, still shows it on its word."""
    w = words(815)
    kept = icon_el(style="circle", added=[{"glyph": {"source": "library", "id": "code"}, "label": "Code"}])
    first = I.plan(kept, ctx_for(w, judged(w, FREE_CODE), track=track()))
    code = item(first, "Code")
    assert (code["source"], code["why"]["by"], code["word"]) == ("user", "kept", "coding")
    # A new take: the AI now refuses code.
    retake = judged(w, {"piggy-bank": {"priority": 5, "label": "Free"}})
    again = I.plan(kept, ctx_for(w, retake, track=track()))
    assert "Code" in shown(again) and item(again, "Code")["word"] == "coding"
    assert item(again, "Code")["why"]["by"] == "you", "the AI no longer picks it: it is simply yours"
    # Without the AI at all (keyword suggestions only): still there.
    cands = D.candidates(w)
    down = director_of(*D.fallback(w, cands, D.keyword_catalog(), "AI unavailable"), source="keywords")
    assert "Code" in shown(I.plan(kept, ctx_for(w, down, track=track())))
    # Not kept, the new take drops it.
    assert "Code" not in shown(I.plan(icon_el(style="circle"), ctx_for(w, retake, track=track())))


# ---------------------------------------------------------------------------
# The team's icons: said -> suggested, never placed; added -> on their words
# ---------------------------------------------------------------------------

def test_a_team_icon_that_is_said_is_a_suggestion_never_placed_on_its_own():
    w = words(815)
    d = judged(w, FREE_CODE)
    teacher = {"id": "c-aaaaaaaaaaaa", "name": "Mentor", "label": "Mentor", "tags": ["teachers"]}
    later = {"id": "c-bbbbbbbbbbbb", "name": "Build", "label": "Build", "tags": ["building"]}
    team = [PISCINE, teacher, later]
    p = I.plan(icon_el(style="circle"), ctx_for(w, d, track=track(), team=team))
    assert shown(p) == ["Free", "Code"], "never placed on its own"
    tips = [i for i in p.ideas if i["source"] == "team"]
    assert [i["glyph"]["id"] for i in tips] == [TEAM_ID, later["id"]], "said, in speech order"
    pis = tips[0]
    assert (pis["state"], pis["auto"], pis["added"], pis["word"]) == ("available", False, False, "school")
    assert pis["glyph"] == {"source": "custom", "id": TEAM_ID} and pis["label"] == "Piscine"
    assert pis["reason"] == I.TEAM_SAID.format(t=f"{onset(w, wi(w, 'school')):.1f} s")
    assert pis["why"]["by"] == "team"
    assert teacher["id"] not in {i["glyph"]["id"] for i in p.ideas}, "“without teachers” is a no: not offered"
    # The same keys with or without the team: suggestions never move a plan.
    bare = I.plan(icon_el(style="circle"), ctx_for(w, d, track=track()))
    assert (p.timing_key, p.plan_key, p.items) == (bare.timing_key, bare.plan_key, bare.items)
    # Added or removed, it isn't suggested again; at most TEAM_SUGGEST_MAX.
    gone = I.plan(icon_el(style="circle", dismissed=[TEAM_ID]), ctx_for(w, d, track=track(), team=team))
    assert TEAM_ID not in {i["glyph"]["id"] for i in gone.ideas if i["source"] == "team"}
    many = [{"id": f"c-{k:012x}", "name": f"Free {k}", "label": f"Free {k}", "tags": ["tuition"]} for k in range(6)]
    assert len(I.team_ideas(w, many, set())) == I.TEAM_SUGGEST_MAX


def test_an_added_team_icon_lands_where_its_tags_are_said_and_its_words_are_in_the_key():
    w = words(815)
    d = judged(w, FREE_CODE)
    el = icon_el(style="circle", added=[{"glyph": {"source": "custom", "id": TEAM_ID}, "label": "Piscine"}])
    p = I.plan(el, ctx_for(w, d, track=track(), terms=team_terms([PISCINE]), team=[PISCINE]))
    assert shown(p) == ["Free", "Piscine"], "the user's add outranks the AI"
    pis = item(p, "Piscine")
    assert (pis["word"], pis["source"], pis["why"]["by"]) == ("school", "user", "team"), "on a tag that is said"
    # Without the team's words (the default lookup), only its label: nothing
    # said, so it shows at a clear time, off any word (an add always shows).
    alone = I.plan(el, ctx_for(w, d, track=track()))
    assert "Piscine" in shown(alone) and item(alone, "Piscine")["w"] is None
    # A teammate re-tags it: the plan's key moves (Generate never freezes a
    # plan the studio didn't show).
    retagged = {**PISCINE, "tags": ["bootcamp"]}
    q = I.plan(el, ctx_for(w, d, track=track(), terms=team_terms([retagged]), team=[retagged]))
    assert q.timing_key != p.timing_key and q.plan_key != p.plan_key
    row = next(i for i in q.ideas if i["glyph"]["id"] == TEAM_ID)
    assert row["w"] is None, "not said: it shows at a clear time, off any word"
    # No team icon added: no words in the key — every other key as it was.
    plain = icon_el(style="circle", added=[{"glyph": {"source": "library", "id": "graduation-cap"},
                                            "label": "School"}])
    a = I.plan(plain, ctx_for(w, d, track=track(), terms=team_terms([PISCINE])))
    b = I.plan(plain, ctx_for(w, d, track=track()))
    assert a.timing_key == b.timing_key and a.plan_key == b.plan_key
    # The studio's lookup: a library icon's words, a team icon's name, label
    # and tags, nothing for one it doesn't know.
    terms = team_terms([PISCINE])
    assert terms(TEAM_ID) == ["Piscine", "Piscine", "bootcamp", "school"]
    assert terms("c-ffffffffffff") == [] and "coding" in terms("code")


# ---------------------------------------------------------------------------
# Hopeful words: suggestions, never refusals — and never a key moved
# ---------------------------------------------------------------------------

def test_a_hopeful_word_the_ai_refused_is_offered_and_no_key_moves(monkeypatch):
    w = words(815)
    d = judged(w, FREE_CODE)
    p = I.plan(icon_el(style="circle"), ctx_for(w, d, track=track()))
    rocket = next(i for i in p.ideas if i["glyph"]["id"] == "rocket-launch")
    assert (rocket["state"], rocket["auto"], rocket["why"]["by"]) == ("available", False, "hopeful")
    assert "Launch" not in shown(p) and "rocket-launch" not in {it["glyph"]["id"] for it in p.items}
    # The lists only: the same keys and icons as with no conversion at all.
    monkeypatch.setattr(D, "as_suggestions", lambda body, words=None: body)
    before = I.plan(icon_el(style="circle"), ctx_for(w, d, track=track()))
    assert (p.timing_key, p.plan_key, p.items) == (before.timing_key, before.plan_key, before.items)
    assert next(i for i in before.ideas if i["glyph"]["id"] == "rocket-launch")["state"] == "refused"


def test_as_suggestions_turns_only_the_ais_own_refusal_of_a_hopeful_word_said_and_meant():
    w = words(815)
    d = judged(w, FREE_CODE)
    out = D.as_suggestions(d, w)
    by = {i["icon"]: i for i in out["ideas"]}
    assert (by["rocket-launch"]["verdict"], by["rocket-launch"]["hopeful"], by["rocket-launch"]["ai_reason"],
            by["rocket-launch"]["auto"]) == ("suggest", True, "filler", False)
    assert by["book-open"]["verdict"] == "refuse", "'learning' is generic, not hopeful"
    assert d["ideas"] != out["ideas"] and next(i for i in d["ideas"] if i["icon"] == "rocket-launch")[
        "verdict"] == "refuse", "a copy: the stored answer is untouched"
    # Nothing to change: the body itself.
    plain = judged(w, {**FREE_CODE, "rocket-launch": {"priority": 3, "label": "Launch"}})
    assert D.as_suggestions(plain, w) is plain
    assert D.as_suggestions({**d, "source": "keywords"}, w)["ideas"] == d["ideas"]
    # One of our rules refused it (a "no"), not the AI: stays refused.
    checked = {**d, "ideas": [{**i, "reason": D.WHY_NEGATED} if i["icon"] == "rocket-launch" else i
                              for i in d["ideas"]]}
    assert next(i for i in D.as_suggestions(checked, w)["ideas"] if i["icon"] == "rocket-launch")[
        "verdict"] == "refuse"
    # Said in a negation ("no future without..."): stays refused.
    neg = words_of("There is no future without teachers, so learn to code.")
    cands = D.candidates(neg)
    fut = [c for c in cands if c["icon"] == "rocket-launch"]
    if fut:
        body, _a = _answer(neg, {"rocket-launch": ("refuse", "filler", 3)})
        assert next(i for i in D.as_suggestions(body, neg)["ideas"] if i["icon"] == "rocket-launch")[
            "verdict"] == "refuse"
    for text, want in (("your future", True), ("the journey", True), ("dreams", True), ("learning", False),
                       ("free", False), ("success stories", True)):
        assert D.hopeful({"quote": text}) is want, text


def words_of(line: str) -> dict:
    from app.services.rebrand_harness import words as W

    body = W.build_words(None, line, None, 8.0)
    body["script"] = line
    return body


def test_the_new_prompt_asks_for_suggestions_and_the_key_is_as_it_was():
    """Hopeful words are a "suggest" verdict from this prompt on (new calls
    only): the prompt, the schema and the checks know it; DIRECTOR_VERSION
    and the key's parts do not move, so a scene already answered is never
    asked again (tests/test_icons_director_dryrun.py holds 815's live key)."""
    assert D.DIRECTOR_VERSION == 1
    assert D.DIRECTOR_SCHEMA["properties"]["ideas"]["items"]["properties"]["verdict"]["enum"] == \
        ["keep", "suggest", "refuse"]
    w = words(815)
    ctx = D.DirectorCtx(words=w, script="x", work_dir=Path("."), topic="t")
    prompt = D.build_prompt(ctx, D.candidates(w), D.keyword_catalog(), has_image=False)
    assert "Suggest (verdict suggest" in prompt and "future,\njourney" in prompt
    assert "(amazing, start, time, things, people)" in prompt, "future and journey are no longer filler"
    assert list(D.key_inputs(ctx, D.keyword_catalog())) == ["version", "words", "script", "topic", "others",
                                                             "catalog", "spots", "take", "model"]
    # The checks: a suggestion is never automatic; said as a "no" it is refused.
    body, answer = _answer(w, {"rocket-launch": ("suggest", "general", 3), "piggy-bank": ("keep", "", 5),
                               "code": ("keep", "", 4), "users-three": ("refuse", "x", 1),
                               "book-open": ("refuse", "x", 1), "puzzle-piece": ("refuse", "x", 1)})
    fut = next(i for i in body["ideas"] if i["icon"] == "rocket-launch")
    assert (fut["verdict"], fut["auto"], fut["reason"]) == ("suggest", False, "general")
    assert D.missing_verdicts(answer, D.candidates(w)) == []
    assert D.refused_by({**fut, "verdict": "refuse", "reason": D.WHY_NEGATED}, answer) == "check"
    t = wi(w, "teachers")
    neg = {"cand_id": "new", "concept": "teachers", "quote": "teachers", "w": t, "w_end": t, "mentions": [],
           "icon": "chalkboard-teacher", "label": "Teachers", "purpose": "", "priority": 3, "verdict": "suggest",
           "refuse_reason": "", "list_span": False}
    body, _a = _answer(w, {"piggy-bank": ("keep", "", 5)}, extra=[neg])
    got = next(i for i in body["ideas"] if i["icon"] == "chalkboard-teacher")
    assert (got["verdict"], got["reason"]) == ("refuse", D.WHY_NEGATED)
    # At most MAX_EXTRA suggestions the candidates missed.
    extra = [{**neg, "icon": icon, "quote": q, "w": wi(w, q), "w_end": wi(w, q), "concept": q}
             for icon, q in (("graduation-cap", "school"), ("hammer", "building"), ("buildings", "Dhabi"))]
    body, _a = _answer(w, {"piggy-bank": ("keep", "", 5)}, extra=extra)
    new = [i for i in body["ideas"] if i["cand_id"] is None]
    assert sum(i["verdict"] == "suggest" for i in new) <= D.MAX_EXTRA
    # An answer from before (keep / refuse only) validates exactly as it did.
    old = judged(w, FREE_CODE)
    assert [i["verdict"] for i in old["ideas"]] == ["refuse", "keep", "keep", "refuse", "keep", "keep"]


# ---------------------------------------------------------------------------
# The log at each Generate, and its report
# ---------------------------------------------------------------------------

def _director_with_everything(w: dict) -> dict:
    body, _a = _answer(w, {"piggy-bank": ("keep", "", 5), "code": ("keep", "", 4), "users-three": ("keep", "", 4),
                           "puzzle-piece": ("keep", "", 4), "book-open": ("refuse", "too generic", 2),
                           "rocket-launch": ("refuse", "filler", 3)})
    return body


def test_feedback_says_what_the_user_did_with_each_pick():
    w = words(815)
    d = _director_with_everything(w)
    props = rc.IconProps.model_validate({
        "added": [{"glyph": {"source": "library", "id": "code"}, "label": "Code"},
                  {"glyph": {"source": "library", "id": "rocket-launch"}, "label": "Your Future"},
                  {"glyph": {"source": "custom", "id": TEAM_ID}, "label": "Piscine"}],
        "dismissed": ["users-three"]})
    shown_ = [{"glyph": {"source": "library", "id": g}} for g in ("piggy-bank", "code", "rocket-launch")]
    rows = FB.feedback_of(d, props, shown_)
    out = {r["glyph"]: r for r in rows}
    assert {g: r["outcome"] for g, r in out.items()} == {
        "piggy-bank": "shown_ai", "code": "kept", "users-three": "removed", "puzzle-piece": "dropped",
        "rocket-launch": "added_refused", TEAM_ID: "added_team"}
    free = out["piggy-bank"]
    assert (free["ai_verdict"], free["ai_priority"], free["ai_auto"], free["shown"]) == ("keep", 5, True, True)
    said = next(i for i in d["ideas"] if i["icon"] == "piggy-bank")
    assert free["quote"] == "tuition-free" and free["said_at"] == round(said["s"], 3) and said["s"] > 2.5
    assert (free["director_key"], free["director_version"], free["model"], free["source"]) == \
        ("k", D.DIRECTOR_VERSION, "gemini-2.5-flash", "ai")
    assert out["puzzle-piece"]["shown"] is False and out[TEAM_ID]["ai_verdict"] is None
    assert set(r["outcome"] for r in rows) <= set(FB.OUTCOMES)
    # A library icon the AI never offered, and a suggestion it made.
    sugg, _a = _answer(w, {"piggy-bank": ("keep", "", 5), "book-open": ("suggest", "general", 2)})
    props = rc.IconProps.model_validate({"added": [
        {"glyph": {"source": "library", "id": "book-open"}, "label": "Learning"},
        {"glyph": {"source": "library", "id": "graduation-cap"}, "label": "School"}]})
    got = {r["glyph"]: r["outcome"] for r in FB.feedback_of(sugg, props, [])}
    assert got == {"piggy-bank": "dropped", "book-open": "added_suggested", "graduation-cap": "added_library"}


class _Plan:
    def __init__(self, director, resolved, frozen=None):
        self.director, self._resolved, self.frozen = director, resolved, frozen

    def resolved(self):
        return list(self._resolved)


def test_rows_are_written_after_generate_in_the_background_and_never_fail_it(guard):
    w = words(815)
    d = _director_with_everything(w)
    p = I.plan(icon_el(style="circle"), ctx_for(w, d, track=track()))
    recipe = I.materialise(rc.normalize({"target": "scene_0", "engine": "house", "elements": [
        {"id": "icons", "kind": "icon", "props": {"style": "circle"}}]}), p)
    exp = {"id": 91, "run_id": 815, "options": {"target": "scene_0"}}

    async def go():
        task = FB.after_generate(exp, recipe, p)
        assert task is not None
        return await task

    assert asyncio.run(go()) is True
    rows = guard["log"].rows
    assert rows and {r["version_id"] for r in rows} == {91} and {r["run_id"] for r in rows} == {815}
    assert {r["target"] for r in rows} == {"scene_0"}
    assert {r["glyph"]: r["outcome"] for r in rows}["piggy-bank"] == "shown_ai"
    # Nothing new decided (a version's icons kept), or no director: no rows.
    assert FB.rows_for(exp, recipe, _Plan(d, p.resolved(), frozen=p.resolved())) == []
    assert FB.rows_for(exp, recipe, object()) == []
    assert FB.after_generate(exp, recipe, None) is None

    # The table can't be reached: logged, never raised.
    class Down:
        async def insert_many(self, rows):
            raise OSError("the database is down")

    FB.set_store(Down())
    assert asyncio.run(FB.record(rows)) is False
    # Broken input: None, never an exception.
    assert FB.after_generate({"id": "x"}, recipe, p) is None


def test_generate_logs_the_picks_and_a_log_that_fails_never_fails_it(monkeypatch, guard):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt
    from app.services import pattern_library as lib
    from app.services import rebrand_pipeline as pipeline

    w = words(815)
    d = _director_with_everything(w)
    p = I.plan(icon_el(style="circle"), ctx_for(w, d, track=track()))

    async def final_icon_plan(run_id, target, recipe, base=None):
        return {"state": "ready", "plan_key": p.plan_key}, p

    async def start(run_id, raw, recipe=None, base=None):
        return {"id": 44, "run_id": run_id, "options": {"target": recipe.target}}

    async def lineage(e, fetch):
        return {"base": None, "layers": [], "depth": 0}

    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)
    monkeypatch.setattr(studio, "final_icon_plan", final_icon_plan)
    monkeypatch.setattr(pipeline, "start", start)
    monkeypatch.setattr(rt, "_lineage", lineage)
    app = FastAPI()
    app.include_router(rt.router)
    recipe = {"target": "scene_0", "engine": "house",
              "elements": [{"id": "icons", "kind": "icon", "props": {"style": "circle"}}]}
    with TestClient(app) as c:
        r = c.post("/api/rebrand/harness/versions",
                   json={"run_id": 815, "target": "scene_0", "recipe": recipe, "icons_plan_key": p.plan_key})
        assert r.status_code == 200, r.text
    rows = guard["log"].rows
    assert rows and {x["version_id"] for x in rows} == {44}

    class Down:
        async def insert_many(self, rows):
            raise OSError("the database is down")

    FB.set_store(Down())
    with TestClient(app) as c:
        r = c.post("/api/rebrand/harness/versions",
                   json={"run_id": 815, "target": "scene_0", "recipe": recipe, "icons_plan_key": p.plan_key})
        assert r.status_code == 200, "a log that fails never fails Generate"


def test_no_test_reaches_the_shared_database():
    """The conftest guard: in a test the feedback log and the team's icons
    are in memory, whatever a test forgets to set."""
    from app.services.rebrand_harness import icon_catalog as ic

    assert not isinstance(FB._store, FB.PgStore) and not isinstance(ic._store, ic.PgStore)


def test_the_report_counts_acceptance_per_director_version_and_what_is_added_back(tmp_path, capsys):
    rows = []
    for outcome, n, concept in (("shown_ai", 6, "free"), ("kept", 2, "code"), ("removed", 2, "team"),
                                ("dropped", 3, "projects"), ("added_refused", 3, "future"),
                                ("added_suggested", 1, "future"), ("added_library", 1, "school"),
                                ("added_team", 2, "piscine")):
        rows += [{"director_version": 1, "model": "gemini-2.5-flash", "outcome": outcome, "concept": concept,
                  "label": concept.title(), "glyph": concept} for _ in range(n)]
    rows.append({"director_version": 1, "model": None, "outcome": "added_library", "concept": "rocket"})
    s = REP.summary(rows)
    v = next(x for x in s["versions"] if x["model"] == "gemini-2.5-flash")
    assert (v["picks"], v["accepted"], v["kept"], v["removed"], v["dropped"]) == (13, 8, 2, 2, 3)
    assert v["rate"] == round(8 / 10, 3)
    assert v["added"] == {"added_refused": 3, "added_suggested": 1, "added_library": 1, "added_team": 2}
    assert s["removed_most"] == [("team", 2)] and s["added_back_most"][0] == ("future", 4)
    assert s["team_added"] == [("Piscine", 2)]
    text = REP.render(s)
    assert "acceptance 80 %" in text and "future" in text
    assert REP.render(REP.summary([])).startswith("0 icon rows")
    # From a file (no database): the same numbers.
    f = tmp_path / "rows.json"
    f.write_text(json.dumps(rows), encoding="utf-8")
    assert REP.main(["--json", str(f), "--as-json"]) == 0
    assert json.loads(capsys.readouterr().out)["versions"][0]["picks"] in (0, 13)


# ---------------------------------------------------------------------------
# A chip's grow
# ---------------------------------------------------------------------------

def _chip(entrance="grow", exit_=None, *, label="Tuition-Free", span_f=45, style="chip"):
    r = PAR.resolved_item("piggy-bank", style, 0.16, label, entrance=entrance, span_f=span_f)
    if exit_:
        r["fx"]["exit"] = exit_
    return rc.IconResolved.model_validate(r).model_dump(mode="json")


def test_the_grow_numbers():
    fin, fout, end = 8, 6, 45
    got = [ir.fx_progress("grow", f, end=end, fin=fin, fout=fout) for f in range(-1, end + 1)]
    assert got[0] is None and got[-1] is None
    ins = got[1:fin + 1]
    pops = [p for p in ins if p.unfold == 0.0]
    assert len(pops) == 3 and all(p.words == 0.0 for p in pops), "the circle pops first"
    assert pops[0].scale < pops[1].scale and pops[-1].alpha == 1.0
    opens = [p.unfold for p in ins if p.unfold > 0]
    assert opens == sorted(opens) and opens[-1] == 1.0 and ins[-1].words == 1.0
    words_ = [p.words for p in ins]
    assert words_ == sorted(words_) and words_[:4] == [0.0] * 4, "the words come in once the pill is open"
    hold = got[fin + 1]
    assert (hold.phase, hold.unfold, hold.words, hold.scale) == ("hold", 1.0, 1.0, 1.0)
    outs = got[end - fout + 1:end + 1]
    assert [p.phase for p in outs] == ["out"] * fout
    assert [p.unfold for p in outs] == sorted([p.unfold for p in outs], reverse=True), "it folds back"
    assert outs[-1].unfold == 0.0 and outs[-1].alpha == 0.0
    assert ir.motion_for("circle", "grow") == "pop" and ir.motion_for("chip", "grow") == "grow"
    assert ir.motion_for("badge", "fade") == "fade" and ir.motion_for("circle", None) is None


def test_a_chip_grows_from_its_circle_and_rests_as_its_sprite():
    st = ir.Stamper([_chip()], fps=30, frame_size=(720, 1280))
    sp = st.sprite_of(0)
    widths, frames = [], []
    for f in range(0, 9):
        got = st.frame_of(0, f / 30)
        frames.append(got)
        cols = np.where(got.rgba[..., 3].max(axis=0) > 128)[0]
        widths.append(int(cols.max() - cols.min()) if len(cols) else 0)
    # Its circle: a square of the chip's height with its shadow, at the glyph's end.
    for got in frames[:3]:
        side = got.rgba.shape[1]
        assert abs(side - (sp.h + 2 * sp.pad)) <= 12 and got.rgba.shape[0] == side
    assert widths[3] > widths[2] and widths[3:8] == sorted(widths[3:8]), "then the pill unfolds"
    assert np.array_equal(frames[7].rgba, sp.rgba) and np.array_equal(frames[8].rgba, sp.rgba)
    # Deterministic: another Stamper draws the same bytes.
    twin = ir.Stamper([_chip()], fps=30, frame_size=(720, 1280))
    for f in range(0, 9):
        a, b = st.frame_of(0, f / 30), twin.frame_of(0, f / 30)
        assert (a.dx, a.dy) == (b.dx, b.dy) and np.array_equal(a.rgba, b.rgba)
    # Going: it folds back to its circle, then pops away.
    outs = [st.frame_of(0, f / 30) for f in range(39, 45)]
    assert outs[0].rgba.shape == sp.rgba.shape and outs[-1].rgba.shape[1] < sp.rgba.shape[1]
    # Words that read right to left grow from the right.
    ar = ir.Stamper([_chip(label="مطور")], fps=30, frame_size=(720, 1280))
    a0 = ar.frame_of(0, 0.0)
    sa = ar.sprite_of(0)
    assert a0.dx == pytest.approx(sa.w - sa.h, abs=12), "its circle is at the right"


def test_a_grow_fully_open_is_the_chip_and_any_other_style_grows_as_a_pop():
    font = I.label_key()
    glyph = ir.library_glyph("library", "piggy-bank", "bold")
    full = ir.chip_grown(glyph, 85, "Tuition-Free", font, "#7f2bf2", "#ffffff", max_w=662.4)
    chip = ir.chip_sprite(glyph, 85, "Tuition-Free", font, "#7f2bf2", "#ffffff", max_w=662.4)
    assert full.rgba.shape == chip.rgba.shape and (full.pad, full.w, full.h) == (chip.pad, chip.w, chip.h)
    assert int(np.abs(full.rgba.astype(int) - chip.rgba.astype(int)).max()) <= 2
    # A circle asked to grow pops, byte for byte.
    grow = ir.Stamper([_chip(style="circle")], fps=30, frame_size=(720, 1280))
    pop = ir.Stamper([_chip("pop", style="circle")], fps=30, frame_size=(720, 1280))
    frame = np.full((1280, 720, 3), 90, np.uint8)
    for f in range(0, 45):
        assert np.array_equal(grow.under(frame, f / 30), pop.under(frame, f / 30)), f
    # The planner hands a chip's own grow to the run.
    w = words(815)
    el = icon_el(style="chip", looks={"piggy-bank": {"enter": "grow", "leave": "fade"}})
    p = I.plan(el, ctx_for(w, judged(w, FREE_CODE), track=track()))
    free = next(r for r in p.resolved() if r["glyph"]["id"] == "piggy-bank")
    assert free["fx"]["entrance"] == "grow" and free["fx"]["exit"] == "fade"


# ---------------------------------------------------------------------------
# The parity report
# ---------------------------------------------------------------------------

def test_the_parity_page_holds_every_case_with_the_runs_own_sprites(tmp_path):
    cs = PAR.cases()
    assert len(cs) == 4 * 2 * 3 + 1 and len({c.id for c in cs}) == len(cs)
    assert all(c.checks == {"png_is_sprite": True, "still_is_run_frame": True} for c in cs)
    chip = next(c for c in cs if c.id == "piggy-bank-chip-M")
    stamps = ir.Stamper([chip.resolved], fps=30, frame_size=(720, 1280))
    assert np.array_equal(chip.sprite.rgba, stamps.sprite_of(0).rgba)
    grow = PAR.grow_frames()
    assert grow["frames"][0]["unfold"] == 0.0 and any(f["phase"] == "out" for f in grow["frames"])
    out = PAR.build(tmp_path / "parity.html", with_bundle=False)
    body = out.read_text(encoding="utf-8")
    assert "__DATA__" not in body and "__SCRIPT__" not in body and "__CHECK_ROWS__" not in body
    data = json.loads(re.search(r"const DATA = (\{.*?\});\n", body, re.S).group(1).replace("<\\/", "</"))
    assert [c["id"] for c in data["cases"]] == [c.id for c in cs]
    first = data["cases"][0]
    png = Image.open(io.BytesIO(__import__("base64").b64decode(first["url"].split(",", 1)[1])))
    assert np.array_equal(np.asarray(png), cs[0].sprite.rgba)
    assert data["thresholds"] == {"iou": 0.995, "mean": 1.5, "p95": 4.0}
    assert "every sprite and still is the run's own" in body


def test_the_new_modules_call_no_model():
    here = Path(PAR.__file__).parent
    for name in ("icon_feedback.py", "icon_director_report.py", "icon_parity.py"):
        src = (here / name).read_text(encoding="utf-8")
        for call in ("gemini_image(", "gemini_multimodal(", "gemini_text(", "person_mask(", "edit_video("):
            assert call not in src, (name, call)
