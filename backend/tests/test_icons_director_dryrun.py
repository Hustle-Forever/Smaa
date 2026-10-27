"""Rebrand studio icons, the director checked for free (rebrand_harness
director.py + director_dryrun.py): the model's raw answer and what it was
asked kept beside its ideas without moving the key (no scene is asked again),
explain() (every keyword matched, and why each dropped one was), replay() (a
stored answer judged by today's rules, for free), refused_by(), and the dry
run's report of every local scene — which never calls a model and never
writes into the studio's folders.

Gemini is always faked; the autouse guard makes any unfaked model call
raise. The words come from the real 806/815 Whisper fixtures; run 815's real
studio folder is used when it is on this machine (read only)."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest

from app import config as app_config
from app.config import config
from app.services import brand_icons as bi
from app.services import vertex_client
from app.services.rebrand_harness import director as D
from app.services.rebrand_harness import director_dryrun as DR
from app.services.rebrand_harness import speaker_track as T
from app.services.rebrand_harness import words as W

cv2 = pytest.importorskip("cv2")
FIX = Path(__file__).parent / "fixtures" / "icons_words"
DATA = Path(__file__).resolve().parents[1] / "data" / "yas_producer"
S815 = DATA / "talking-head-v2" / "rebrand" / "_studio" / "815" / "scene_0_84f81b9967"
# Run 815's words as the studio gave them to the director (read once, read
# only, from yas_generations 815: a one-scene run, so no other lines).
TOPIC_815 = "42\nYour Future, Uncoded."
KEY_815 = "b5eddf666b8ac944a270"
MODEL_815 = "gemini-2.5-flash"


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    async def refuse(*_a, **_k):
        raise AssertionError("a paid model call in a test")

    for name in DR.PAID_CALLS:
        if hasattr(vertex_client, name):
            monkeypatch.setattr(vertex_client, name, refuse)
    D._INFLIGHT.clear()
    DR.ATTEMPTS.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pin_model(monkeypatch, model: str = MODEL_815) -> None:
    """The fast model the key was made with (config is frozen: swapped whole;
    the director reads it when it makes a key)."""
    monkeypatch.setattr(app_config, "config", dataclasses.replace(app_config.config, vertex_gemini_fast_model=model))


def fixture_words(run: str) -> dict:
    fx = json.loads((FIX / f"{run}.json").read_text(encoding="utf-8"))
    env = W.shape_envelope(fx["env"]["rms"], fx["env"]["thr"], fx["env"]["peak"], fx["env"]["voiced"])
    return W.build_words(fx["chunks"], fx["dialogue"], None, fx["duration"], env=env)


def line_of(run: str) -> str:
    return json.loads((FIX / f"{run}.json").read_text(encoding="utf-8"))["dialogue"]


def wi(words: dict, text: str, n: int = 0) -> int:
    return [w["w"] for w in words["words"] if w["said"] and w["text"].lower() == text.lower()][n]


# Library ids a resolver below may answer with (the real catalogue has them
# all; the keyword table's does not).
LIBRARY = {"money", "hammer", "chalkboard-teacher", "graduation-cap"}


def NO_SCHOOL(name: str):
    """The resolver as the live call had it: "school" only 0.77, under the bar."""
    return ("chalkboard-teacher", 0.77) if name == "school" else None


def CAT(resolve=None) -> D.Catalog:
    base = D.keyword_catalog()
    return D.Catalog(version="test-1", picks=base.picks, exists=lambda i: base.exists(i) or i in LIBRARY,
                     resolve=resolve or base.resolve)


def idea(cand_id, quote, w, icon, verdict="keep", priority=4, **kw) -> dict:
    return {"cand_id": cand_id, "concept": kw.get("concept", icon), "quote": quote, "w": w,
            "w_end": kw.get("w_end", w + len(quote.replace("-", " ").split()) - 1),
            "mentions": kw.get("mentions", []), "icon": icon, "label": kw.get("label", icon[:16]),
            "purpose": kw.get("purpose", "Why it helps"), "priority": priority, "verdict": verdict,
            "refuse_reason": kw.get("refuse_reason", "filler" if verdict == "refuse" else ""),
            "list_span": kw.get("list_span", False)}


def judge_all(words: dict, keep: dict, *, extra=(), room=None, skip=()) -> dict:
    """An answer refusing every candidate except `keep` {icon: overrides};
    `skip`: candidate ids left without a verdict."""
    ideas = []
    for c in D.candidates(words):
        if c["id"] in skip:
            continue
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
    one repeats)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def __call__(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return copy.deepcopy(a)


def save_track(icons: Path) -> T.Track:
    """A speaker track with nobody in it: the full-bleed spot grid on a
    plain frame (what speaker_track.build leaves, without MODNet)."""
    icons.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(icons / "track_frame0.png"), np.full((1280, 720, 3), 150, np.uint8))
    spots = T.spot_grid(720, 1280)
    for s in spots:
        s["calm"] = 0.8
    n = 16
    T._save(T.Track(width=720, height=1280, fps=30.0, frames=240, step=15,
                    times=np.arange(n, dtype=np.float32) / 2, heads=np.full((n, 4), -1, np.int32),
                    masks=np.zeros((n, 160, 90), np.uint8), bars=None, layout="full", picture=(0, 1280, 0, 720),
                    spots=spots, frame0=icons / "track_frame0.png", key="test-track"), icons)
    return T.load(icons)


def room_of(n: int, home: int = 1) -> dict:
    return {"spots": [{"n": k, "verdict": "avoid" if k == 3 else "good", "why": f"spot {k}"} for k in range(1, n + 1)],
            "home": home, "home_why": "Beside the shoulder"}


def the_815_answer(words: dict, n_spots: int = 10) -> dict:
    """Like the live answer of 2026-09-28: Free, Team, Code and Projects kept,
    future and learning refused, "school" as the AI's own extra idea."""
    return judge_all(words, {
        "piggy-bank": {"priority": 5, "label": "Tuition-Free", "purpose": "No cost for education"},
        "users-three": {"priority": 4, "quote": "peer-learning", "label": "Peer Learning"},
        "code": {"priority": 5, "label": "Coding"},
        "puzzle-piece": {"priority": 4, "label": "Real Projects"},
    }, extra=[idea("new", "school", wi(words, "school"), "other:school", "keep", 3, label="School",
                concept="school")],
        room=room_of(n_spots))


