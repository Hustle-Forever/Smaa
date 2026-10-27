"""Rebrand studio icons, the director (rebrand_harness/director.py): one
Gemini call per scene, and every rule that keeps its answer honest — a
verdict for every keyword candidate, a verbatim quote, negations to the end
of their clause on what was SAID, idioms, the brand, lists, one icon per idea,
the auto floor, the keyword fallback (suggestions only) and the cache.

Gemini is always faked; the autouse guard makes any unfaked model call
raise. The words come from the real 806/815 Whisper fixtures or from the
char-linear estimate of a line."""
from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import numpy as np
import pytest

from app.config import config
from app.services import brand_icons as bi
from app.services import vertex_client
from app.services.rebrand_harness import director as D
from app.services.rebrand_harness import speaker_track as T
from app.services.rebrand_harness import words as W

cv2 = pytest.importorskip("cv2")
FIX = Path(__file__).parent / "fixtures" / "icons_words"


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    async def refuse(*_a, **_k):
        raise AssertionError("a paid model call in a test")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse)
    monkeypatch.setattr(vertex_client, "gemini_image", refuse)
    D._INFLIGHT.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def words_815() -> dict:
    fx = json.loads((FIX / "815.json").read_text(encoding="utf-8"))
    env = W.shape_envelope(fx["env"]["rms"], fx["env"]["thr"], fx["env"]["peak"], fx["env"]["voiced"])
    body = W.build_words(fx["chunks"], fx["dialogue"], None, fx["duration"], env=env)
    body["script"] = fx["dialogue"]
    return body


def words_for(line: str) -> dict:
    body = W.build_words(None, line, None, 6.0)
    body["script"] = line
    return body


def wi(words: dict, text: str, n: int = 0) -> int:
    return [w["w"] for w in words["words"] if w["said"] and w["text"].lower() == text.lower()][n]


def CAT(**kw) -> D.Catalog:
    base = D.keyword_catalog()
    resolve = kw.pop("resolve", base.resolve)
    return D.Catalog(version="test-1", picks=base.picks, exists=base.exists, resolve=resolve)


def idea(cand_id, quote, w, icon, verdict="keep", priority=4, **kw) -> dict:
    return {"cand_id": cand_id, "concept": kw.get("concept", icon), "quote": quote, "w": w,
            "w_end": kw.get("w_end", w + len(quote.replace("-", " ").split()) - 1),
            "mentions": kw.get("mentions", []), "icon": icon, "label": kw.get("label", icon[:16]),
            "purpose": kw.get("purpose", "Why it helps"), "priority": priority, "verdict": verdict,
            "refuse_reason": kw.get("refuse_reason", "filler" if verdict == "refuse" else ""),
            "list_span": kw.get("list_span", False)}


def judge_all(words: dict, keep: dict, *, extra=(), room=None) -> dict:
    """An answer refusing every candidate except `keep` {icon: overrides}."""
    ideas = []
    for c in D.candidates(words):
        o = keep.get(c["icon"])
        if o is None:
            ideas.append(idea(c["id"], c["quote"], c["w"], c["icon"], "refuse", 1))
        else:
            ideas.append(idea(c["id"], o.get("quote", c["quote"]), o.get("w", c["w"]), o.get("icon", c["icon"]),
                              "keep", o.get("priority", 4), **{k: v for k, v in o.items()
                                                                if k not in ("quote", "w", "icon", "priority")}))
    ideas += list(extra)
    return {"ideas": ideas, "room": room or {"spots": [], "home": 0, "home_why": ""}}