def scene_with(tmp_path: Path, monkeypatch, run: str = "815", *, answer=None, catalog=None):
    """A studio folder as the studio leaves it: icons/words.json, a speaker
    track and — when `answer` is given — the director's answer to it
    (director.direct with Gemini faked). (LocalScene, Lines, words, catalog)."""
    work = tmp_path / "studio" / run / "scene_0_abcdef0123"
    icons = work / "icons"
    words = fixture_words(run)
    W._write(icons / "words.json", words)
    track = save_track(icons)
    lines = DR.Lines(script=line_of(run), topic="Why 42", others=[], source="given")
    catalog = catalog or CAT(NO_SCHOOL)
    if answer is not None:
        fake = Fake(answer(words) if callable(answer) else answer)
        monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
        body = asyncio.run(D.direct(D.DirectorCtx(
            words=words, script=lines.script, work_dir=icons, topic=lines.topic, others=[],
            frame0=track.frame0, spots=list(track.spots), catalog=catalog)))
        assert body["source"] == "ai" and len(fake.calls) in (1, 2)       # 2: a verdict was missing

        async def refuse(*_a, **_k):
            raise AssertionError("a paid model call in a test")

        monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse)
    return DR.LocalScene(int(run), "scene_0", work), lines, words, catalog


def snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns,
                                               hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# director.json keeps the call, and no key moves
# ---------------------------------------------------------------------------