class Fake:
    """gemini_multimodal, scripted: each call takes the next answer (the last
    one repeats); an Exception is raised; a callable gets the prompt."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def __call__(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return copy.deepcopy(a(prompt) if callable(a) else a)


def ctx(words: dict, tmp_path: Path, **kw) -> D.DirectorCtx:
    return D.DirectorCtx(words=words, script=words["script"], work_dir=kw.pop("work_dir", tmp_path / "icons"),
                         catalog=kw.pop("catalog", CAT()), **kw)


def run(c: D.DirectorCtx) -> dict:
    return asyncio.run(D.direct(c))


def by_icon(body: dict, icon: str) -> dict:
    return next(i for i in body["ideas"] if i["icon"] == icon)


def frame(tmp_path: Path) -> Path:
    img = np.full((1280, 720, 3), 150, np.uint8)
    p = tmp_path / "frame0.png"
    cv2.imwrite(str(p), img)
    return p


# ---------------------------------------------------------------------------
# brand_icons' said-transcript rules (new helpers; find_hits is unchanged)
# ---------------------------------------------------------------------------

def _neg(text: str, word: str) -> bool:
    return bi.negated_in_clause(text, text.index(word))


def test_a_negation_reaches_the_end_of_its_clause():
    assert _neg("You don't need a degree or any coding experience.", "coding")
    assert _neg("You don't need a degree, coding experience or a laptop.", "laptop")
    assert _neg("You'll never sit in a classroom.", "classroom")                   # 3 words away
    assert _neg("Forget degrees: companies hire for skills.", "degrees")
    assert not _neg("Forget degrees: companies hire for skills.", "companies")     # a new clause
    assert _neg("Stop waiting for a teacher.", "teacher")
    assert _neg("Nobody cares about your degree.", "degree")
    assert _neg("It's not about grades.", "grades")
    assert _neg("You'll never have to pay fees.", "fees")                          # "have to" still negated


def test_a_negation_stops_at_a_turn_a_clause_comma_or_a_purpose():
    s = "42 Abu Dhabi: a coding school without teachers, building real projects."
    assert _neg(s, "teachers") and not _neg(s, "projects")
    assert not _neg("Not about grades but skills.", "skills")
    assert not _neg("You don't need a degree to code.", "code")
    assert not _neg("It's not only free, it's fun.", "free")
    assert not _neg("The one-stop shop for coding.", "coding")
    assert not _neg("From zero to coder in a year.", "coder")


def test_idioms_and_the_brand():
    assert bi.in_idiom("Feel free to ask questions.", 5)
    assert not bi.in_idiom("Feel free to ask questions.", "Feel free to ask questions.".index("questions"))
    assert bi.in_idiom("The dress code is simple.", "The dress code is simple.".index("code"))
    assert not bi.in_idiom("It's free to apply.", "It's free to apply.".index("free"))
    assert not bi.in_idiom("A tuition-free school.", 2)
    assert bi.is_brand("42 Abu Dhabi") and bi.is_brand("forty-two") and not bi.is_brand("fortune")


def test_find_hits_said_against_the_classic_table():
    line = "Feel free to ask questions, and forget degrees."
    assert [h.icon for h in bi.find_hits(line)] == ["piggy-bank", "question", "graduation-cap"]
    assert [h.icon for h in bi.find_hits_said(line)] == ["question"]
    raw = bi.keyword_spans("A tuition-free school, free forever.", "piggy-bank")
    assert [m.pos for m in raw] == [2, 10, 23]                                    # every raw match
    kept = bi.find_mentions("A tuition-free school, free forever.")
    assert [(m.icon, m.pos) for m in kept] == [("piggy-bank", 2), ("piggy-bank", 23)]


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def test_candidates_come_from_the_said_transcript_with_ids():
    w = words_815()
    cands = D.candidates(w)
    assert [(c["id"], c["icon"]) for c in cands] == [
        ("k1", "rocket-launch"), ("k2", "piggy-bank"), ("k3", "users-three"), ("k4", "book-open"),
        ("k5", "code"), ("k6", "puzzle-piece")]
    free = cands[1]
    assert free["quote"] == "tuition-free" and free["w"] == wi(w, "tuition") and free["w_end"] == wi(w, "free")
    assert free["strong"] and not cands[0]["strong"]
    assert "chalkboard-teacher" not in [c["icon"] for c in cands]                  # "without teachers"


@pytest.mark.parametrize("line, absent", [
    ("Forget degrees: companies hire for skills.", "graduation-cap"),
    ("You don't need a degree or any coding experience.", "code"),
    ("You'll never sit in a classroom.", "chalkboard-teacher"),
    ("Feel free to ask questions.", "piggy-bank"),
    ("The dress code is simple.", "code"),
    ("Stop waiting for a teacher.", "chalkboard-teacher"),
])
def test_the_critiques_break_lines_give_no_such_candidate(line, absent):
    assert absent not in [c["icon"] for c in D.candidates(words_for(line))]
    # ...while the classic table (unchanged) still finds it on the script.
    assert absent in [h.icon for h in bi.find_hits(line)]


def test_a_list_said_together_is_found():
    w = words_for("Learn code, design, AI, games and cybersecurity with us.")
    groups = D.list_groups(w, D.candidates(w))
    assert groups == [(wi(w, "code"), wi(w, "cybersecurity"))]
    w = words_815()
    assert D.list_groups(w, D.candidates(w)) == []                                 # "tuition-free, peer-"


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def _schema_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "OBJECT":
            yield node
        for v in node.values():
            yield from _schema_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _schema_objects(v)


def test_the_schema_requires_every_field():
    objs = list(_schema_objects(D.DIRECTOR_SCHEMA))
    assert len(objs) == 5
    for o in objs:
        assert set(o["required"]) == set(o["properties"])


def test_one_call_fast_model_temperature_zero_and_the_numbered_room(tmp_path, monkeypatch):
    w = words_815()
    spots = T.spot_grid(720, 1280)
    fake = Fake(judge_all(w, {"piggy-bank": {"priority": 5}},
                          room={"spots": [{"n": 1, "verdict": "good", "why": "plain wall"}], "home": 1,
                                "home_why": "Left, beside the shoulder"}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path, frame0=frame(tmp_path), spots=spots, topic="Why 42", others=["Scene two."]))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["temperature"] == 0.0 and call["thinking_budget"] == 512
    assert call["model"] == config.vertex_gemini_fast_model and call["json_schema"] is D.DIRECTOR_SCHEMA
    assert call["system"] == D.DIRECTOR_SYSTEM
    room = Path(call["images"][0])
    assert room.is_file() and cv2.imread(str(room)).shape[1] == D.ROOM_WIDTH
    p = call["prompt"]
    assert f"{wi(w, 'coding')}:coding" in p and "k2: icon piggy-bank" in p and "TOPIC: Why 42" in p
    assert "- Scene two." in p and "piggy-bank: Free" in p
    assert body["source"] == "ai" and body["room"]["home"] == "L1" and body["room"]["home_n"] == 1


def test_815_the_director_keeps_free_and_code_and_refuses_the_filler(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(judge_all(w, {
        "piggy-bank": {"priority": 5, "label": "Free", "purpose": "It costs nothing — the line's hook"},
        "code": {"priority": 4, "mentions": [{"w": wi(w, "coding"), "quote": "coding"}]},
        "users-three": {"priority": 3, "quote": "peer-learning", "purpose": "Learn from peers, not teachers"},
        "puzzle-piece": {"priority": 3},
    }))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    auto = [i["icon"] for i in D.auto_ideas(body)]
    assert auto == ["piggy-bank", "code", "users-three", "puzzle-piece"]
    free = by_icon(body, "piggy-bank")
    assert (free["w"], free["w_end"], free["quote"], free["word"]) == (wi(w, "tuition"), wi(w, "free"),
                                                                      "tuition-free", "tuition")
    assert free["s"] == pytest.approx(3.30, abs=0.01) and free["label"] == "Free"
    assert by_icon(body, "users-three")["quote"] == "peer-learning"
    assert by_icon(body, "rocket-launch")["verdict"] == "refuse" and not by_icon(body, "rocket-launch")["auto"]
    assert all(i["id"] for i in body["ideas"]) and [i["w"] for i in body["ideas"]] == sorted(i["w"] for i in body["ideas"])


# ---------------------------------------------------------------------------
# The critique's break cases, with the AI trying to keep them
# ---------------------------------------------------------------------------

def _keep_extra(tmp_path, monkeypatch, line: str, quote: str, word: str, icon: str) -> dict:
    w = words_for(line)
    fake = Fake(judge_all(w, {}, extra=[idea(None, quote, wi(w, word), icon, "keep", 5)]))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    return next(i for i in body["ideas"] if i["icon"] == icon and i["cand_id"] is None)


def test_forget_degrees_is_refused_as_a_negation(tmp_path, monkeypatch):
    got = _keep_extra(tmp_path, monkeypatch, "Forget degrees: companies hire for skills.", "Forget degrees",
                      "Forget", "graduation-cap")
    assert got["verdict"] == "refuse" and got["reason"] == "said as a negation" and not got["auto"]


def test_no_degree_or_any_coding_experience_is_refused(tmp_path, monkeypatch):
    got = _keep_extra(tmp_path, monkeypatch, "You don't need a degree or any coding experience.",
                      "coding experience", "coding", "code")
    assert got["verdict"] == "refuse" and got["reason"] == "said as a negation"


def test_feel_free_is_an_idiom_not_a_price(tmp_path, monkeypatch):
    got = _keep_extra(tmp_path, monkeypatch, "Feel free to ask questions.", "free", "free", "piggy-bank")
    assert got["verdict"] == "refuse" and got["reason"] == "an idiom, not the idea"


def test_the_dress_code_is_not_code(tmp_path, monkeypatch):
    got = _keep_extra(tmp_path, monkeypatch, "The dress code is simple.", "dress code", "dress", "code")
    assert got["verdict"] == "refuse" and got["reason"] == "an idiom, not the idea"


def test_no_fees_is_still_a_piggy_bank(tmp_path, monkeypatch):
    w = words_for("No tuition, no fees, just code.")
    fake = Fake(judge_all(w, {"piggy-bank": {"priority": 5, "quote": "No tuition"}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    assert by_icon(body, "piggy-bank")["verdict"] == "keep" and by_icon(body, "piggy-bank")["auto"]


def test_a_long_phrase_is_not_refused_for_a_negation_at_its_end(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(judge_all(w, {"code": {"priority": 4, "quote": "coding school without teachers"}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    code = by_icon(body, "code")
    assert code["verdict"] == "keep" and code["w_end"] == wi(w, "teachers")


def test_the_brand_is_never_an_icon(tmp_path, monkeypatch):
    got = _keep_extra(tmp_path, monkeypatch, "42 Abu Dhabi opens its doors.", "Abu Dhabi", "Abu", "map-pin")
    assert got["verdict"] == "refuse" and got["reason"] == "the brand, not an idea"


# ---------------------------------------------------------------------------
# Verdicts and quotes
# ---------------------------------------------------------------------------

def test_a_missing_verdict_asks_again_then_refuses_that_candidate(tmp_path, monkeypatch):
    w = words_815()
    full = judge_all(w, {"piggy-bank": {"priority": 5}, "code": {"priority": 5}})
    short = copy.deepcopy(full)
    short["ideas"] = [i for i in short["ideas"] if i["cand_id"] != "k5"]               # no word on code
    fake = Fake(short, short)
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    assert len(fake.calls) == 2
    assert "no verdict for k5" in fake.calls[1]["prompt"]
    code = by_icon(body, "code")
    assert code["verdict"] == "refuse" and code["reason"] == "no verdict from the AI" and not code["auto"]
    assert by_icon(body, "piggy-bank")["auto"]


def test_a_missing_verdict_answered_on_the_second_ask_is_used(tmp_path, monkeypatch):
    w = words_815()
    full = judge_all(w, {"piggy-bank": {"priority": 5}, "code": {"priority": 5}})
    short = copy.deepcopy(full)
    short["ideas"] = [i for i in short["ideas"] if i["cand_id"] != "k5"]
    fake = Fake(short, full)
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    assert len(fake.calls) == 2 and by_icon(body, "code")["auto"]


def test_a_quote_that_is_not_what_was_said_is_refused(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(judge_all(w, {"code": {"priority": 5, "quote": "coding bootcamp"}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    code = by_icon(body, "code")
    assert code["verdict"] == "refuse" and code["reason"] == "the quote is not what was said there"
    assert code["w"] == wi(w, "coding")                                              # shown on the table's word


def test_an_index_slip_of_up_to_three_words_is_corrected(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(judge_all(w, {"code": {"priority": 5, "w": wi(w, "coding") - 2},
                              "puzzle-piece": {"priority": 4, "w": wi(w, "projects") - 5}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    assert by_icon(body, "code")["verdict"] == "keep" and by_icon(body, "code")["w"] == wi(w, "coding")
    assert by_icon(body, "puzzle-piece")["verdict"] == "refuse"                     # 5 words off


def test_mentions_are_only_the_ones_really_said(tmp_path, monkeypatch):
    w = words_for("Free school, free food, and free forever. Really free.")
    free_w = [x["w"] for x in w["words"] if x["text"].lower() == "free"]
    fake = Fake(judge_all(w, {"piggy-bank": {"priority": 5, "w": free_w[2], "mentions": [
        {"w": free_w[0], "quote": "Free school"}, {"w": free_w[1], "quote": "free food"},
        {"w": free_w[3], "quote": "free lunch"}]}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    free = by_icon(body, "piggy-bank")
    assert free["w"] == free_w[2]
    assert [m["w"] for m in free["mentions"]] == free_w[:3]                          # "free lunch" was not said
    assert free["table_mentions"] == free_w


# ---------------------------------------------------------------------------
# Icons, merges, lists, extras, text limits, the room
# ---------------------------------------------------------------------------

def test_other_icons_resolve_through_the_library_or_are_refused(tmp_path, monkeypatch):
    w = words_for("Grab a coffee and a hammer in our lab.")

    async def resolve(name):
        return {"coffee cup": ("coffee", 0.95), "hammer": ("hammer", 0.62)}.get(name)

    fake = Fake(judge_all(w, {"coffee": {"icon": "other:coffee cup", "priority": 4}}, extra=[
        idea(None, "hammer", wi(w, "hammer"), "other:hammer", "keep", 4),
        idea(None, "lab", wi(w, "lab"), "github-logo", "keep", 4)]))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path, catalog=CAT(resolve=resolve)))
    assert by_icon(body, "coffee")["verdict"] == "keep"
    hammer = next(i for i in body["ideas"] if i["w"] == wi(w, "hammer"))
    assert hammer["verdict"] == "refuse" and hammer["reason"] == "no icon for 'hammer'"
    lab = next(i for i in body["ideas"] if i["w"] == wi(w, "lab"))
    assert lab["verdict"] == "refuse"


def test_a_brand_logo_is_refused_even_when_it_resolves(tmp_path, monkeypatch):
    w = words_for("Push it to GitHub today.")
    fake = Fake(judge_all(w, {"git-branch": {"icon": "other:github", "priority": 5}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path, catalog=CAT(resolve=lambda n: ("github-logo", 1.0))))
    g = next(i for i in body["ideas"] if i["cand_id"] == "k1")
    assert g["verdict"] == "refuse" and g["reason"] == "a brand logo"


def test_one_idea_per_icon_the_better_mention_wins_and_takes_the_others(tmp_path, monkeypatch):
    w = words_for("It is free to join. Totally free, forever.")
    free_w = [x["w"] for x in w["words"] if x["text"].lower() == "free"]
    cands = D.candidates(w)
    assert [c["icon"] for c in cands] == ["piggy-bank"] and len(cands[0]["mentions"]) == 2
    fake = Fake({"ideas": [
        idea("k1", "free", free_w[0], "piggy-bank", "keep", 3),
        idea(None, "Totally free", free_w[1] - 1, "piggy-bank", "keep", 5),
    ], "room": {"spots": [], "home": 0, "home_why": ""}})
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    kept = [i for i in body["ideas"] if i["verdict"] == "keep"]
    assert len(kept) == 1 and kept[0]["w"] == free_w[1] - 1 and kept[0]["priority"] == 5
    assert free_w[0] in [m["w"] for m in kept[0]["mentions"]]
    loser = next(i for i in body["ideas"] if i["cand_id"] == "k1")
    assert loser["verdict"] == "refuse" and loser["reason"].startswith("same icon as")


def test_a_list_keeps_one_icon_at_most(tmp_path, monkeypatch):
    w = words_for("Learn code, design, AI, games and cybersecurity with us.")
    fake = Fake(judge_all(w, {"code": {"priority": 4}, "palette": {"priority": 3},
                              "robot": {"priority": 5}, "game-controller": {"priority": 4}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    kept = [i["icon"] for i in body["ideas"] if i["verdict"] == "keep"]
    assert kept == ["robot"]
    assert by_icon(body, "code")["reason"] == "part of a list — one icon for the whole list"


def test_a_list_the_ai_marks_keeps_one_icon(tmp_path, monkeypatch):
    w = words_for("You build websites or apps.")
    fake = Fake({"ideas": [
        idea(None, "websites", wi(w, "websites"), "globe-hemisphere-east", "keep", 4, list_span=True),
        idea(None, "apps", wi(w, "apps"), "laptop", "keep", 4, list_span=True),
    ], "room": {"spots": [], "home": 0, "home_why": ""}})
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    assert [i["icon"] for i in body["ideas"] if i["verdict"] == "keep"] == ["globe-hemisphere-east"]


def test_at_most_two_extra_ideas(tmp_path, monkeypatch):
    w = words_for("Hackathons with mentors. Then rockets.")
    assert D.candidates(w) == []
    extra = [idea(None, "Hackathons", wi(w, "Hackathons"), "trophy", "keep", 5),
             idea(None, "mentors", wi(w, "mentors"), "handshake", "keep", 4),
             idea(None, "rockets", wi(w, "rockets"), "rocket-launch", "keep", 3)]
    fake = Fake(judge_all(w, {}, extra=extra))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    kept = {i["icon"] for i in body["ideas"] if i["verdict"] == "keep" and i["cand_id"] is None}
    assert kept == {"trophy", "handshake"}
    assert by_icon(body, "rocket-launch")["reason"] == "too many extra ideas"


def test_priority_is_clamped_and_text_is_trimmed(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(judge_all(w, {"code": {"priority": 9, "label": "A very long label indeed",
                                       "purpose": "x" * 200},
                              "piggy-bank": {"priority": -3}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    code = by_icon(body, "code")
    assert code["priority"] == 5 and len(code["label"]) <= D.LABEL_MAX and len(code["purpose"]) <= D.PURPOSE_MAX
    assert by_icon(body, "piggy-bank")["priority"] == 1


def test_the_room_drops_unknown_spots_and_an_avoided_home(tmp_path, monkeypatch):
    spots = T.spot_grid(720, 1280)
    room = D.validate_room({"spots": [{"n": 1, "verdict": "good", "why": "wall"},
                                      {"n": 5, "verdict": "avoid", "why": "a sign"},
                                      {"n": 5, "verdict": "good", "why": "dup"},
                                      {"n": 99, "verdict": "good", "why": "?"},
                                      {"n": 2, "verdict": "maybe", "why": "?"}],
                            "home": 5, "home_why": "x"}, spots, len(spots))
    assert [(s["n"], s["id"], s["verdict"]) for s in room["spots"]] == [(1, "L1", "good"), (5, "R1", "avoid")]
    assert room["home"] is None and room["home_n"] is None
    room = D.validate_room({"spots": [{"n": 1, "verdict": "good", "why": "wall"}], "home": 1,
                            "home_why": "Left of the speaker"}, spots, len(spots))
    assert room["home"] == "L1" and room["home_why"] == "Left of the speaker"


# ---------------------------------------------------------------------------
# The auto floor
# ---------------------------------------------------------------------------

def test_the_auto_floor(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(judge_all(w, {"rocket-launch": {"priority": 3}, "book-open": {"priority": 4},
                              "users-three": {"priority": 2}, "code": {"priority": 3}},
                          extra=[idea(None, "school", wi(w, "school"), "student", "keep", 3)]))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    assert not by_icon(body, "rocket-launch")["auto"]           # weak keyword at 3
    assert by_icon(body, "book-open")["auto"]                   # weak keyword at 4
    assert not by_icon(body, "users-three")["auto"]             # strong at 2
    assert by_icon(body, "code")["auto"]                        # strong at 3
    assert not by_icon(body, "student")["auto"]                 # the AI's own idea at 3
    assert [i["icon"] for i in D.auto_ideas(body)] == ["book-open", "code"]


# ---------------------------------------------------------------------------
# Without the AI
# ---------------------------------------------------------------------------

def test_ai_off_is_keyword_suggestions_only_and_free(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(AssertionError("no call"))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path, ai=False))
    assert fake.calls == [] and body["source"] == "keywords"
    assert {i["icon"] for i in body["ideas"]} == {c["icon"] for c in D.candidates(w)}
    assert all(i["verdict"] == "suggest" and not i["auto"] for i in body["ideas"])
    assert by_icon(body, "piggy-bank")["purpose"] == "Says 'tuition-free'"
    assert not (tmp_path / "icons" / "director.json").exists()


@pytest.mark.parametrize("line, allowed", [
    ("Forget degrees: companies hire for skills.", {"buildings", "briefcase"}),
    ("You don't need a degree or any coding experience.", set()),
    ("Feel free to ask questions.", {"question"}),
    ("The dress code is simple.", set()),
])
def test_when_the_ai_is_down_the_break_lines_suggest_nothing_wrong(line, allowed, tmp_path, monkeypatch):
    fake = Fake(RuntimeError("Vertex 503"))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(words_for(line), tmp_path))
    assert body["source"] == "keywords" and body["failed_at"] and "503" in body["error"]
    assert {i["icon"] for i in body["ideas"]} == allowed
    assert not any(i["auto"] for i in body["ideas"])


def test_a_failure_is_cached_and_retried_only_on_a_new_take_or_after_the_backoff(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(RuntimeError("quota"))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    first = run(ctx(w, tmp_path))
    assert len(fake.calls) == 2 and first["source"] == "keywords"                  # tried, and once more
    again = run(ctx(w, tmp_path))
    assert len(fake.calls) == 2 and again["failed_at"] == first["failed_at"]       # the warm-up poll
    saved = tmp_path / "icons" / "director.json"
    body = json.loads(saved.read_text(encoding="utf-8"))
    body["failed_at"] = time.time() - D.FAIL_BACKOFF_SEC - 1
    saved.write_text(json.dumps(body), encoding="utf-8")
    fake.answers = [judge_all(w, {"piggy-bank": {"priority": 5}})]
    later = run(ctx(w, tmp_path))
    assert len(fake.calls) == 3 and later["source"] == "ai"


def test_an_answer_that_breaks_validation_is_a_cached_failure_not_a_retry_loop(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(judge_all(w, {"code": {"priority": 5}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)

    def boom(*_a, **_k):
        raise KeyError("w")

    monkeypatch.setattr(D, "validate", boom)
    body = run(ctx(w, tmp_path))
    assert body["source"] == "keywords" and body["failed_at"] and body["error"].startswith("validation")
    assert not any(i["auto"] for i in body["ideas"])
    run(ctx(w, tmp_path))
    assert len(fake.calls) == 1


def test_an_unreadable_answer_is_asked_once_more(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake("not json at all", judge_all(w, {"code": {"priority": 5}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    assert len(fake.calls) == 2 and body["source"] == "ai" and by_icon(body, "code")["auto"]


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

def test_a_second_plan_makes_no_call_and_ask_again_makes_one(tmp_path, monkeypatch):
    w = words_815()
    fake = Fake(judge_all(w, {"piggy-bank": {"priority": 5}}))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    a = run(ctx(w, tmp_path))
    b = run(ctx(w, tmp_path))
    assert len(fake.calls) == 1 and a == b
    c = run(ctx(w, tmp_path, take=1))
    assert len(fake.calls) == 2 and c["key"] != a["key"]


def test_the_key_follows_every_input(tmp_path):
    w = words_815()
    spots = T.spot_grid(720, 1280)
    f0 = frame(tmp_path)
    base = D.DirectorCtx(words=w, script=w["script"], work_dir=tmp_path, topic="t", others=["o"],
                         frame0=f0, spots=spots, catalog=CAT())
    k = D.director_key(base, base.catalog)

    def with_(**kw):
        c = D.DirectorCtx(**{**base.__dict__, **kw})
        return D.director_key(c, c.catalog)

    assert with_(topic="u") != k and with_(others=["p"]) != k and with_(script="x") != k
    assert with_(take=1) != k and with_(spots=spots[:-1]) != k
    assert with_(words={**w, "digest": "other"}) != k
    other = D.Catalog(version="test-2", picks=base.catalog.picks, exists=base.catalog.exists)
    assert with_(catalog=other) != k
    assert D.director_key(base, base.catalog) == k


def test_two_plans_at_once_share_one_call(tmp_path, monkeypatch):
    w = words_815()
    answer = judge_all(w, {"piggy-bank": {"priority": 5}})
    calls = []

    async def slow(prompt, **kw):
        calls.append(1)
        await asyncio.sleep(0.05)
        return copy.deepcopy(answer)

    monkeypatch.setattr(vertex_client, "gemini_multimodal", slow)

    async def both():
        return await asyncio.gather(D.direct(ctx(w, tmp_path)), D.direct(ctx(w, tmp_path)))

    a, b = asyncio.run(both())
    assert len(calls) == 1 and a["key"] == b["key"]


def test_nothing_said_makes_no_call(tmp_path, monkeypatch):
    w = W.build_words(None, "", None, 3.0)
    w["script"] = ""
    fake = Fake(AssertionError("no call"))
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = run(ctx(w, tmp_path))
    assert fake.calls == [] and body["ideas"] == [] and body["note"] == "nothing said"


def test_the_keyword_catalogue_resolves_names_and_ids():
    cat = D.keyword_catalog()
    assert cat.exists("piggy-bank") and not cat.exists("hammer")
    assert cat.resolve("piggy bank") == ("piggy-bank", 1.0)
    assert cat.resolve("coding")[0] == "code"
    assert len(cat.picks) == len(bi.icon_ids())


def test_the_library_catalogue_is_icon_catalogs_library_ids_only():
    from app.services.rebrand_harness import icon_catalog as ic

    cat = D.default_catalog()
    assert cat.version == ic.catalogue_version()
    assert [p["id"] for p in cat.picks] == list(ic.PICK_IDS)
    assert cat.exists("code") and cat.exists("money")          # a pick, and a library id beyond them
    assert not cat.exists("apple-logo")                        # brand logos are not in the library
    # A drawn glyph is the user's to add, never the director's: a Stamper can
    # draw one only after it is fetched from the database.
    assert not cat.exists(ic.custom_id("halal food"))
    # "other:<noun>" resolves through the studio's own free search (the guard
    # fails any model call): the synonym table knows "school" now (the live
    # 815 answer refused it at 0.77, a library gap).
    assert asyncio.run(D._resolve(cat, "cash")) == ("money", 0.99)
    assert asyncio.run(D._resolve(cat, "cyber cafe"))[1] < D.MATCH_STRONG
    school = asyncio.run(D._resolve(cat, "school"))
    assert school[0] == "graduation-cap" and school[1] >= D.MATCH_STRONG
    assert asyncio.run(D._resolve(cat, "no teachers"))[1] < D.MATCH_STRONG