def test_the_answer_is_kept_as_it_came_with_what_was_asked(tmp_path, monkeypatch):
    words = fixture_words("815")
    answer = the_815_answer(words, n_spots=0)
    fake = Fake(answer)
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    ctx = D.DirectorCtx(words=words, script=line_of("815"), work_dir=tmp_path / "icons", topic="t",
                        others=["o"], catalog=CAT())
    body = asyncio.run(D.direct(ctx))
    assert body["version"] == D.DIRECTOR_VERSION == 1
    assert body["raw"] == answer and "raw_bytes" not in body and "reasked" not in body
    assert body["prompt_digest"] == D.prompt_digest(fake.calls[0]["prompt"])
    assert body["inputs"] == D.key_inputs(ctx, ctx.catalog) and list(body["inputs"]) == \
        ["version", "words", "script", "topic", "others", "catalog", "spots", "take", "model"]
    assert body["key"] == D.director_key(ctx, ctx.catalog) == D._key_of(body["inputs"])
    on_disk = json.loads((tmp_path / "icons" / "director.json").read_text(encoding="utf-8"))
    assert on_disk == body
    # The plan's key reads the director's key, source, ideas and room only.
    from app.services.rebrand_harness.elements import icon as icon_el

    bare = {k: v for k, v in body.items() if k not in ("raw", "prompt_digest", "inputs", "raw_digest")}
    assert icon_el._director_digest(body) == icon_el._director_digest(bare)


def test_a_second_ask_is_recorded_with_its_own_prompt(tmp_path, monkeypatch):
    words = fixture_words("815")
    full = judge_all(words, {"code": {"priority": 5}})
    fake = Fake(judge_all(words, {"code": {"priority": 5}}, skip=("k2", "k4")), full)
    monkeypatch.setattr(vertex_client, "gemini_multimodal", fake)
    body = asyncio.run(D.direct(D.DirectorCtx(words=words, script=line_of("815"), work_dir=tmp_path,
                                              catalog=CAT())))
    assert len(fake.calls) == 2 and body["reasked"] == ["k2", "k4"] and body["raw"] == full
    assert body["prompt_digest"] == D.prompt_digest(fake.calls[1]["prompt"]) != \
        D.prompt_digest(fake.calls[0]["prompt"])


def test_an_answer_too_big_is_not_kept_but_still_used(tmp_path, monkeypatch):
    words = fixture_words("815")
    monkeypatch.setattr(D, "RAW_MAX", 100)
    monkeypatch.setattr(vertex_client, "gemini_multimodal", Fake(the_815_answer(words, n_spots=0)))
    body = asyncio.run(D.direct(D.DirectorCtx(words=words, script=line_of("815"), work_dir=tmp_path,
                                              catalog=CAT())))
    assert "raw" not in body and body["raw_bytes"] > 100 and body["source"] == "ai"
    assert asyncio.run(D.replay(body, words, CAT())) is None


def test_an_answer_that_breaks_validation_keeps_its_raw_for_a_free_retry(tmp_path, monkeypatch):
    words = fixture_words("815")
    answer = judge_all(words, {"code": {"priority": 5}})
    monkeypatch.setattr(vertex_client, "gemini_multimodal", Fake(answer))
    real = D.validate

    def boom(*_a, **_k):
        raise KeyError("w")

    monkeypatch.setattr(D, "validate", boom)
    body = asyncio.run(D.direct(D.DirectorCtx(words=words, script=line_of("815"), work_dir=tmp_path,
                                              catalog=CAT())))
    assert body["failed_at"] and body["raw"] == answer
    monkeypatch.setattr(D, "validate", real)
    ideas, _room = asyncio.run(D.replay(body, words, CAT()))
    assert [i["icon"] for i in ideas if i["auto"]] == ["code"]


def _old_key(ctx: D.DirectorCtx, catalog: D.Catalog) -> str:
    """director_key exactly as it was written before I4 (0f06454)."""
    body = [D.DIRECTOR_VERSION, ctx.words.get("digest"), D._sha(ctx.script), (ctx.topic or "").strip(),
            D._sha(json.dumps(list(ctx.others or []))), catalog.version,
            D.spots_digest(ctx.frame0, ctx.spots), int(ctx.take or 0), config.vertex_gemini_fast_model]
    return hashlib.sha256(json.dumps(body, default=str).encode("utf-8")).hexdigest()[:20]


def test_the_key_is_made_exactly_as_before(tmp_path):
    words = fixture_words("815")
    icons = tmp_path / "icons"
    track = save_track(icons)
    for kw in ({}, {"topic": " t \n", "others": ["a", "b"], "take": 2},
               {"frame0": track.frame0, "spots": list(track.spots)}):
        ctx = D.DirectorCtx(words=words, script=line_of("815"), work_dir=icons, **kw)
        for cat in (CAT(), D.default_catalog()):
            assert D.director_key(ctx, cat) == _old_key(ctx, cat)


@pytest.mark.skipif(not (S815 / "icons" / "director.json").is_file() or not (S815 / "icons" / "track.json").is_file(),
                    reason="run 815's studio folder is not on this machine")
def test_815s_live_answer_still_answers_its_key(monkeypatch):
    """The one paid answer: the scene's own words, room and line give the
    key it was stored under — keeping `raw` from now on moved nothing, and
    the icon library's catalogue version is the fill file's (C9)."""
    pin_model(monkeypatch)
    before = snapshot(S815 / "icons")
    words = json.loads((S815 / "icons" / "words.json").read_text(encoding="utf-8"))
    track = T.load(S815 / "icons")
    ctx = D.DirectorCtx(words=words, script=line_of("815"), work_dir=S815 / "icons", topic=TOPIC_815,
                        others=[], frame0=track.frame0, spots=list(track.spots), take=0)
    assert D.director_key(ctx, D.default_catalog()) == KEY_815
    body = D.cached(S815 / "icons", KEY_815)
    assert body is not None and body["key"] == KEY_815 and body["source"] == "ai" and "raw" not in body
    assert snapshot(S815 / "icons") == before


# ---------------------------------------------------------------------------
# replay: a stored answer judged again, for free
# ---------------------------------------------------------------------------

def _answers(words: dict) -> dict:
    code = wi(words, "coding")
    return {
        "the_815_picks": the_815_answer(words),
        "a_slip_a_silence_and_an_unsaid_quote": judge_all(
            words, {"code": {"priority": 5, "w": code + 2},                     # an index slip, corrected
                    "puzzle-piece": {"priority": 4, "quote": "real toys"}},     # not what was said
            skip=("k3",), room=room_of(10, home=3)),                            # home on an avoided spot
        "extras_negation_and_the_same_icon": judge_all(
            words, {"piggy-bank": {"priority": 5}, "code": {"priority": 5}},
            extra=[idea("new", "teachers", wi(words, "teachers"), "chalkboard-teacher", "keep", 5),
                   idea("new", "coding", code, "code", "keep", 4, concept="coding again"),   # same icon
                   idea("new", "school", wi(words, "school"), "other:cash", "keep", 3),
                   idea("new", "building", wi(words, "building"), "hammer", "keep", 2),
                   idea("new", "real", wi(words, "real"), "graduation-cap", "keep", 2)],    # a third extra
            room=room_of(10)),
    }


@pytest.mark.parametrize("name", ["the_815_picks", "a_slip_a_silence_and_an_unsaid_quote",
                                  "extras_negation_and_the_same_icon"])
def test_replaying_the_raw_answer_reproduces_the_stored_ideas(name, tmp_path, monkeypatch):
    def resolve(n: str):
        return {"school": ("chalkboard-teacher", 0.77), "cash": ("money", 0.99),
                "hammer": ("hammer", 0.95)}.get(n)

    scene, _lines, words, cat = scene_with(tmp_path, monkeypatch, answer=lambda w: _answers(w)[name],
                                           catalog=CAT(resolve))
    body = json.loads((scene.icons / "director.json").read_text(encoding="utf-8"))
    spots = list(T.load(scene.icons).spots)
    ideas, room = asyncio.run(D.replay(body, words, cat, spots=spots))
    assert DR._plain(ideas) == body["ideas"] and DR._plain(room) == body["room"]
    # An answer rebuilt from the ideas alone (a director.json kept before
    # `raw` was) comes back the same too.
    ideas2, room2 = asyncio.run(D.replay(body, words, cat, spots=spots, answer=DR.rebuilt_answer(body)))
    assert DR._plain(ideas2) == body["ideas"] and DR._plain(room2) == body["room"]


def test_a_changed_rule_is_judged_again_without_a_call(tmp_path, monkeypatch):
    """The library learns "school" (I6's resolver): the stored answer, judged
    again, now keeps the AI's school idea — no model call."""
    scene, _lines, words, _cat = scene_with(tmp_path, monkeypatch, answer=the_815_answer, catalog=CAT(NO_SCHOOL))
    body = json.loads((scene.icons / "director.json").read_text(encoding="utf-8"))
    school = next(i for i in body["ideas"] if i["concept"] == "school")
    assert school["verdict"] == "refuse" and school["reason"] == "no icon for 'school'"
    assert D.refused_by(school, body["raw"]) == D.refused_by(school) == "check"
    better = CAT(lambda n: ("graduation-cap", 0.93) if n == "school" else None)
    ideas, _room = asyncio.run(D.replay(body, words, better, spots=list(T.load(scene.icons).spots)))
    now = next(i for i in ideas if i["concept"] == "school")
    assert now["verdict"] == "keep" and now["icon"] == "graduation-cap" and not now["reason"]
    changes = DR.diff_ideas(body["ideas"], ideas)
    assert [c["idea"] for c in changes] == ["School"]
    assert changes[0]["changed"]["verdict"] == ["refuse", "keep"]


def test_refused_by_tells_the_ai_from_our_rules(tmp_path, monkeypatch):
    words = fixture_words("815")
    answer = judge_all(words, {"piggy-bank": {"priority": 5}}, skip=("k1",),
                       extra=[idea("new", "teachers", wi(words, "teachers"), "chalkboard-teacher", "keep", 5)])
    monkeypatch.setattr(vertex_client, "gemini_multimodal", Fake(answer))
    body = asyncio.run(D.direct(D.DirectorCtx(words=words, script=line_of("815"), work_dir=tmp_path,
                                              catalog=CAT())))
    by = {(i["cand_id"] or i["concept"]): i for i in body["ideas"]}
    for raw in (None, body["raw"]):
        assert D.refused_by(by["k2"], raw) is None                         # Free, kept
        assert D.refused_by(by["k3"], raw) == "ai"                         # refused: "filler"
        assert D.refused_by(by["chalkboard-teacher"], raw) == "check"      # "without teachers"
    # k1 had no verdict on either ask: refused, and the AI said nothing.
    assert D.refused_by(by["k1"]) == "silent" and by["k1"]["reason"] == D.WHY_SILENT
    assert by["chalkboard-teacher"]["reason"] == D.WHY_NEGATED
    # Every rule's reason reads as a rule's; the AI's own words do not.
    for why in (D.WHY_NOT_THERE, D.WHY_NO_ICON_FOR.format(name="x y"), D.WHY_NO_ICON.format(name="z"),
                D.WHY_LOGO, D.WHY_NOT_SAID, D.WHY_BRAND, D.WHY_NEGATED, D.WHY_IDIOM, D.WHY_LIST,
                D.WHY_SAME_ICON.format(label="Free"), D.WHY_SAME_WORD.format(label="Free"), D.WHY_EXTRAS):
        assert D.refused_by({"verdict": "refuse", "reason": why}) == "check", why
    assert D.refused_by({"verdict": "refuse", "reason": "Too generic, a filler word"}) == "ai"


# ---------------------------------------------------------------------------
# explain: every keyword the transcript matched, and why
# ---------------------------------------------------------------------------

def test_explain_marks_815s_teachers_as_negated():
    words = fixture_words("815")
    got = D.explain(words)
    teachers = next(e for e in got if e["said"].lower() == "teachers")
    assert teachers["dropped"] == "negated" and teachers["w"] == wi(words, "teachers")
    assert words["transcript"][teachers["pos"]:teachers["end"]] == teachers["said"]
    free = [e for e in got if e["icon"] == "piggy-bank"]
    assert [e["dropped"] for e in free] == [None, "overlap"] and free[0]["said"].lower() == "tuition-free"
    # What is kept is exactly the keyword table's own (find_mentions), which
    # the candidates are made of.
    kept = [(e["icon"], e["pos"]) for e in got if e["dropped"] is None]
    assert kept == [(m.icon, m.pos) for m in sorted(bi.find_mentions(words["transcript"]),
                                                    key=lambda m: (m.pos, m.tier, m.icon))]
    assert {c["icon"] for c in D.candidates(words)} == {i for i, _p in kept}


@pytest.mark.parametrize("line,word,why", [
    ("Feel free to ask anything about coding.", "free", "idiom"),
    ("42 Abu Dhabi welcomes you.", "Abu", None),
    ("You don't need a degree or any coding experience.", "coding", "negated"),
])
def test_explain_says_why_a_keyword_is_dropped(line, word, why):
    words = W.build_words(None, line, None, 6.0)
    got = [e for e in D.explain(words) if e["said"].lower().startswith(word.lower())]
    if why is None:
        assert all(e["dropped"] in ("brand", None) for e in got)
    else:
        assert got and got[0]["dropped"] == why and D.DROPPED_WHY[why]


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------

def test_blocked_refuses_and_notes_every_paid_call():
    before = {n: getattr(vertex_client, n) for n in DR.PAID_CALLS if hasattr(vertex_client, n)}
    assert set(before) >= {"gemini_multimodal", "gemini_image", "gemini_text"}
    with DR.blocked():
        for n in before:
            with pytest.raises(DR.PaidCallBlocked):
                asyncio.run(getattr(vertex_client, n)("x"))
    assert DR.ATTEMPTS == list(before)
    assert all(getattr(vertex_client, n) is fn for n, fn in before.items())


@pytest.mark.parametrize("run", ["815", "806"])
def test_the_dry_run_of_a_scene_calls_no_model_and_writes_nothing_there(run, tmp_path, monkeypatch):
    asked = run == "815"
    scene, lines, words, cat = scene_with(tmp_path, monkeypatch, run,
                                          answer=the_815_answer if asked else None)
    calls = []

    async def count(*_a, **_k):
        calls.append(1)
        raise AssertionError("a paid model call in the dry run")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", count)
    before = snapshot(scene.work)
    rep = asyncio.run(DR.scene_report(scene, lines, cache=tmp_path / "out", catalog=cat))
    assert calls == [] and DR.ATTEMPTS == []
    assert snapshot(scene.work) == before, "the studio's folder is only read"
    assert rep["state"] == "ok" and rep["words_from"] == "the studio's" and rep["track_from"] == "the studio's"
    assert rep["said"]["transcript"] == words["transcript"] and rep["explain"] == D.explain(words)
    assert rep["asked"]["image"] and 0 < rep["asked"]["cost"]["usd"] < 0.02
    if asked:
        assert rep["key"]["state"] == "reused" and rep["key"]["changed"] == []
        assert rep["asked"]["prompt_digest"] == rep["asked"]["stored_prompt_digest"]
        assert rep["replay"] == {"from": "raw", "same": True, "changes": [], "room_same": True}
        rows = {r["label"]: r for r in rep["answer"]["ideas"]}
        assert rows["School"]["by"] == "check" and rows["School"]["library"]["best"] == "chalkboard-teacher"
        assert rep["answer"]["raw_kept"]
        shown = [it["icon"] for it in rep["shown"]["items"]]
        assert shown and set(shown) <= {"piggy-bank", "code", "users-three", "puzzle-piece"}
        assert len(shown) <= rep["shown"]["auto_max"]
        assert rep["pictures"]["room"] and Path(rep["pictures"]["room"]).is_file()
    else:
        assert rep["key"]["state"] == "not_asked"
        assert "answer" not in rep and rep["replay"]["from"] is None
        assert rep["shown"]["items"] == [], "without the AI nothing is placed on its own"
        assert [f["icon"] for f in rep["fallback"]] == [c["icon"] for c in D.candidates(words)]
    json.dumps(rep)                                                          # the report is plain data
    page = DR.render_html([rep], generated="now", attempts=[])
    assert "No model was called" in page and f"Run {run} · scene_0" in page


def test_the_key_says_what_changed(tmp_path, monkeypatch):
    scene, lines, _words, cat = scene_with(tmp_path, monkeypatch, answer=the_815_answer)
    edited = DR.Lines(script=lines.script, topic="A new topic", others=[], source="given")
    rep = asyncio.run(DR.scene_report(scene, edited, cache=tmp_path / "out", catalog=cat))
    assert rep["key"]["state"] == "ask_again" and rep["key"]["changed"] == ["topic"]
    assert "topic changed" in rep["key"]["why"]
    unknown = asyncio.run(DR.scene_report(scene, DR.Lines(), cache=tmp_path / "out", catalog=cat))
    assert unknown["key"]["state"] == "unchecked"


def test_want_scores_what_showed(tmp_path, monkeypatch):
    scene, lines, _words, cat = scene_with(tmp_path, monkeypatch, answer=the_815_answer)
    rep = asyncio.run(DR.scene_report(scene, lines, cache=tmp_path / "out", catalog=cat,
                                      want=["piggy-bank", "Rocket Launch", "no such thing at all"]))
    wt = rep["want"]
    assert wt["wanted"] == ["piggy-bank", "rocket-launch"] and wt["unknown"] == ["no such thing at all"]
    assert wt["shown"]["hit"] == ["piggy-bank"] and wt["shown"]["recall"] == 0.5


def test_a_paid_call_is_caught_even_when_swallowed_and_main_exits_non_zero(tmp_path, monkeypatch):
    scene, _lines, _words, _cat = scene_with(tmp_path, monkeypatch)

    async def sneaky(sc, lines, **_kw):
        try:
            await vertex_client.gemini_multimodal("hello")
        except Exception:  # noqa: BLE001 — the code that tried swallowed it
            pass
        return {"scene": sc.name, "run_id": sc.run_id, "target": sc.target, "folder": str(sc.work),
                "state": "no_words", "words_from": "none"}

    monkeypatch.setattr(DR, "_scene_report", sneaky)
    out = tmp_path / "out"
    code = DR.main(["--root", str(tmp_path / "studio"), "--offline", "--out", str(out)])
    assert code == DR.EXIT_PAID
    body = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert body["paid_calls_attempted"] == ["gemini_multimodal"]
    assert "attempted and blocked" in (out / "report.html").read_text(encoding="utf-8")


def test_main_writes_the_report_for_every_local_scene(tmp_path, monkeypatch):
    s815, _l, _w, cat = scene_with(tmp_path, monkeypatch, "815", answer=the_815_answer)
    scene_with(tmp_path, monkeypatch, "806")
    monkeypatch.setattr(D, "default_catalog", lambda: cat)
    out = tmp_path / "out"
    page = tmp_path / "elsewhere" / "report.html"
    code = DR.main(["--root", str(tmp_path / "studio"), "--offline", "--no-listen", "--out", str(out),
                    "--html", str(page)])
    assert code == 0 and DR.ATTEMPTS == []
    body = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert [s["scene"] for s in body["scenes"]] == ["806/scene_0", "815/scene_0"]
    assert all(s["key"]["state"] == "unchecked" for s in body["scenes"])      # --offline: no line
    html_ = page.read_text(encoding="utf-8")
    assert "No model was called" in html_ and "Run 815 · scene_0" in html_ and "Run 806 · scene_0" in html_
    # One scene's line, given.
    code = DR.main(["--root", str(tmp_path / "studio"), "--run", "815", "--target", "scene_0", "--out", str(out),
                    "--line", line_of("815"), "--topic", "Why 42"])
    body = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert code == 0 and [s["key"]["state"] for s in body["scenes"]] == ["reused"]
    assert s815.work.is_dir()
    with pytest.raises(SystemExit):
        DR.main(["--line", "only one scene has a line"])


def test_the_page_escapes_what_it_quotes(tmp_path, monkeypatch):
    scene, lines, _words, cat = scene_with(tmp_path, monkeypatch, answer=the_815_answer)
    rep = asyncio.run(DR.scene_report(scene, lines, cache=tmp_path / "out", catalog=cat))
    rep["lines"]["script"] = "<script>alert(1)</script>"
    rep["said"]["transcript"] = rep["said"]["transcript"].replace("42", "<b>42</b>", 1)
    page = DR.render_html([rep], generated="now", attempts=[])
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)" in page and "<b>42</b>" not in page


def test_local_scenes_takes_the_folder_the_studio_uses(tmp_path):
    root = tmp_path / "studio"
    old = root / "815" / "scene_0_84f81b9967"
    new = root / "815" / "scene_0_84f81b9967_t188"
    (old / "icons").mkdir(parents=True)
    (old / "icons" / "director.json").write_text("{}", encoding="utf-8")
    new.mkdir(parents=True)
    (root / "815" / "not_a_scene").mkdir()
    (root / "_cache").mkdir()
    (root / "806" / "scene_1_abcdef0123").mkdir(parents=True)
    assert [(s.name, s.work.name) for s in DR.local_scenes(root)] == \
        [("806/scene_1", "scene_1_abcdef0123"), ("815/scene_0", "scene_0_84f81b9967")]
    (new / "icons").mkdir()
    (new / "icons" / "director.json").write_text("{}", encoding="utf-8")
    (new / "icons" / "words.json").write_text("{}", encoding="utf-8")
    assert [s.work.name for s in DR.local_scenes(root, run_id=815)] == ["scene_0_84f81b9967_t188"]
    assert DR.local_scenes(root, run_id=815, target="scene_1") == []


def test_lines_of_a_run_are_the_studios():
    one = {"id": 815, "topic": TOPIC_815, "metadata": {"script": {"dialogue": line_of("815")}}}
    got = DR.lines_of(one, "scene_0")
    assert (got.script, got.topic, got.others, got.source) == (line_of("815"), TOPIC_815, [], "database")
    many = {"id": 1, "topic": "T", "metadata": json.dumps({"scene_count": 3, "scenes": [
        {"dialogue": "First line.", "animation_id": 1},
        {"dialogue": "Second line.", "animation_id": 2},
        {"dialogue": "Third line.", "animation_id": 3, "excluded": True}]})}
    got = DR.lines_of(many, "scene_1")
    assert (got.script, got.others) == ("Second line.", ["First line."])


def test_the_dry_run_has_no_paid_call_in_it():
    src = Path(DR.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b(?:gemini_\w+|person_mask|veo_generate|omni\w*)\s*\(", src)


@pytest.mark.skipif(not (S815 / "icons" / "director.json").is_file() or not (S815 / "icons" / "track.json").is_file(),
                    reason="run 815's studio folder is not on this machine")
def test_the_815_report(tmp_path, monkeypatch):
    """What the owner reads for 815: Free and Code shown; Team over the
    limit; Projects too close to the end; "school" kept by the AI and refused
    for a library gap (0.77 < 0.80); "future" refused by the AI; the stored
    answer still answers its key; the answer, rebuilt from its ideas, comes
    back exactly — and the studio's folder is only read."""
    pin_model(monkeypatch)
    base = D.default_catalog()
    # The resolver as the live call had it (I6 will teach the library "school").
    cat = D.Catalog(version=base.version, picks=base.picks, exists=base.exists,
                    resolve=lambda n: ("chalkboard-teacher", 0.77) if n == "school" else None)
    before = snapshot(S815 / "icons")
    lines = DR.Lines(script=line_of("815"), topic=TOPIC_815, others=[], source="given")
    rep = asyncio.run(DR.scene_report(DR.LocalScene(815, "scene_0", S815), lines, cache=tmp_path, catalog=cat))
    assert snapshot(S815 / "icons") == before and DR.ATTEMPTS == []
    assert rep["key"]["state"] == "reused" and rep["key"]["today"] == KEY_815
    assert rep["said"]["line_matches"] is True
    assert [it["icon"] for it in rep["shown"]["items"]] == ["piggy-bank", "code"]
    assert [round(it["settle"], 1) for it in rep["shown"]["items"]] == [3.1, 4.9]
    ideas = {i["icon"]: i for i in rep["shown"]["ideas"]}
    assert ideas["users-three"]["reason"] == "Over the limit for this length (2)"
    assert ideas["puzzle-piece"]["reason"] == "Said too close to the end"
    rows = {r["concept"]: r for r in rep["answer"]["ideas"]}
    assert rows["school"]["by"] == "check" and rows["school"]["reason"] == "no icon for 'school'"
    assert rows["school"]["library"] == {"name": "school", "best": "chalkboard-teacher", "score": 0.77,
                                         "needs": D.MATCH_STRONG}
    assert rows["future"]["by"] == "ai" and rows["learning"]["by"] == "ai"
    assert rep["replay"]["from"] == "rebuilt" and rep["replay"]["same"] is True
    assert next(e for e in rep["explain"] if e["said"] == "teachers")["dropped"] == "negated"
    added = {a["icon"] for p in rep["studio_plans"] for a in p["added"]}
    assert "rocket-launch" in added, "the owner added the refused 'Your Future' back"
    assert any("Free" in s or "Tuition-Free" in s for s in rep["summary"])
