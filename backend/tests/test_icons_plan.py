"""Rebrand studio icons, the plan (elements/icon.py), its recipe contract
(recipe.IconProps …), the studio's lazy inputs (studio.icon_plan) and the icon
routes.

The words are the REAL 806/815 Whisper fixtures (tests/fixtures/icons_words)
replayed through words.build_words, or char-linear estimates of a made-up
line; the icon director's answer is scripted and run through the real
director.validate; the speaker track is synthetic. No model is ever called:
an autouse guard makes every Vertex call raise, the drawn-icon store is an
in-memory fake and every cache is under tmp_path. The 42 default is held
byte for byte by tests/test_rebrand_golden.py; here its recipe hash, options
and preview key are pinned against icons."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import rebrand_pipeline as pipeline
from app.services import vertex_client
from app.services.rebrand_harness import director as D
from app.services.rebrand_harness import icon_catalog as ic
from app.services.rebrand_harness import icon_render as ir
from app.services.rebrand_harness import prompt as hprompt
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import speaker_track as T
from app.services.rebrand_harness import studio
from app.services.rebrand_harness import words as W
from app.services.rebrand_harness.elements import icon as I

FIX = Path(__file__).parent / "fixtures" / "icons_words"
W_, H_ = 720, 1280


class FakeStore:
    """yas_rebrand_icons in memory."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def takes(self, name_key, origin):
        return sorted((dict(r) for r in self.rows.values()
                       if r["name_key"] == name_key and r.get("origin", "ai") == origin),
                      key=lambda r: r.get("take", 0))

    async def count_since(self, origin, hours):
        return sum(1 for r in self.rows.values() if r.get("origin", "ai") == origin)

    async def by_id(self, icon_id):
        r = self.rows.get(icon_id)
        return dict(r) if r else None

    async def insert(self, row):
        self.rows.setdefault(row["id"], {**row, "created_at": "t", "hidden_at": None})
        return dict(self.rows[row["id"]])

    async def set_hidden(self, icon_id, hidden):
        r = self.rows.get(icon_id)
        if not r or bool(r["hidden_at"]) == hidden:
            return False
        r["hidden_at"] = "now" if hidden else None
        return True

    async def listing(self, limit, include_hidden, origin=None):
        return [dict(r) for r in self.rows.values() if include_hidden or not r["hidden_at"]][:limit]


@pytest.fixture(autouse=True)
def guard(monkeypatch, tmp_path):
    """No paid call can run; drawn icons live in a fake store; caches and the
    studio's icon jobs start empty."""
    calls: list[str] = []

    def refuse(name):
        async def _refuse(*a, **k):
            calls.append(name)
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse("gemini_multimodal"))
    monkeypatch.setattr(vertex_client, "gemini_image", refuse("gemini_image"))
    monkeypatch.setattr(ic, "CACHE_DIR", tmp_path / "icons_cache")
    monkeypatch.delenv("ICON_DRAWS_PER_DAY", raising=False)
    ic._DRAW_FAILED.clear()
    ic._REJECTED.clear()
    store = FakeStore()
    old = ic.set_store(store)
    monkeypatch.setattr(studio, "_ICON_TASKS", {})
    yield {"calls": calls, "store": store}
    ic.set_store(old)
    assert not calls, f"unexpected paid calls: {calls}"


# ---------------------------------------------------------------------------
# Helpers: words, a scripted director, synthetic tracks
# ---------------------------------------------------------------------------

def words(rid: int) -> dict:
    fx = json.loads((FIX / f"{rid}.json").read_text(encoding="utf-8"))
    env = W.shape_envelope(fx["env"]["rms"], fx["env"]["thr"], fx["env"]["peak"], fx["env"]["voiced"])
    body = W.build_words(fx["chunks"], fx["dialogue"], None, fx["duration"], env=env)
    body["script"] = fx["dialogue"]
    return body


def line_words(line: str, seconds: float = 12.0) -> dict:
    body = W.build_words(None, line, None, seconds)
    body["script"] = line
    return body


def wi(w: dict, text: str, n: int = 0) -> int:
    return [x["w"] for x in w["words"] if x["said"] and x["text"].lower() == text.lower()][n]


def onset(w: dict, idx: int) -> float:
    return next(x["s"] for x in w["words"] if x["w"] == idx)


def judged(w: dict, keep: dict, *, extra=(), room=None) -> dict:
    """The director's body for a scripted answer: every keyword candidate
    refused as filler except `keep` {icon: overrides}, run through the real
    director.validate (negation, quotes, one per icon …)."""
    ideas = []
    for c in D.candidates(w):
        o = keep.get(c["icon"])
        base = {"cand_id": c["id"], "concept": c["icon"], "quote": c["quote"], "w": c["w"], "w_end": c["w_end"],
                "mentions": [], "icon": c["icon"], "label": c["icon"][:16], "purpose": "",
                "priority": 1, "verdict": "refuse", "refuse_reason": "filler", "list_span": False}
        if o is not None:
            base.update({"verdict": "keep", "refuse_reason": "", "purpose": "Why it helps", **o})
        ideas.append(base)
    ideas += list(extra)
    cands = D.candidates(w)
    got, room_ = D.validate({"ideas": ideas, "room": room or {"spots": [], "home": 0, "home_why": ""}},
                            w, cands, D.keyword_catalog(), spots=(room or {}).get("_spots") or [],
                            n_spots=len((room or {}).get("_spots") or []))
    return {"version": D.DIRECTOR_VERSION, "key": "k", "source": "ai", "ideas": got, "room": room_,
            "candidates": cands, "note": ""}


def idea(w: dict, icon: str, at: list[int], *, priority: int = 5, label: str = "", strong: bool = True) -> dict:
    """A director idea by hand: `at` = its word indices (the first is where
    it is the point; the rest its other mentions)."""
    by = {x["w"]: x for x in w["words"]}
    ments = [{"w": k, "w_end": k, "quote": by[k]["text"], "s": by[k]["s"]} for k in sorted(at)]
    return {"id": f"i{at[0]}", "cand_id": None, "source": "ai", "concept": icon, "quote": by[at[0]]["text"],
            "w": at[0], "w_end": at[0], "word": by[at[0]]["text"], "s": by[at[0]]["s"], "end": by[at[0]]["e"],
            "mentions": ments, "table_mentions": [], "icon": icon, "label": label or icon, "purpose": "Why",
            "priority": priority, "strong": strong, "verdict": "keep", "reason": "", "list_span": False,
            "auto": True}


def director_of(*ideas, room=None, source="ai", **extra) -> dict:
    return {"version": D.DIRECTOR_VERSION, "key": "k", "source": source, "ideas": list(ideas),
            "room": room or {"spots": [], "home": None, "home_n": None, "home_why": ""}, "candidates": [],
            "note": "", **extra}


def icon_el(**props) -> rc.Element:
    placement = props.pop("placement", None)
    animation = props.pop("animation", None)
    raw = {"id": "icons", "kind": "icon", "props": props}
    if placement:
        raw["placement"] = placement
    if animation:
        raw["animation"] = animation
    return rc.normalize({"elements": [raw]}).elements[0]


FREE_CODE = {"piggy-bank": {"priority": 5, "label": "Free"}, "code": {"priority": 4, "label": "Code"},
             "users-three": {"priority": 3, "label": "Team"},
             "puzzle-piece": {"priority": 4, "label": "Projects", "quote": "building real projects",
                              "w": 18, "w_end": 20}}


def ctx_for(w: dict, d: dict, **kw) -> I.IconCtx:
    kw.setdefault("enters", True)
    kw.setdefault("seed", "studio:1:scene_0")
    return I.IconCtx(words=w, director=d, **kw)


def shown(p: I.IconPlan) -> list[str]:
    return [it["label"] for it in p.items]


def item(p: I.IconPlan, label: str) -> dict:
    return next(it for it in p.public()["items"] if it["label"] == label)


def idea_of(p: I.IconPlan, label: str) -> dict:
    return next(i for i in p.ideas if i["label"] == label)


def _masks(n: int, *, hand_from: int = 99, head=None) -> np.ndarray:
    h8, w8 = H_ // 8, W_ // 8
    out = np.zeros((n, h8, w8), np.uint8)
    out[:, 70:, 25:65] = 255                                  # body: x 200-520, y 560-
    out[:, 25:70, 30:60] = 255                                # head: x 240-480, y 200-560
    for i in range(hand_from, n):
        out[i, 60:75, 65:88] = 255                            # a hand: x 520-704, y 480-600
    return out


def track(n: int = 17, *, hand_from: int = 99, heads=None, layout: str = "full", bars=None,
          picture=None, masks=None) -> T.Track:
    heads = heads if heads is not None else np.tile(np.array([[240, 200, 480, 560]], np.int32), (n, 1))
    pic = picture or (0, H_, 0, W_)
    return T.Track(width=W_, height=H_, fps=30.0, frames=n * 15, step=15,
                   times=np.arange(n, dtype=np.float32) * 0.5, heads=np.asarray(heads, np.int32),
                   masks=masks if masks is not None else _masks(n, hand_from=hand_from), bars=bars,
                   layout=layout, picture=pic,
                   spots=T.spot_grid(W_, H_, picture if layout == "letterbox" else None, layout), key="tk")


def px(box: dict) -> tuple[float, float, float, float]:
    return box["x"] * W_, box["y"] * H_, (box["x"] + box["w"]) * W_, (box["y"] + box["h"]) * H_


# ---------------------------------------------------------------------------
# The recipe
# ---------------------------------------------------------------------------

def test_the_icon_kind_is_ready_one_element_behind_and_stamped_exactly():
    rule = rc.KIND_RULES["icon"]
    assert (rule["z"], rule["exact"], rule["exact_allowed"], rule["max"], rule["ready"]) == \
        ("behind", True, True, 1, True)
    assert rule["anchors"] == ("auto", "left", "right", "top", "bottom", "custom")
    assert rule["entrances"] == ("auto", "none", "fade", "pop", "slide", "glitch")
    r = rc.normalize({"elements": [{"id": "icons", "kind": "icon"}]})
    rc.runnable(r)
    assert (r.elements[0].z, r.elements[0].exact) == ("behind", True)
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize({"elements": [{"id": "a", "kind": "icon"}, {"id": "b", "kind": "icon"}]})
    assert e.value.errors[0]["element_id"] == "b" and e.value.errors[0]["field"] == "kind"
    with pytest.raises(rc.RecipeError, match="cannot go 'edge-left'"):
        rc.normalize({"elements": [{"id": "i", "kind": "icon", "placement": {"anchor": "edge-left"}}]})
    with pytest.raises(rc.RecipeError, match="no 'left_to_right' entrance"):
        rc.normalize({"elements": [{"id": "i", "kind": "icon", "animation": {"entrance": "left_to_right"}}]})


def test_icon_props_are_checked_read_only_with_their_field_paths():
    bad = {"id": "icons", "kind": "icon", "z": "front", "placement": {"size": 0.3}, "props": {
        "colour": "violet", "junk": 1, "hold_sec": 4,
        "added": [{"glyph": {"source": "library", "id": "c-0123456789ab"}, "label": "x"}],
        "tweaks": {"code": {"sec": 2.0}, "piggy-bank": {"off_word": True}},
        "dismissed": ["BAD ID"]}}
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize({"elements": [bad]})
    fields = {x["field"] for x in e.value.errors}
    assert {"props.colour", "props.junk", "props.hold_sec", "props.added.0.glyph", "props.tweaks.code",
            "props.tweaks.piggy-bank", "props.dismissed", "z", "placement.size"} <= fields
    colour = next(x for x in e.value.errors if x["field"] == "props.colour")
    assert colour["message"] == "That colour isn't one we can use — pick another."

    four = [{"glyph": {"source": "library", "id": f"i{k}"}, "label": ""} for k in range(rc.ICON_ADDED_MAX + 1)]
    for props, field in (({"added": four}, "props.added"),
                         ({"added": four[:1] * 2}, "props"),
                         ({"added": four[:1], "dismissed": ["i0"]}, "props"),
                         ({"tweaks": {f"i{k}": {} for k in range(rc.ICON_TWEAKS_MAX + 1)}}, "props.tweaks"),
                         ({"lanes": {f"i{k}": 1 for k in range(rc.ICON_LANES_MAX + 1)}}, "props.lanes"),
                         ({"lanes": {"code": rc.ICON_LANE_MAX + 1}}, "props.lanes"),
                         ({"lanes": {"code": 1.5}}, "props.lanes"),
                         ({"lanes": {"BAD ID": 1}}, "props.lanes"),
                         ({"added": [{**four[0], "at": -1.0}]}, "props.added.0.at"),
                         ({"added": [{"glyph": {"source": "custom", "id": "piggy-bank"}}]}, "props.added.0.glyph")):
        with pytest.raises(rc.RecipeError) as e:
            rc.normalize({"elements": [{"id": "icons", "kind": "icon", "props": props}]})
        assert e.value.errors[0]["field"] == field, props

    # Valid props are never rewritten: unset defaults stay unset.
    ok = {"elements": [{"id": "icons", "kind": "icon", "props": {
        "added": [{"glyph": {"source": "custom", "id": "c-0123456789ab"}, "label": "Cyber cafe"}],
        "tweaks": {"code": {"off_word": True, "sec": 2.4}, "c-0123456789ab": {"w": 3, "words_digest": "abc"}},
        "dismissed": ["rocket-launch"]}, "placement": {"size": 0.12}}]}
    r = rc.normalize(ok)
    assert r.elements[0].props == ok["elements"][0]["props"]
    assert rc.icon_props(r.elements[0]).style == "badge" and "style" not in r.elements[0].props
    assert rc.canonical(rc.normalize(json.loads(rc.canonical(r)))) == rc.canonical(r)


def test_the_brief_may_open_a_recipe_with_icons_but_never_a_logo():
    rc.normalize({"elements": [{"id": "icons", "kind": "icon"}]})
    with pytest.raises(rc.RecipeError, match="harness opening"):
        rc.normalize({"elements": [{"id": "l", "kind": "logo"}]})


def test_icons_by_pipeline_only_for_icons_that_hide_omnis_own():
    d42 = rc.preset_42_default("scene_0")
    assert rc.recipe_hash(d42) == "a05a67c9c4c84f13" and rc.recipe_hash(rc.preset_42_default()) == "1ab8816f2e8a7feb"
    assert "icons_by_pipeline" not in rc.options_from_recipe(d42)

    def with_icons(**el) -> rc.Recipe:
        return rc.normalize({**d42.model_dump(mode="json"),
                             "elements": [*d42.model_dump(mode="json")["elements"],
                                          {"id": "icons", "kind": "icon", **el}]})

    on = with_icons()
    assert rc.options_from_recipe(on)["icons_by_pipeline"] is True and rc.icons_by_pipeline(on)
    assert {k: v for k, v in rc.options_from_recipe(on).items() if k != "icons_by_pipeline"} == \
        rc.options_from_recipe(d42)
    assert "icons_by_pipeline" not in rc.options_from_recipe(with_icons(enabled=False))
    assert "icons_by_pipeline" not in rc.options_from_recipe(with_icons(props={"omni_own": "keep"}))
    assert rc.icon_element(with_icons(enabled=False)) is None
    assert rc.icon_element(with_icons(enabled=False), enabled=False).id == "icons"


def test_start_keeps_icons_by_pipeline_past_normalize_options(monkeypatch):
    seen = {}

    async def run_row(run_id):
        return {"metadata": {}}

    async def check(recipe, chain=()):
        return []

    async def listing(*a, **k):
        return []

    async def create(run_id, options):
        seen["options"] = options
        return {"id": 7}

    async def get(slug):
        return object()

    async def run(*a, **k):
        return None

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(pipeline.pattern_el, "check", check)
    monkeypatch.setattr(pipeline.store, "list_for_run", listing)
    monkeypatch.setattr(pipeline.store, "create_experiment", create)
    monkeypatch.setattr(pipeline.pattern_library, "get", get)
    monkeypatch.setattr(pipeline.omni, "providers", lambda: ["vertex"])
    monkeypatch.setattr(pipeline, "_run", run)
    d42 = rc.preset_42_default("scene_0").model_dump(mode="json")
    r = rc.normalize({**d42, "elements": [*d42["elements"], {"id": "icons", "kind": "icon"}]})

    async def go(recipe):
        await pipeline.start(1, None, recipe=recipe)
        await asyncio.sleep(0)

    asyncio.run(go(r))
    assert seen["options"]["icons_by_pipeline"] is True
    asyncio.run(go(rc.preset_42_default("scene_0")))
    assert "icons_by_pipeline" not in seen["options"]


def test_a_frozen_plan_is_validated_and_stripped_from_what_a_client_sends():
    res = {"glyph": {"source": "library", "id": "code"}, "label": "Code", "from_sec": 1.0, "settle_sec": 1.2667,
           "to_sec": 2.4, "box": {"x": 0.04, "y": 0.3, "w": 0.16, "h": 0.09}, "fill": "#7f2bf2",
           "ink": "#ffffff", "fx": {"entrance": "glitch", "seed": "s:code"}}
    r = rc.normalize({"elements": [{"id": "icons", "kind": "icon", "props": {"resolved": [res]}}]})
    assert I.strip_resolved(r).elements[0].props == {}
    with pytest.raises(rc.RecipeError):
        rc.normalize({"elements": [{"id": "icons", "kind": "icon", "props": {
            "resolved": [{**res, "settle_sec": 3.0}]}}]})


# ---------------------------------------------------------------------------
# When: the scheduler on the real 815 / 806 words
# ---------------------------------------------------------------------------

def _on_point(p: I.IconPlan, w: dict) -> None:
    """Every shown icon settles within [onset - 0.20, onset + 0.30] of the
    word it landed on, two never overlap, all within the zones."""
    dur = w["duration"]
    items = p.public()["items"]
    for it in items:
        s = onset(w, it["w"])
        assert s - 0.20 - 1e-6 <= it["settle"] <= s + 0.30 + 1e-6, (it["label"], it["settle"], s)
        assert it["from"] >= 0.5 - 1e-6 and it["to"] <= dur - 0.30 + 1e-6
        assert it["settle"] >= 1.5 - 1e-6                     # the pattern comes in first
        assert 1.0 - 1e-6 <= it["hold"] <= 3.0 + 1e-6
    for a, b in zip(items, items[1:]):
        assert a["to"] <= b["from"]


def test_815_free_then_code_the_owners_example_with_the_key_gap():
    w = words(815)
    p = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE)))
    assert shown(p) == ["Free", "Code"]
    free, code = item(p, "Free"), item(p, "Code")
    # Free lands on "tuition" at its tolerance's early edge, holds the minimum
    # second; Code lands 0.19 s after "coding" — both on point, 0.30 s apart.
    assert (free["word"], free["from"], free["settle"], free["to"]) == ("tuition", 2.833, 3.1, 4.3)
    assert (code["word"], code["from"], code["settle"]) == ("coding", 4.6, 4.867)
    assert code["from"] - free["to"] == pytest.approx(0.30, abs=1e-3)
    _on_point(p, w)
    assert idea_of(p, "Team")["state"] == "limit"                # over the limit, not "no room"
    assert idea_of(p, "Team")["reason"] == "Over the limit for this length (2)"
    assert idea_of(p, "Projects")["reason"] == "Over the limit for this length (2)"
    # "Your Future": the AI refused it as filler; a hopeful word is offered as
    # a one-tap suggestion instead (the owner's call, I15) — never on its own.
    rocket = next(i for i in p.ideas if i["glyph"]["id"] == "rocket-launch")
    assert (rocket["state"], rocket["reason"], rocket["auto"]) == ("available", D.WHY_HOPEFUL, False)
    assert rocket["why"] == {"by": "hopeful", "text": "filler"}
    assert p.source == {"words": "whisper", "director": "ai"}


def test_815_a_lesser_code_has_no_room_after_free_until_the_user_adds_it():
    w = words(815)
    minor = {**FREE_CODE, "code": {"priority": 3, "label": "Code"}}
    d = judged(w, minor)
    p = I.plan(icon_el(), ctx_for(w, d))
    # 0.60 s of air after Free leaves Code no room; the closing "building
    # real projects" takes the second place (then the length is full).
    assert shown(p) == ["Free", "Projects"] and item(p, "Projects")["word"] == "building"
    assert idea_of(p, "Code")["reason"] == "Over the limit for this length (2)"
    _on_point(p, w)
    d = judged(w, {k: v for k, v in minor.items() if k != "puzzle-piece"})
    p = I.plan(icon_el(), ctx_for(w, d))
    assert shown(p) == ["Free"]
    assert idea_of(p, "Code")["reason"] == "No room right after 'Free'"
    assert idea_of(p, "Team")["reason"] == "No room right after 'Free'"
    # The user adds it: a user item is a key idea — 0.30 s of air — and
    # outranks every AI pick.
    add_code = icon_el(added=[{"glyph": {"source": "library", "id": "code"}, "label": "Code"}])
    added = I.plan(add_code, ctx_for(w, d))
    assert shown(added) == ["Free", "Code"]
    assert item(added, "Code")["source"] == "user" and item(added, "Free")["source"] == "ai"
    _on_point(added, w)
    added = I.plan(add_code, ctx_for(w, judged(w, minor)))
    assert shown(added) == ["Free", "Code"]
    assert idea_of(added, "Projects")["reason"] == "No room right after 'Code'"


def test_806_free_and_projects_land_on_their_words():
    w = words(806)
    d = judged(w, {"piggy-bank": {"priority": 5, "label": "Free"},
                   "puzzle-piece": {"priority": 4, "label": "Projects"}})
    p = I.plan(icon_el(), ctx_for(w, d))
    assert shown(p) == ["Free", "Projects"]
    free, proj = item(p, "Free"), item(p, "Projects")
    assert (free["word"], free["settle"]) == ("tuition", 4.0)          # 2 frames before 4.08
    assert (proj["word"], proj["settle"]) == ("projects", 6.4)         # 2 frames before 6.48
    assert proj["to"] <= w["duration"] - 0.30
    _on_point(p, w)
    assert I.auto_max(w["voiced"][1] - w["voiced"][0]) == 2


def test_a_negation_or_filler_never_shows_and_without_the_ai_nothing_is_automatic():
    w = words(815)
    teachers = {"cand_id": "new", "concept": "teachers", "quote": "teachers", "w": wi(w, "teachers"),
                "w_end": wi(w, "teachers"), "mentions": [], "icon": "chalkboard-teacher", "label": "Teachers",
                "purpose": "x", "priority": 5, "verdict": "keep", "refuse_reason": "", "list_span": False}
    d = judged(w, FREE_CODE, extra=[teachers])
    p = I.plan(icon_el(), ctx_for(w, d))
    assert "Teachers" not in shown(p)
    t = next(i for i in p.ideas if i["glyph"]["id"] == "chalkboard-teacher")
    assert t["state"] == "refused" and t["reason"] == "said as a negation"
    assert t["why"] == {"by": "check", "text": "said as a negation"}, "the AI kept it; our rule refused it"
    # A hopeful word the AI refused is a suggestion (I15), never shown on its own.
    assert next(i for i in p.ideas if i["glyph"]["id"] == "rocket-launch")["state"] == "available"

    # The AI down: keyword suggestions only — no automatic icon at all.
    cands = D.candidates(w)
    down = director_of(*D.fallback(w, cands, D.keyword_catalog(), "AI unavailable"), source="keywords",
                       failed_at=time.time())
    p = I.plan(icon_el(), ctx_for(w, down))
    assert p.items == [] and {i["state"] for i in p.ideas} == {"available"}
    assert I.W_AI_DOWN in p.warnings and p.source["director"] == "keywords"
    # Keywords only by choice: the same, with no "AI unavailable".
    off = I.plan(icon_el(ai=False), ctx_for(w, director_of(*D.fallback(w, cands, D.keyword_catalog(),
                                                                         "keywords only"), source="keywords")))
    assert off.items == [] and I.W_AI_DOWN not in off.warnings


def _cue(key: str, at: float, *, priority: int = 5, tier: int = 2, end: float | None = None) -> I.Cue:
    return I.Cue(key=key, glyph={"source": "library", "id": key}, label=key.capitalize(), priority=priority,
                 tier=tier, rank=(-priority, 0, int(at * 10)), anchors=[(0, int(at * 10), at, end or at + 0.3)])


def test_the_caps_follow_the_length_of_the_speech():
    assert [I.auto_max(v) for v in (1.0, 5.0, 7.9, 9.0, 12.0, 30.0)] == [1, 1, 2, 3, 3, 3]
    s = I.Scheduler(duration=16.0, enters=False, voiced=[0.0, 16.0])
    slots, drops = s.run([_cue(k, t) for k, t in (("a", 2.0), ("b", 5.5), ("c", 9.0), ("d", 12.5))])
    assert [x.cue.key for x in slots] == ["a", "b", "c"] and drops == {"d": "Over the limit for this length (3)"}
    s = I.Scheduler(duration=8.0, enters=False, voiced=[0.3, 7.6])
    slots, drops = s.run([_cue(k, t) for k, t in (("a", 1.5), ("b", 4.0), ("c", 6.0))])
    assert len(slots) == 2 and drops["c"] == "Over the limit for this length (2)"
    # The user's own adds are not held to the AI's share (only to three).
    user = [_cue(k, t, tier=1) for k, t in (("a", 1.5), ("b", 4.0))] + [_cue("c", 6.0)]
    slots, drops = I.Scheduler(duration=8.0, enters=False, voiced=[0.3, 7.6]).run(user)
    assert [x.cue.key for x in slots] == ["a", "b", "c"]
    # One that could not show even alone says so, not "over the limit" (Add
    # would not make it show either).
    slots, drops = I.Scheduler(duration=8.0, enters=False, voiced=[0.3, 7.6]).run(
        [_cue(k, t) for k, t in (("a", 1.5), ("b", 4.0), ("late", 7.6))])
    assert len(slots) == 2 and drops == {"late": "Said too close to the end"}


def test_the_zones_and_why_an_icon_is_left_out():
    s = I.Scheduler(duration=8.0, enters=False)
    slots, drops = s.run([_cue("early", 0.3), _cue("late", 7.6)])
    assert slots == [] and drops == {"early": "Said too close to the start", "late": "Said too close to the end"}
    s = I.Scheduler(duration=8.0, enters=True)
    slots, drops = s.run([_cue("hook", 1.0)])
    assert drops == {"hook": "Said while the pattern comes in"}
    slots, _ = I.Scheduler(duration=8.0, enters=True).run([_cue("x", 1.6)])
    assert slots[0].settle_f >= 45 and slots[0].to_f <= int((8.0 - 0.30) * 30)
    # Not tied to any word, and a spot the speaker takes.
    loose = I.Cue(key="loose", glyph={"source": "library", "id": "loose"}, label="Loose")
    blocked = I.Scheduler(duration=8.0, enters=False, placeable=lambda c, f0, f1: (False, 5.2))
    _, drops = blocked.run([loose, _cue("hand", 5.0)])
    assert drops == {"loose": I.NOT_TIED, "hand": "No clear spot — the speaker's hands are up at 5.2 s"}


def test_the_head_word_first_then_the_rest_of_the_phrase():
    # "building real projects" at the end of 815: "projects" (7.00) is too
    # late, but the phrase may land on "building" (6.61) when there is room.
    w = words(815)
    d = judged(w, {"puzzle-piece": {"priority": 5, "label": "Projects", "quote": "building real projects",
                                    "w": 18, "w_end": 20}})
    p = I.plan(icon_el(), ctx_for(w, d))
    assert shown(p) == ["Projects"] and item(p, "Projects")["word"] == "building"
    only = judged(w, {"puzzle-piece": {"priority": 5, "label": "Projects"}})
    p = I.plan(icon_el(), ctx_for(w, only))
    assert p.items == [] and idea_of(p, "Projects")["state"] == "near_end"
    assert idea_of(p, "Projects")["reason"] == "Said too close to the end"


# ---------------------------------------------------------------------------
# Where: the room
# ---------------------------------------------------------------------------

def _clear_of_the_head(p: I.IconPlan, tr: T.Track) -> None:
    """Every shown icon, at every track sample of its window (± 0.25 s),
    stays off the head box grown by 0.02 W, and the speaker covers at most
    3 % of it."""
    g = 0.02 * tr.width
    for it in p.public()["items"]:
        x0, y0, x1, y1 = px(it["box"])
        for i in tr.samples_in(it["from"] - 0.25, it["to"] + 0.25):
            hx0, hy0, hx1, hy1 = (float(v) for v in tr.heads[i])
            assert x1 <= hx0 - g or x0 >= hx1 + g or y1 <= hy0 - g or y0 >= hy1 + g, (it["label"], i)
        assert tr.overlap(it["box"], it["from"] - 0.25, it["to"] + 0.25) <= 0.03
        assert it["max_overlap"] <= 0.03


def test_an_icon_never_touches_the_grown_head_or_a_raised_hand():
    w = words(815)
    # The head drifts left from 3 s (the left column is then off-limits) and
    # a hand comes up on the right at 2.5 s.
    heads = np.array([[240, 200, 480, 560]] * 6 + [[60, 200, 300, 560]] * 11, np.int32)
    tr = track(hand_from=5, heads=heads)
    p = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE), track=tr))
    assert shown(p) == ["Free", "Code"]
    _clear_of_the_head(p, tr)
    blocked = {s["id"] for s in p.spots if not s["ok"]}
    assert {"L1", "L2", "L3", "R3"} <= blocked and "L4" not in blocked      # L4 is below the head
    assert p.home["spot"] not in blocked and p.home["by"] == "score"
    # Size L: bigger boxes, the same rules.
    big = I.plan(icon_el(placement={"size": 0.20}), ctx_for(w, judged(w, FREE_CODE), track=tr))
    assert all(abs(it["box"]["w"] * W_ - 144) <= 1 for it in big.public()["items"])
    _clear_of_the_head(big, tr)


def _letterbox() -> T.Track:
    n = 17
    bars = np.zeros((H_, W_), bool)
    bars[:400] = True
    bars[880:] = True
    masks = np.zeros((n, H_ // 8, W_ // 8), np.uint8)
    masks[:, 50:110, 25:65] = 255                              # the speaker, inside the picture
    masks[:, 0:10, 0:20] = 255                                 # MODNet's specks on the top bar
    heads = np.tile(np.array([[240, 420, 480, 700]], np.int32), (n, 1))
    return track(n, heads=heads, layout="letterbox", bars=bars, picture=(400, 880, 0, W_), masks=masks)


def test_a_letterboxed_scene_keeps_the_icons_in_the_bars():
    w = words(806)
    tr = _letterbox()
    d = judged(w, {"piggy-bank": {"priority": 5, "label": "Free"},
                   "puzzle-piece": {"priority": 4, "label": "Projects"}})
    p = I.plan(icon_el(), ctx_for(w, d, track=tr))
    assert shown(p) == ["Free", "Projects"]
    assert p.home["spot"].split("-")[0] in ("TB", "BB") and "never on the picture" in p.home["why"]
    for it in p.public()["items"]:
        _x0, y0, _x1, y1 = px(it["box"])
        assert y1 <= 400 + 1 or y0 >= 880 - 1                 # in a bar, never on the picture
    _clear_of_the_head(p, tr)
    assert p.anchors_available == ["auto", "left", "right", "top", "bottom"]
    # Asked for the picture's edge, it goes there.
    left = I.plan(icon_el(placement={"anchor": "left"}), ctx_for(w, d, track=tr))
    assert left.home["spot"] == "PL" and left.home["by"] == "you"


def test_the_where_chip_restricts_the_side_and_an_unavailable_one_is_ignored():
    w = words(815)
    d = judged(w, FREE_CODE)
    tr = track()
    right = I.plan(icon_el(placement={"anchor": "right"}), ctx_for(w, d, track=tr))
    assert right.home["spot"].startswith("R") and right.home["by"] == "you"
    assert right.home["why"].endswith("your pick")
    assert {it["spot"] for it in right.items} == {right.home["spot"]}
    # A full-bleed shot has no bottom spots: "bottom" places as auto does.
    bottom = I.plan(icon_el(placement={"anchor": "bottom"}), ctx_for(w, d, track=tr))
    auto = I.plan(icon_el(), ctx_for(w, d, track=tr))
    assert "bottom" not in bottom.anchors_available and bottom.home["spot"] == auto.home["spot"]


def test_the_ai_home_is_used_when_clear_and_an_avoid_is_a_veto():
    w = words(815)
    tr = track()
    spots = tr.spots
    n_of = {s["id"]: k for k, s in enumerate(spots, 1)}
    base = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE), track=tr))
    best = base.home["spot"]
    other = "L2" if best != "L2" else "R2"
    room = {"spots": [{"n": n_of[best], "verdict": "avoid", "why": "a sign"},
                      {"n": n_of[other], "verdict": "good", "why": "a calm wall"}],
            "home": n_of[other], "home_why": "a calm wall", "_spots": spots}
    p = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE, room=room), track=tr))
    assert p.home == {"spot": other, "why": p.home["why"], "by": "ai"} and "AI: a calm wall" in p.home["why"]
    vetoed = next(s for s in p.spots if s["id"] == best)
    assert vetoed["ai"] == {"verdict": "avoid", "why": "a sign"}
    room_no_home = {**room, "home": 0}
    p = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE, room=room_no_home), track=tr))
    assert p.home["spot"] != best and p.home["by"] == "score"


def test_the_ais_avoid_is_a_veto_on_the_spots_the_planner_picks():
    w = words(815)
    tr = track()
    spots = tr.spots
    n_of = {s["id"]: k for k, s in enumerate(spots, 1)}
    left = [s["id"] for s in spots if s["group"] == "left"]

    def room(avoid):
        return {"spots": [{"n": n_of[i], "verdict": "avoid", "why": "a sign"} for i in avoid],
                "home": 0, "home_why": "", "_spots": spots}

    # Every clear spot but the left column is avoided: the icons go left,
    # never on an avoided spot, whatever it scores.
    rest = [s["id"] for s in spots if s["id"] not in left]
    p = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE, room=room(rest)), track=tr))
    assert shown(p) == ["Free", "Code"] and {it["spot"] for it in p.items} <= set(left)
    # All of them: none is placed, and it says why.
    p = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE, room=room([s["id"] for s in spots])), track=tr))
    assert shown(p) == [] and idea_of(p, "Free")["reason"] == I.NO_SPOT_AI
    # A side the user chose keeps its avoided spots (scored down): their call.
    p = I.plan(icon_el(placement={"anchor": "right"}),
               ctx_for(w, judged(w, FREE_CODE, room=room([s["id"] for s in spots])), track=tr))
    assert shown(p) == ["Free", "Code"] and all(it["spot"].startswith("R") for it in p.items)


FACE = {"x": 0.35, "y": 0.2, "w": 0.16, "h": 0.09}          # on the head


@pytest.mark.parametrize("face", ["shift", "reject", "warn"])
def test_a_box_dragged_onto_the_face_is_kept_there_whatever_the_face_guard(face):
    # The face guard only decides the places the planner picks: a box the
    # user put an icon in is honoured exactly (behind the speaker), said, and the
    # nearest clear spot is offered ("Move to a clear spot").
    w = words(815)
    el = icon_el(tweaks={"piggy-bank": {"box": FACE}})
    p = I.plan(el, ctx_for(w, judged(w, FREE_CODE), track=track(), face=face))
    free = item(p, "Free")
    assert free["box"] == FACE and free["spot"] is None and free["layer"] == "behind"
    assert free["covered"] == "face" and free["max_overlap"] > 0.03
    assert any(x.startswith("'Free' is behind the speaker's head at") for x in p.warnings)
    assert free["clear"] is not None and track().feasible(free["clear"], free["from"] - 0.25, free["to"] + 0.25)


def test_without_a_track_the_spots_are_a_guess_but_stay_in_the_frame():
    w = words(815)
    for size in (None, 0.12, 0.20):
        p = I.plan(icon_el(placement={"size": size} if size else {}),
                   ctx_for(w, judged(w, FREE_CODE), notes=[I.W_NO_TRACK]))
        assert shown(p) == ["Free", "Code"] and I.W_NO_TRACK in p.warnings
        for s in p.spots:
            b = s["box"]
            assert 0 <= b["x"] and b["x"] + b["w"] <= 1 + 1e-4 and 0 <= b["y"] and b["y"] + b["h"] <= 1 + 1e-4
            if size:
                assert abs(b["w"] * W_ - size * W_) <= 1


def test_a_clear_dragged_box_is_kept_where_it_was_put():
    w = words(815)
    spot = {"x": 0.05, "y": 0.62, "w": 0.16, "h": 0.09}         # low on the left: clear
    p = I.plan(icon_el(tweaks={"code": {"box": spot}}), ctx_for(w, judged(w, FREE_CODE), track=track()))
    assert item(p, "Code")["box"] == spot and item(p, "Code")["spot"] is None
    assert item(p, "Free")["spot"] == p.home["spot"]


def backgrounds_of(tr: T.Track, rgb) -> I.Backgrounds:
    thumbs = np.zeros((len(tr.times), H_ // 8, W_ // 8, 3), np.uint8)
    thumbs[:] = rgb
    return I.Backgrounds(key="bg", times=np.asarray(tr.times), thumbs=thumbs)


def test_colours_follow_the_brand_the_scene_and_the_users_pick():
    w = words(815)
    tr = track()
    wall = backgrounds_of(tr, (235, 232, 228))
    d = judged(w, FREE_CODE)
    p = I.plan(icon_el(), ctx_for(w, d, track=tr, backgrounds=wall))
    assert p.colours["fill"] == "#7f2bf2" and p.colours["ink"] == "#ffffff"
    # On a violet pattern strip: not the pattern's own colour.
    strip = I.IconPattern(key="p1", colour="#7f2bf2", box={"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
                          ink=np.ones((H_ // 8, W_ // 8), np.float32))
    p = I.plan(icon_el(), ctx_for(w, d, track=tr, backgrounds=wall, pattern=strip))
    assert p.colours["fill"] != "#7f2bf2" and "pattern" in p.colours["why"]
    mine = I.plan(icon_el(colour="#303030", style="glyph"), ctx_for(w, d, track=tr, backgrounds=wall))
    assert mine.colours["fill"] == "#303030" and mine.colours["why"] == "Your colour"
    assert {r["style"] for r in mine.resolved()} == {"glyph"}


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------

def test_the_plan_key_hashes_the_validated_props_and_splits_timing_from_place():
    w = words(815)
    d = judged(w, FREE_CODE)
    c = ctx_for(w, d, track=track())
    a = I.plan(icon_el(), c)
    explicit = I.plan(icon_el(ai=True, style="badge", colour="auto", omni_own="hide", added=[], dismissed=[],
                              tweaks={}, hold_sec=None), c)
    assert explicit.key() == a.key() and explicit.timing_key == a.timing_key
    assert I.plan(icon_el(), ctx_for(w, copy.deepcopy(d), track=track())).key() == a.key()
    frozen = icon_el(resolved=a.resolved())
    assert I.plan(frozen, c).key() == a.key(), "a frozen plan never moves the key"
    recolour = I.plan(icon_el(colour="#303030"), c)
    assert recolour.timing_key == a.timing_key and recolour.key() != a.key()
    pat = [I.plan(icon_el(), ctx_for(w, d, track=track(), pattern=I.IconPattern(key=k, provisional=pv)))
           for k, pv in (("p1", True), ("p1", False), ("p2", False))]
    assert len({x.timing_key for x in pat} | {a.timing_key}) == 1
    assert len({x.key() for x in pat}) == 3 and pat[0].provisional and not pat[1].provisional
    assert I.plan(icon_el(dismissed=["code"]), c).timing_key != a.timing_key


# ---------------------------------------------------------------------------
# The look: Circle, Circle + words, each icon its own (I5)
# ---------------------------------------------------------------------------

# Run 815's head box: x 0.27-0.72 W (icon_look.md §1.4) — an M chip clears
# it only in the top corners.
HEAD_815 = [int(0.27 * W_), 200, int(0.72 * W_), 560]
OLD_ITEM_KEYS = {"glyph", "label", "from_sec", "settle_sec", "to_sec", "box", "fill", "ink", "fx", "style",
                 "word", "purpose", "max_overlap"}


def track_815(**kw) -> T.Track:
    n = kw.pop("n", 17)
    return track(n, heads=np.tile(np.array([HEAD_815], np.int32), (n, 1)), **kw)


def test_an_old_recipe_plans_as_the_badge_with_the_keys_it_always_had():
    """No style (every recipe before the circle): the badge, fill glyphs, the
    very items and keys made before this change (pinned from the planner as
    it was)."""
    w = words(815)
    p = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE), track=track()))
    assert (p.timing_key, p.key()) == ("17616569746b1ec0", "3674cbfd1e37781f")
    for r in p.resolved():
        assert set(r) == OLD_ITEM_KEYS and r["style"] == "badge" and set(r["fx"]) == {"entrance", "seed"}
    assert p.resolved()[0] == {
        "box": {"h": 0.09, "w": 0.16, "x": 0.04, "y": 0.295}, "fill": "#7f2bf2", "from_sec": 2.8333,
        "fx": {"entrance": "glitch", "seed": "studio:1:scene_0:piggy-bank"},
        "glyph": {"id": "piggy-bank", "source": "library"}, "ink": "#ffffff", "label": "Free",
        "max_overlap": 0.0, "purpose": "Why it helps", "settle_sec": 3.1, "style": "badge", "to_sec": 4.3,
        "word": "tuition"}
    tr = track()
    glyph = I.plan(icon_el(colour="#303030", style="glyph"),
                   ctx_for(w, judged(w, FREE_CODE), track=tr, backgrounds=backgrounds_of(tr, (235, 232, 228))))
    assert (glyph.timing_key, glyph.key()) == ("17616569746b1ec0", "72738221830beb35")
    assert all(i["style"] == "glyph" and i["weight"] == "fill" and i["look_v"] == 0 for i in glyph.public()["items"])
    # An empty looks map is no look at all: the same keys.
    assert I.plan(icon_el(looks={}), ctx_for(w, judged(w, FREE_CODE), track=track())).key() == p.key()


def test_looks_and_the_new_styles_enter_the_timing_key():
    w = words(815)
    c = ctx_for(w, judged(w, FREE_CODE), track=track())
    base = I.plan(icon_el(style="circle"), c)
    assert base.timing_key != I.plan(icon_el(), c).timing_key, "the style is in the key once it is new"
    for looks in ({"code": {"colour": "#303030"}}, {"code": {"style": "chip"}}, {"code": {"size": 0.2}},
                  {"code": {"label": "Coding"}}, {"code": {"layer": "front"}}, {"code": {"enter": "pop"}}):
        got = I.plan(icon_el(style="circle", looks=looks), c)
        assert got.timing_key != base.timing_key and got.key() != base.key(), looks
    # A look waiting for an icon that isn't shown still keys (it returns with it).
    assert I.plan(icon_el(style="circle", looks={"exam": {"colour": "#303030"}}), c).timing_key != base.timing_key


def test_a_circle_takes_the_badges_place_in_bold_and_the_chip_its_words():
    w = words(815)
    c = ctx_for(w, judged(w, FREE_CODE), track=track())
    badge = I.plan(icon_el(), c)
    circle = I.plan(icon_el(style="circle"), c)
    assert [i["box"] for i in circle.items] == [i["box"] for i in badge.items], "the same square body"
    for r in circle.resolved():
        assert (r["style"], r["weight"], r["look_v"]) == ("circle", "bold", 1) and "text" not in r
    pub = circle.public()["items"][0]
    assert pub["d"] == ic.library_d("piggy-bank", "bold") and pub["asked"] == "circle" and pub["size"] == 0.16
    chip = I.plan(icon_el(style="chip", placement={"anchor": "top"}), c)
    font = I.label_key()
    for r, it in zip(chip.resolved(), chip.items):
        L = 0.16 * W_
        h = I.chip_height(L)
        g = ir.chip_geometry(r["label"], h, font, max_w=ir.CHIP_MAX_W * W_)
        assert (r["style"], r["text"], r["font"], r["look_v"], r["weight"]) == ("chip", g.text, font, 1, "bold")
        assert round(r["box"]["h"] * H_) == h and round(r["box"]["w"] * W_) == g.w, "the box is the body"
        # A top corner keeps its corner: TR's right edge, TL's left.
        spot = next(s for s in chip.spots if s["id"] == it["spot"])
        edge = (lambda b: b["x"]) if it["spot"] == "TL" else (lambda b: b["x"] + b["w"])
        assert abs(edge(r["box"]) - edge(spot["box"])) <= 2e-4 and abs(r["box"]["y"] - spot["box"]["y"]) <= 2e-4
        # The run's Stamper draws a body exactly that box.
        stamps = ir.Stamper([r], frame_size=(W_, H_))
        x, y, bw, bh = stamps.items[0].box
        assert (bw, bh) == (g.w, h) and abs(x - r["box"]["x"] * W_) <= 1 and abs(y - r["box"]["y"] * H_) <= 1


def test_an_815_like_chip_falls_back_to_a_circle_at_l1_and_fits_at_tl():
    """On run 815 an M chip clears the head only in the top corners: on the
    left it shows as a circle of its size (said so); at the top it stays a
    chip; with Where: Auto the chips take the top corners."""
    w = words(815)
    tr = track_815()
    c = ctx_for(w, judged(w, FREE_CODE), track=tr)
    left = I.plan(icon_el(style="chip", placement={"anchor": "left"}), c)
    assert shown(left) == ["Free", "Code"]
    for it in left.public()["items"]:
        assert it["spot"].startswith("L") and (it["style"], it["asked"]) == ("circle", "chip")
        assert I.W_CHIP_CIRCLE.format(label=it["label"]) in it["notes"]
        assert it["text"] is None and it["look_v"] == 1
    assert I.W_CHIP_CIRCLE.format(label="Free") in left.warnings
    assert all(r["style"] == "circle" for r in left.resolved())
    top = I.plan(icon_el(style="chip", placement={"anchor": "top"}), c)
    assert [(i["style"], i["spot"] in ("TL", "TR")) for i in top.public()["items"]] == [("chip", True)] * 2
    assert not any("too wide" in x for x in top.warnings)
    auto = I.plan(icon_el(style="chip"), c)
    assert all(i["style"] == "chip" and i["spot"] in ("TL", "TR") for i in auto.public()["items"])
    _clear_of_the_head(auto, tr)
    # The circle it fell back to is the one the run stamps: the square body.
    circle = I.plan(icon_el(style="circle", placement={"anchor": "left"}), c)
    assert [i["box"] for i in left.items] == [i["box"] for i in circle.items]


def test_each_icon_its_own_colour_ink_size_words_and_motion():
    w = words(815)
    tr = track()
    wall = backgrounds_of(tr, (240, 240, 240))
    c = ctx_for(w, judged(w, FREE_CODE), track=tr, backgrounds=wall)
    looks = {"code": {"colour": "#FFFFFF", "enter": "pop", "leave": "fade", "size": 0.12},
             "piggy-bank": {"style": "chip", "label": "Tuition-Free", "ink": "#303030"}}
    p = I.plan(icon_el(style="circle", looks=looks), c)
    free, code = item(p, "Free"), item(p, "Code")
    # Code: its own white on a white wall — kept, with its own warning; the
    # ink worked out from the fill (on white, 42 violet without a pattern).
    assert (code["fill"], code["ink"], code["style"]) == ("#ffffff", "#7f2bf2", "circle")
    assert code["notes"] == ["'Code' in white is hard to see on the background"] and code["notes"][0] in p.warnings
    assert abs(code["box"]["w"] * W_ - 0.12 * W_) <= 1 and code["size"] == 0.12
    rc_code = next(r for r in p.resolved() if r["label"] == "Code")
    assert rc_code["fx"] == {"entrance": "pop", "seed": "studio:1:scene_0:code", "exit": "fade"}
    # Free: the scene's colour (auto: violet on the wall), its own ink and words.
    assert (free["fill"], free["ink"], free["style"], free["text"]) == ("#7f2bf2", "#303030", "chip", "Tuition-Free")
    assert free["edited"] == {"look": True, "place": False, "time": False}
    assert p.colours["fill"] == "#7f2bf2"
    # Not every icon's look is its own.
    plain = I.plan(icon_el(style="circle", looks={"code": {"colour": "#303030"}}), c)
    assert item(plain, "Free")["edited"]["look"] is False and item(plain, "Code")["edited"]["look"] is True


def test_the_chip_words_are_its_name_or_its_own_cut_to_fit():
    w = words(815)
    c = ctx_for(w, judged(w, FREE_CODE), track=track())
    long = "Peer to peer learning with no teachers at all"
    el = icon_el(style="chip", placement={"anchor": "top"},
                 added=[{"glyph": {"source": "library", "id": "exam"}, "label": long[:40]}],
                 dismissed=["code"], tweaks={"exam": {"w": wi(w, "school"), "words_digest": w["digest"]}})
    p = I.plan(el, c)
    got = item(p, long[:40])
    assert len(got["text"]) <= rc.CHIP_LABEL_MAX and got["text"].endswith("…")
    assert item(p, "Free")["text"] == "Free", "the AI's name for the idea"
    assert I.chip_text("  Tuition   Free ") == "Tuition Free"


def test_in_front_is_the_users_choice_and_always_holds():
    w = words(815)
    d = judged(w, FREE_CODE)
    front = {"piggy-bank": {"layer": "front"}}
    clear = I.plan(icon_el(style="circle", looks=front), ctx_for(w, d, track=track()))
    assert item(clear, "Free")["layer"] == "front" and item(clear, "Code")["layer"] == "behind"
    assert next(r for r in clear.resolved() if r["label"] == "Free")["layer"] == "front"
    # On the face (kept there by the face guard's "warn"): still in front.
    on_face = I.plan(icon_el(style="circle", looks=front, tweaks={"piggy-bank": {"box": FACE}}),
                     ctx_for(w, d, track=track(), face="warn"))
    free = item(on_face, "Free")
    assert free["box"] == FACE and free["layer"] == "front"
    assert I.W_FRONT_FACE.format(label="Free") not in on_face.warnings
    assert next(r for r in on_face.resolved() if r["label"] == "Free")["layer"] == "front"
    # No speaker found: still in front.
    blind = I.plan(icon_el(style="circle", looks=front), ctx_for(w, d, notes=[I.W_NO_TRACK]))
    assert item(blind, "Free")["layer"] == "front" and I.W_FRONT_UNKNOWN.format(label="Free") not in blind.warnings


def test_a_box_of_the_users_takes_a_chip_by_its_height_and_nearest_edge():
    w = words(815)
    right = {"x": 0.7, "y": 0.05, "w": 0.2, "h": 0.07}           # top right: clear of the speaker
    p = I.plan(icon_el(style="chip", tweaks={"code": {"box": right}}),
               ctx_for(w, judged(w, FREE_CODE), track=track()))
    code = item(p, "Code")
    assert code["style"] == "chip" and code["edited"]["place"] is True
    assert round(code["box"]["h"] * H_) == round(right["h"] * H_)
    assert abs((code["box"]["x"] + code["box"]["w"]) - (right["x"] + right["w"])) <= 2e-4, "its right edge kept"
    assert round(code["box"]["w"] * W_) == ir.chip_geometry(
        "Code", round(right["h"] * H_), I.label_key(), max_w=ir.CHIP_MAX_W * W_).w


# ---------------------------------------------------------------------------
# Membership and pins
# ---------------------------------------------------------------------------

def test_a_removed_ai_pick_stays_out_and_is_listed_until_added_back():
    w = words(815)
    d = judged(w, FREE_CODE)
    p = I.plan(icon_el(dismissed=["code"]), ctx_for(w, d))
    assert "Code" not in shown(p)
    assert idea_of(p, "Code")["state"] == "dismissed" and idea_of(p, "Code")["reason"] == "Removed by you"
    back = I.plan(icon_el(), ctx_for(w, d))
    assert "Code" in shown(back) and idea_of(back, "Code")["state"] == "on"


def test_an_added_icon_lands_on_its_own_mention_or_waits_for_a_word():
    w = words(806)
    d = judged(w, {"piggy-bank": {"priority": 5, "label": "Free"},
                   "puzzle-piece": {"priority": 4, "label": "Projects"}})
    el = icon_el(added=[{"glyph": {"source": "library", "id": "exam"}, "label": "Exam"},
                        {"glyph": {"source": "library", "id": "swimming-pool"}, "label": "Piscine"}])
    p = I.plan(el, ctx_for(w, d))
    exam = item(p, "Exam")
    assert (exam["word"], exam["source"], exam["purpose"]) == ("school", "user", "Says 'school'")
    # The user's adds outrank the AI: Free has no room around them.
    assert idea_of(p, "Free")["reason"] == "No room right after 'Piscine'"
    # Added and never said: it still shows, at the first clear time (no word).
    pool = item(p, "Piscine")
    assert (pool["source"], pool["w"]) == ("user", None)
    # "Pick a word": any word the user chooses for it.
    el = icon_el(added=[{"glyph": {"source": "library", "id": "swimming-pool"}, "label": "Piscine"}],
                 tweaks={"swimming-pool": {"w": wi(w, "future"), "words_digest": w["digest"]}})
    p = I.plan(el, ctx_for(w, d))
    pool = item(p, "Piscine")
    assert pool["word"] == "future" and pool["pinned"] and shown(p) == ["Piscine", "Free", "Projects"]


def test_an_icon_the_ai_refused_never_lands_on_the_words_it_refused():
    """815: "... coding school without teachers". The AI refuses a teacher
    icon there (a "no"); added anyway, it lands only where its name is said
    and meant — here nowhere, so it waits for the user's word."""
    w = words(815)
    t = wi(w, "teachers")
    ref = idea(w, "person", [t], priority=3, label="Teachers")
    ref.update({"verdict": "refuse", "reason": "Said as a negation", "auto": False})
    d = director_of(idea(w, "piggy-bank", [wi(w, "free")], label="Free"), ref)
    el = icon_el(added=[{"glyph": {"source": "library", "id": "person"}, "label": "Teachers"}])
    p = I.plan(el, ctx_for(w, d, track=track()))
    # Added anyway, it still shows: at a clear time, never on the refused words.
    assert "Teachers" in shown(p) and "Free" in shown(p)
    row = item(p, "Teachers")
    assert (row["w"], row["source"]) == (None, "user")
    # Not added, it is listed as the AI refused it.
    plain = idea_of(I.plan(icon_el(), ctx_for(w, d, track=track())), "Teachers")
    assert (plain["state"], plain["w"], plain["auto"]) == ("refused", t, False)
    # The word the user picks for it is the one it lands on.
    other = wi(w, "school")
    el = icon_el(added=[{"glyph": {"source": "library", "id": "person"}, "label": "Teachers"}],
                 tweaks={"person": {"w": other, "words_digest": w["digest"]}})
    assert item(I.plan(el, ctx_for(w, d, track=track())), "Teachers")["word"] == "school"


def test_removing_an_icon_the_ai_also_picks_takes_it_off_screen():
    """The studio's Remove puts it on `dismissed` whoever chose it (and off
    `added`): the AI's own pick of the same icon then stays gone too."""
    w = words(815)
    d = director_of(idea(w, "piggy-bank", [wi(w, "free")], label="Free"))
    added = icon_el(added=[{"glyph": {"source": "library", "id": "piggy-bank"}, "label": "Free"}])
    assert shown(I.plan(added, ctx_for(w, d, track=track()))) == ["Free"]
    removed = I.plan(icon_el(dismissed=["piggy-bank"]), ctx_for(w, d, track=track()))
    assert shown(removed) == [] and idea_of(removed, "Free")["state"] == "dismissed"
    assert idea_of(removed, "Free")["auto"] is True, "the AI's own: Add back only un-removes it"
    with pytest.raises(rc.RecipeError):
        icon_el(added=[{"glyph": {"source": "library", "id": "piggy-bank"}, "label": "Free"}],
                dismissed=["piggy-bank"])


def test_a_library_icon_the_catalogue_no_longer_has_is_left_out_and_said():
    w = words(815)
    d = judged(w, FREE_CODE)
    el = icon_el(added=[{"glyph": {"source": "library", "id": "gone-in-a-rebake"}, "label": "Gone"}])
    p = I.plan(el, ctx_for(w, d, track=track()))
    assert "Gone" not in shown(p) and I.W_GONE.format(label="Gone") in p.warnings
    row = idea_of(p, "Gone")
    assert (row["state"], row["reason"], row["added"]) == ("gone", I.GONE, True), "listed, to be removed"
    assert shown(p) == ["Free", "Code"]


LINE = "Our school is free. We teach you to code with real projects, and it stays free for all of you."


def _free_code(w: dict, *, free_at: list[int], code_at: list[int]) -> dict:
    return director_of(idea(w, "piggy-bank", free_at, label="Free"),
                       idea(w, "code", code_at, priority=4, label="Code"))


def test_a_pin_moves_an_icon_to_another_mention_or_off_its_word():
    w = line_words(LINE)
    free1, free2, code = wi(w, "free"), wi(w, "free", 1), wi(w, "code")
    d = _free_code(w, free_at=[free1, free2], code_at=[code])
    c = ctx_for(w, d, enters=False)
    assert [(i["label"], i["w"]) for i in I.plan(icon_el(), c).public()["items"]] == \
        [("Free", free1), ("Code", code)]
    p = I.plan(icon_el(tweaks={"piggy-bank": {"w": free2, "words_digest": w["digest"]}}), c)
    assert [(i["label"], i["w"], i["pinned"]) for i in p.public()["items"]] == \
        [("Code", code, False), ("Free", free2, True)]
    # Off its word, only because the user chose it: exactly then.
    p = I.plan(icon_el(tweaks={"code": {"off_word": True, "sec": 1.0}}), c)
    got = item(p, "Code")
    assert got["settle"] == 1.0 and got["off_word"] and got["pinned"]
    assert item(p, "Free")["w"] == free2, "Free steps to its other mention"
    # A word that is not one of its mentions: the nearest mention.
    p = I.plan(icon_el(tweaks={"piggy-bank": {"w": code, "words_digest": w["digest"]}}), c)
    assert item(p, "Free")["w"] == free1 and "'Free' isn't said there, so it stays on 'free'" in p.warnings
    # A pin from an older take of the scene: re-anchored to a mention, said so.
    p = I.plan(icon_el(tweaks={"piggy-bank": {"w": free2, "words_digest": "0000000000000000"}}), c)
    assert item(p, "Free")["w"] == free2 and any("older take" in x for x in p.warnings)
    loose = icon_el(added=[{"glyph": {"source": "library", "id": "swimming-pool"}, "label": "Piscine"}],
                    tweaks={"swimming-pool": {"w": 3, "words_digest": "0000000000000000"}})
    p = I.plan(loose, c)
    # Its word is gone, so it shows at a clear time instead (an add always shows).
    assert "Piscine" in shown(p) and item(p, "Piscine")["w"] is None
    assert "'Piscine' lost its word after the new take" in p.warnings


def test_reorder_reanchors_the_icon_or_the_ones_it_jumps_and_never_swaps():
    w = line_words(LINE)
    free1, free2, code = wi(w, "free"), wi(w, "free", 1), wi(w, "code")
    # 1. Free is said earlier too: moving it first lands it there.
    d = director_of(idea(w, "piggy-bank", [free2, free1], label="Free"),
                    idea(w, "code", [code], priority=4, label="Code"))
    d["ideas"][0].update({"w": free2, "w_end": free2, "s": onset(w, free2)})
    c = ctx_for(w, d, enters=False)
    assert shown(I.plan(icon_el(), c)) == ["Code", "Free"]
    got = I.reorder(icon_el(), c, "piggy-bank", 0)
    assert got["ok"] and got["tweaks"] == {"piggy-bank": {"w": free1, "words_digest": w["digest"],
                                                           "off_word": False, "sec": None}}
    assert shown(I.plan(I.with_tweaks(icon_el(), got["tweaks"]), c)) == ["Free", "Code"]
    # 2. Code has no earlier mention: Free (jumped) moves to its later one.
    c = ctx_for(w, _free_code(w, free_at=[free1, free2], code_at=[code]), enters=False)
    got = I.reorder(icon_el(), c, "code", 0)
    assert got["ok"] and list(got["tweaks"]) == ["piggy-bank"] and got["tweaks"]["piggy-bank"]["w"] == free2
    # 3. Neither: nothing moves — the question, with the pin that would do it.
    c = ctx_for(w, _free_code(w, free_at=[free2], code_at=[code]), enters=False)
    assert shown(I.plan(icon_el(), c)) == ["Code", "Free"]
    got = I.reorder(icon_el(), c, "piggy-bank", 0)
    assert not got["ok"] and got["tweaks"] == {}
    assert got["choice"]["message"] == f"'Free' is only said at {onset(w, free2):.1f} s, after 'Code'"
    anyway = got["choice"]["anyway"]
    moved = I.plan(I.with_tweaks(icon_el(), anyway["tweaks"]), c)
    assert shown(moved) == ["Free", "Code"] and item(moved, "Free")["off_word"]
    assert item(moved, "Code")["w"] == code, "Code keeps its word: never a swap"


# ---------------------------------------------------------------------------
# Generate and the run: the frozen plan and the stamper hand-off
# ---------------------------------------------------------------------------

def test_generate_freezes_the_plan_and_the_run_stamps_exactly_that():
    w = words(815)
    d = judged(w, FREE_CODE)
    d42 = rc.preset_42_default("scene_0").model_dump(mode="json")
    recipe = rc.normalize({**d42, "elements": [*d42["elements"], {"id": "icons", "kind": "icon"}]})
    el = rc.icon_element(recipe)
    p = I.plan(el, ctx_for(w, d, track=track()))
    frozen = I.materialise(recipe, p)
    assert rc.icon_element(frozen).props["resolved"] == p.resolved()
    assert el.props == {}, "the draft's element is never rewritten"
    assert [e.model_dump() for e in frozen.elements if e.kind == "pattern"] == \
        [e.model_dump() for e in recipe.elements if e.kind == "pattern"]
    first = p.resolved()[0]
    assert first["fx"] == {"entrance": "glitch", "seed": "studio:1:scene_0:piggy-bank"}
    assert (first["from_sec"], first["settle_sec"], first["to_sec"]) == (2.8333, 3.1, 4.3)

    got, made = asyncio.run(I.plan_for_run(frozen))
    assert got == p.resolved() and made is None, "the run reads only resolved"

    async def ctx():
        return ctx_for(w, d, track=track())

    got, made = asyncio.run(I.plan_for_run(recipe, ctx))
    assert got == p.resolved() and made is not None and made.key() == p.key()
    assert asyncio.run(I.plan_for_run(recipe)) == ([], None)
    assert asyncio.run(I.plan_for_run(rc.preset_42_default("scene_0"), ctx)) == ([], None)

    stamps = asyncio.run(I.stampers(got, [(0.0, 4.0), (4.0, 3.97)], frame_size=(W_, H_)))
    assert all(isinstance(s, ir.Stamper) for s in stamps)
    frame = np.full((H_, W_, 3), 200, np.uint8)
    assert stamps[0].under(frame, 0.5) is frame                        # nothing on screen yet
    on = stamps[0].under(frame, 3.5)
    x0 = int(round(px(first["box"])[0]))
    assert (on != frame).any() and not (on[:, :max(0, x0 - 2)] != frame[:, :max(0, x0 - 2)]).any()
    assert stamps[1].under(frame, 4.867 - 4.0 + 0.5) is not frame     # Code, chunk-local time
    assert asyncio.run(I.stampers([], [(0.0, 4.0)], frame_size=(W_, H_))) == [None]
    late = asyncio.run(I.stampers(got[1:], [(0.0, 4.0), (4.0, 3.97)], frame_size=(W_, H_)))
    assert late[0] is None and isinstance(late[1], ir.Stamper)


def test_omni_keeping_its_own_icons_means_none_of_ours():
    w = words(815)
    p = I.plan(icon_el(omni_own="keep"), ctx_for(w, judged(w, FREE_CODE)))
    assert p.items == [] and I.W_OMNI_KEEP in p.warnings
    assert idea_of(p, "Free")["state"] == "available"


# ---------------------------------------------------------------------------
# The studio: the reel's words, the prompts, the lazy inputs
# ---------------------------------------------------------------------------

GOLD = Path(__file__).parent / "fixtures" / "rebrand_golden"
FRAME, INTRO, PERSON = GOLD / "frame.png", GOLD / "intro.mp4", GOLD / "person.png"
COLOURS = {"ground": "#3d3c3d", "ink": "#f4f4f8", "fell_back": False, "frame": None, "colour": "#2ed8e8"}


def with_icons(**el) -> rc.Recipe:
    d42 = rc.preset_42_default("scene_0").model_dump(mode="json")
    return rc.normalize({**d42, "seed": "studio:1:scene_0",
                         "elements": [*d42["elements"], {"id": "icons", "kind": "icon", **el}]})


def test_the_studio_reads_the_reels_words_for_the_director(tmp_path, monkeypatch):
    from app.services import rebrand_media as media

    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "studio")
    monkeypatch.setattr(studio, "_PREPARED", {})
    monkeypatch.setattr(studio, "_warm", lambda scene: None)
    src = tmp_path / "scene1.mp4"
    src.write_bytes(b"scene-one")
    md = {"scene_count": 2, "aspect_ratio": "9:16",
          "scenes": [{"dialogue": "Welcome to the school."}, {"dialogue": "It's free, and you learn to code."}]}

    async def run_row(run_id):
        return {"id": run_id, "topic": "Why 42", "metadata": md}

    async def bookend(*a, **k):
        return None

    async def scenes(md_, cache, *, strict=True, force_index=None):
        return [pipeline.Segment(kind="scene", index=i, source=src, is_still=False) for i in (0, 1)]

    def normalize(source, out, **kw):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, out)
        return Path(out)

    def extract(video, out, *, at):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FRAME, out)
        return Path(out)

    async def frame(video, out, at):
        return extract(video, out, at=at)

    monkeypatch.setattr(studio.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(pipeline, "_bookend", bookend)
    monkeypatch.setattr(pipeline, "_scenes", scenes)
    monkeypatch.setattr(pipeline, "_frame", frame)
    monkeypatch.setattr(media, "normalize", normalize)
    monkeypatch.setattr(media, "split_chunks", lambda base, out: [normalize(base, Path(out) / "c0.mp4")])
    monkeypatch.setattr(media, "extract_frame", extract)
    monkeypatch.setattr(media, "probe_duration", lambda p: 6.0)
    scene = asyncio.run(studio.prepare(1, "scene_1"))
    assert scene.ctx.run_ctx["topic"] == "Why 42" and scene.ctx.run_ctx["target"] == "scene_1"
    assert studio._scene_lines(scene) == ("It's free, and you learn to code.", ["Welcome to the school."],
                                          "Why 42")
    assert studio._scene_clip(scene).name == "scene1.mp4"


def test_the_prompts_tell_omni_to_draw_none_only_when_the_icons_are_ours():
    seed = "studio:1:scene_0"
    d42 = rc.normalize({**rc.preset_42_default("scene_0").model_dump(mode="json"), "seed": seed})
    # The 42 default's preview key is what it always was.
    body = {"v": studio.PREVIEW_VERSION,
            "patterns": [studio._keyed_pattern(e, d42.colours) for e in rc.pattern_elements(d42)],
            "seed": seed, "guards": d42.guards.model_dump(mode="json"), "head": d42.director.head}
    assert studio._preview_key(d42, seed) == \
        hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    assert studio._preview_key(with_icons(), seed) != studio._preview_key(d42, seed)
    assert studio._preview_key(with_icons(props={"omni_own": "keep"}), seed) == studio._preview_key(d42, seed)
    assert studio._preview_key(with_icons(enabled=False), seed) == studio._preview_key(d42, seed)
    # On top of a version, icons switched off take its own away (no clause),
    # where no icon element stamps them again (the clause): two previews.
    on_v = {"version_id": 9, "mode": "stack"}
    assert studio._preview_key(with_icons(enabled=False), seed, base=on_v) != \
        studio._preview_key(d42, seed, base=on_v)
    assert hprompt.ICONS_CLAUSE == (" Add no new drawn symbols, pictograms, badges or figures anywhere in "
                                    "the frame; only the patterns are added; the speaker stays exactly as shot.")


@pytest.fixture
def scene_env(tmp_path, monkeypatch):
    """A prepared one-part scene over the golden frame (colours on disk, the
    local mask faked) and the icon inputs faked: the 815 words, a synthetic
    track, a scripted director. Counts every input's calls."""
    from PIL import Image

    from app.services import person_matte

    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    monkeypatch.setattr(studio, "_PREPARED", {})
    monkeypatch.setattr(studio, "_MASK_TASKS", {})
    monkeypatch.setattr(studio, "_PREVIEWS", {})
    monkeypatch.setattr(studio, "_COLOUR_TASKS", {})
    person = np.asarray(Image.open(PERSON).convert("L"), dtype="float32") / 255.0

    async def local_mask(frame):
        return person

    async def paid(frame):
        raise AssertionError("the studio asked for the paid (Gemini) mask")

    monkeypatch.setattr(person_matte, "local_mask", local_mask)
    monkeypatch.setattr(person_matte, "person_mask", paid)
    work = tmp_path / "1" / "scene_0_abc"
    work.mkdir(parents=True)
    first = work / "c0_first.png"
    shutil.copyfile(FRAME, first)
    (work / "colours.json").write_text(json.dumps(COLOURS), encoding="utf-8")
    intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
    seg = pipeline.Segment(kind="scene", index=0, source=first, is_still=False)
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=720, height=1280, work=work, options={},
                       run_ctx={"topic": "Why 42", "scene_dialogue": {0: words(815)["script"]}},
                       intro=intro, scenes=[seg], outro=None)
    ctx.target_seg = seg
    ctx.frames["th_first"] = first
    scene = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=720, height=1280, work=work,
                         ctx=ctx, chunks=[work / "c0.mp4"], firsts=[first], seconds=[7.97])
    studio._PREPARED[(1, "scene_0")] = scene
    gate = threading.Event()
    gate.set()
    hold = threading.Event()             # the director answers once it is set
    hold.set()
    seen = {"words": 0, "track": 0, "direct": [], "gate": gate, "scene": scene, "track_fails": False,
            "hold": hold, "ai_down": False}

    async def fake_words(sc):
        seen["words"] += 1
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return words(815)

    async def fake_track(sc):
        seen["track"] += 1
        if seen["track_fails"]:
            raise RuntimeError("MODNet cannot run here")
        return track(), None

    async def fake_direct(c):
        seen["direct"].append((c.take, c.ai, len(c.spots)))
        while not hold.is_set():
            await asyncio.sleep(0.01)
        if seen["ai_down"]:
            # As director.direct answers when the model fails: keyword
            # suggestions only, the failure remembered.
            cands = D.candidates(c.words)
            return {"version": D.DIRECTOR_VERSION, "key": "k", "source": "keywords",
                    "ideas": D.fallback(c.words, cands, D.keyword_catalog(), "AI unavailable"),
                    "room": {"spots": [], "home": None, "home_n": None, "home_why": ""},
                    "candidates": cands, "note": "AI unavailable", "failed_at": time.time()}
        return judged(c.words, FREE_CODE)

    monkeypatch.setattr(studio, "_icon_words", fake_words)
    monkeypatch.setattr(studio, "_icon_track", fake_track)
    monkeypatch.setattr(studio.dmod, "direct", fake_direct)
    return seen


async def _ready(call, *, tries: int = 300):
    body = await call()
    for _ in range(tries):
        if body["state"] == "ready":
            return body
        await asyncio.sleep(0.02)
        body = await call()
    raise AssertionError(f"never ready: {body}")


def test_the_plan_places_at_once_then_settles_on_the_pattern_preview(scene_env):
    recipe = with_icons()

    async def main():
        plan = lambda: studio.icon_plan(1, "scene_0", recipe)  # noqa: E731
        first = await _ready(plan)
        masks = first["underlay"].pop("masks")
        assert first["provisional"] and first["underlay"] == {"kind": "scene", "video_url": None,
                                                              "pattern_url": None}
        # The speaker's masks, for the moving preview to cut by: one strip.
        assert masks["times"] == [round(float(t), 3) for t in track().times] and \
            (masks["width"], masks["height"]) == (W_ // 8, H_ // 8)
        with Image.open(studio.resolve_file(masks["url"].split("/file/", 1)[1])) as im:
            assert im.mode == "L" and im.size == (W_ // 8, len(masks["times"]) * H_ // 8)
        assert [i["label"] for i in first["items"]] == ["Free", "Code"]
        await studio.preview(1, "scene_0", recipe)
        final = await plan()
        return first, final

    first, final = asyncio.run(main())
    assert not final["provisional"] and final["timing_key"] == first["timing_key"]
    assert final["plan_key"] != first["plan_key"]
    # The patterns UNCUT (the preview's layers), which the moving preview
    # cuts the speaker out of at each moment — never the target's ink, with
    # the first frame's speaker cut out of it (issue 7's ghost).
    assert final["underlay"]["pattern_url"].endswith(studio.LAYERS_PNG)
    assert studio.resolve_file(final["underlay"]["pattern_url"].split("/file/", 1)[1]).is_file()
    assert [i["label"] for i in final["items"]] == ["Free", "Code"]
    assert scene_env["words"] == 1 and scene_env["track"] == 1 and len(scene_env["direct"]) == 1
    kept = studio.kept_plan(1, "scene_0", final["plan_key"])
    assert kept["plan"]["plan_key"] == final["plan_key"] and len(kept["resolved"]) == 2


def test_the_plan_hands_the_stage_each_icons_sprite_as_the_run_stamps_it(scene_env):
    """New drafts' circles (and a chip of its own): each shown icon comes
    with its settled sprite, the Stamper's own pixels for the item Generate
    freezes — a local PNG, no bucket."""
    recipe = with_icons(props={"style": "circle", "looks": {"code": {"style": "chip", "label": "Coding"}}},
                        placement={"anchor": "top"})
    body = asyncio.run(_ready(lambda: studio.icon_plan(1, "scene_0", recipe)))
    kept = studio.kept_plan(1, "scene_0", body["plan_key"])
    assert [(i["label"], i["style"]) for i in body["items"]] == [("Free", "circle"), ("Code", "chip")]
    for item_, frozen in zip(body["items"], kept["resolved"]):
        sp = item_["sprite"]
        png = np.asarray(Image.open(studio.resolve_file(sp["url"].split("/file/", 1)[1])))
        want = ir.Stamper([frozen], frame_size=(W_, H_)).sprite_of(0)
        assert np.array_equal(png, want.rgba) and sp["ox"] == want.pad
        assert item_["d"] == ic.library_d(frozen["glyph"]["id"], "bold")
    assert body["items"][1]["text"] == "Coding" and kept["resolved"][1]["text"] == "Coding"


def test_the_preview_of_a_draft_with_icons_tells_omni_and_starts_the_inputs(scene_env):
    async def main():
        body = await studio.preview(1, "scene_0", with_icons())
        await asyncio.gather(*(j.task for j in studio._ICON_TASKS.values()), return_exceptions=True)
        plain = await studio.preview(1, "scene_0", rc.normalize({**rc.preset_42_default("scene_0").model_dump(
            mode="json"), "seed": "studio:1:scene_0"}))
        return body, plain

    body, plain = asyncio.run(main())
    assert body["chunks"][0]["prompt"].endswith(hprompt.ICONS_CLAUSE.strip())
    assert hprompt.ICONS_CLAUSE.strip() not in plain["chunks"][0]["prompt"]
    assert scene_env["words"] == 1 and scene_env["track"] == 1 and len(scene_env["direct"]) == 1


def test_icons_added_on_top_of_a_version_are_planned_over_it(scene_env, monkeypatch):
    from app.services.rebrand_harness import base as hbase

    scene = scene_env["scene"]
    ink = np.zeros((H_, W_), np.float32)
    ink[:, :150] = 1.0                                  # the version's own strip, on the left
    exp = {"id": 900, "run_id": 1, "status": "ready", "options": {"target": "scene_0"},
           "evidence": {"omni_pattern": {"colour": "#7f2bf2"}},
           "arms": {"omni": {"status": "ready", "final_path": "v900.mp4"}}}
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(version_id=900, mode="stack"), exp=exp, chain=[exp], layers=[],
                           sources=[scene.firsts[0]], frames=[scene.firsts[0]], occupied=ink)

    async def prepare_base(run_id, target, version_id):
        return bv

    monkeypatch.setattr(studio, "prepare_base", prepare_base)
    monkeypatch.setattr(hbase, "entering", lambda e, j: True)
    monkeypatch.setattr(I, "backgrounds", lambda *a, **k: None)     # its parts are stills here
    icons_only = rc.normalize({"target": "scene_0", "seed": "studio:1:scene_0",
                               "elements": [{"id": "icons", "kind": "icon"}]})
    body = asyncio.run(_ready(lambda: studio.icon_plan(1, "scene_0", icons_only,
                                                       hbase.BaseSpec(version_id=900, mode="stack"))))
    assert body["underlay"]["kind"] == "version" and body["underlay"]["version_id"] == 900
    assert "/experiments/900/" in (body["underlay"]["video_url"] or "")
    assert not body["provisional"] and [i["label"] for i in body["items"]] == ["Free", "Code"]
    # Beside the version's own strip (its side), after its entrance.
    assert body["home"]["spot"].startswith("L") and "on the pattern's side" in body["home"]["why"]
    assert any(z["why"] == I.ZONE_ENTRANCE for z in body["timeline"]["zones"])


def test_the_icon_inputs_of_older_scenes_are_let_go(monkeypatch):
    monkeypatch.setattr(studio, "ICON_SCENES_KEPT", 1)

    async def one():
        return 1

    async def main():
        await studio._icon_job("A|words", one).task
        await studio._icon_job("B|words", one).task
        await studio._icon_job("B|spots", one).task
        return list(studio._ICON_TASKS)

    assert asyncio.run(main()) == ["B|words", "B|spots"]


def test_a_track_that_cannot_be_made_is_remembered_and_the_plan_goes_without(scene_env):
    scene_env["track_fails"] = True

    async def main():
        plan = lambda: studio.icon_plan(1, "scene_0", with_icons())  # noqa: E731
        body = await _ready(plan)
        again = await plan()
        return body, again

    body, again = asyncio.run(main())
    assert {s["name"]: s["state"] for s in body["steps"]}["spots"] == "failed"
    assert I.W_NO_TRACK in body["warnings"] and [i["label"] for i in body["items"]] == ["Free", "Code"]
    assert scene_env["track"] == 1, "a failure is not retried on every ask"
    assert again["plan_key"] == body["plan_key"]


def test_ask_the_ai_again_only_after_it_failed_and_once_while_it_asks(scene_env):
    """Each take is a paid call: "Ask the AI again" starts one only when
    this take answered without the AI, and clicks while it asks start none."""
    scene_env["ai_down"] = True
    recipe = with_icons()

    async def main():
        plan = lambda **kw: studio.icon_plan(1, "scene_0", recipe, **kw)  # noqa: E731
        first = await _ready(plan)
        scene_env["ai_down"] = False
        scene_env["hold"].clear()                      # the new take takes a while
        asked = await plan(ask_again=True)
        clicks = []
        for _ in range(3):
            await asyncio.sleep(0.02)
            clicks.append(await plan(ask_again=True))
        takes_while_asking = [d[0] for d in scene_env["direct"]]
        scene_env["hold"].set()
        done = await _ready(plan)
        after = await plan(ask_again=True)             # the AI answered: nothing more to ask
        return first, asked, clicks, takes_while_asking, done, after

    first, asked, clicks, while_asking, done, after = asyncio.run(main())
    assert first["source"]["director"] == "keywords" and first["items"] == [] and I.W_AI_DOWN in first["warnings"]
    assert asked["state"] == "warming" and all(c["state"] == "warming" for c in clicks)
    assert {s["name"]: s["state"] for s in asked["steps"]}["director"] == "running"
    assert while_asking == [0, 1], "one new take, however many clicks while it asks"
    assert done["source"]["director"] == "ai" and [i["label"] for i in done["items"]] == ["Free", "Code"]
    assert after["plan_key"] == done["plan_key"] and [d[0] for d in scene_env["direct"]] == [0, 1]


def test_words_worked_out_again_ask_the_director_again(scene_env, monkeypatch):
    """The director's answer indexes the words it was given: when the words
    are worked out again (a failed try retried), it is not reused."""
    calls = {"n": 0}

    async def words_fail_then_whisper(sc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no word timing")
        return words(815)

    monkeypatch.setattr(studio, "_icon_words", words_fail_then_whisper)
    recipe = with_icons()

    async def main():
        plan = lambda: studio.icon_plan(1, "scene_0", recipe)  # noqa: E731
        first = await _ready(plan)                     # the failure is kept: the estimate
        monkeypatch.setattr(studio, "ICON_RETRY_SEC", 0)
        second = await _ready(plan)                    # tried again: Whisper's words
        return first, second

    first, second = asyncio.run(main())
    assert first["source"]["words"] == "estimate" and second["source"]["words"] == "whisper"
    assert len(scene_env["direct"]) == 2, "asked again on the new words"
    assert first["words_digest"] != second["words_digest"]


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt
    from app.services import pattern_library as lib

    async def list_patterns():
        return list(lib.BUILTINS.values())

    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "list_patterns", list_patterns)
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)
    app = FastAPI()
    app.include_router(rt.router)
    with TestClient(app) as c:                     # one event loop: background jobs live on
        yield c


def test_the_catalogues_offer_icons(client):
    body = client.get("/api/rebrand/harness/catalogue").json()
    icon = next(k for k in body["kinds"] if k["kind"] == "icon")
    assert icon["ready"] is True and icon["max"] == 1 and body["limits"]["icons"] == rc.ICON_ITEMS_MAX == 5
    cat = client.get("/api/rebrand/harness/icons/catalogue").json()
    assert [g["group"] for g in cat["picks"]] == ["Money", "Code", "People", "Learning", "Career", "Time",
                                                   "Places", "Fun"]
    # No Names tab any more: one search, and what a drawing costs.
    assert "names" not in cat and cat["generate"]["per_day"] == 25
    assert cat["rules"] == I.rules() and cat["sizes"] == {"S": 0.12, "M": 0.16, "L": 0.20}
    # Circle and Circle + words; the old looks are offered only while in use.
    assert [(s["value"], s.get("old", False)) for s in cat["styles"]] == \
        [("circle", False), ("chip", False), ("badge", True), ("glyph", True)]
    assert [e["value"] for e in cat["entrances"]] == ["glitch", "pop", "fade", "slide", "none"]
    assert [a["value"] for a in cat["anchors"]] == list(rc.ICON_ANCHORS) and cat["count"] > 1000
    assert "icon_props" in client.get("/api/rebrand/harness/schema").json()


def test_icon_search_pages(client):
    got = client.get("/api/rebrand/harness/icons/search", params={"q": "coffee"}).json()
    assert got["items"][0]["id"] == "coffee" and got["total"] >= 1 and got["strong"]
    page = client.get("/api/rebrand/harness/icons/search", params={"q": "", "offset": 10, "limit": 5}).json()
    assert len(page["items"]) == 5 and page["total"] > 60


def _drawing() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (512, 512), "white")
    ImageDraw.Draw(img).ellipse((128, 128, 384, 384), fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_a_name_nothing_matches_is_drawn_only_on_a_press(client, monkeypatch, guard):
    """Typing never draws (nor asks the AI): a weak name gets the library's
    closest icons and the offer to generate, for free. Only the press
    (POST /icons/generate) draws; the drawing is served as a PNG, and can
    be hidden."""
    drawn: list[str] = []

    async def image(prompt, **kw):
        drawn.append(prompt)
        return _drawing()

    monkeypatch.setattr(vertex_client, "gemini_image", image)
    url = "/api/rebrand/harness/icons/search"
    strong = client.get(url, params={"q": "developer"}).json()
    assert strong["items"][0]["id"] == "code" and strong["strong"] and drawn == []
    weak = client.get(url, params={"q": "cyber cafe"}).json()
    assert not weak["strong"] and weak["generate"]["offer"] and drawn == []
    brand = client.get(url, params={"q": "instagram"}).json()
    assert not brand["generate"]["offer"] and drawn == []

    made = client.post("/api/rebrand/harness/icons/generate", json={"name": "cyber cafe"}).json()
    cid = ic.custom_id("cyber cafe")
    assert made["id"] == cid and made["created"] and len(drawn) == 1
    png_url = f"/api/rebrand/harness/icons/glyph/{cid}"
    svg = client.get("/api/rebrand/harness/icons/glyph/code")
    assert svg.headers["content-type"].startswith("image/svg+xml") and ic.library_d("code")[:20] in svg.text
    png = client.get(png_url)
    assert png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert client.get("/api/rebrand/harness/icons/glyph/no-such-icon").status_code == 404

    hide = client.delete(f"/api/rebrand/harness/icons/custom/{cid}")
    assert hide.json() == {"id": cid, "hidden": True} and guard["store"].rows[cid]["hidden_at"]
    assert client.delete(f"/api/rebrand/harness/icons/custom/{cid}").status_code == 200
    assert client.get(png_url).status_code == 200, "a hidden icon still draws"
    assert client.delete("/api/rebrand/harness/icons/custom/c-000000000000").status_code == 404
    assert client.delete("/api/rebrand/harness/icons/custom/code").status_code == 404


def test_the_plan_route_warms_up_then_answers_and_asks_the_ai_again(client, scene_env):
    url = "/api/rebrand/harness/icons/plan"
    recipe = with_icons().model_dump(mode="json")
    req = {"run_id": 1, "target": "scene_0", "recipe": recipe}
    scene_env["gate"].clear()
    first = client.post(url, json=req)
    assert first.status_code == 200
    body = first.json()
    assert body["state"] == "warming" and body["items"] == [] and body["plan_key"] is None
    steps = {s["name"]: s["state"] for s in body["steps"]}
    assert steps["words"] == "running" and steps["director"] == "running"
    scene_env["gate"].set()
    for _ in range(300):
        body = client.post(url, json=req).json()
        if body["state"] == "ready":
            break
        time.sleep(0.02)
    assert body["state"] == "ready" and [i["label"] for i in body["items"]] == ["Free", "Code"]
    assert {s["state"] for s in body["steps"]} == {"done"}
    assert body["source"] == {"words": "whisper", "director": "ai"} and body["provisional"]
    assert body["timeline"]["words"][0]["text"] == "42" and body["words_digest"] == words(815)["digest"]
    assert body["underlay"]["kind"] == "scene"
    same = client.post(url, json=req).json()
    assert same["plan_key"] == body["plan_key"] and scene_env["words"] == 1
    # A frozen plan the client sends is never read.
    res = [{"glyph": {"source": "library", "id": "code"}, "label": "Code", "from_sec": 1.0, "settle_sec": 1.3,
            "to_sec": 2.5, "box": {"x": 0.1, "y": 0.1, "w": 0.16, "h": 0.09}, "fill": "#7f2bf2",
            "ink": "#ffffff", "fx": {"entrance": "glitch", "seed": "x"}}]
    sent = copy.deepcopy(recipe)
    next(e for e in sent["elements"] if e["kind"] == "icon")["props"] = {"resolved": res}
    assert client.post(url, json={**req, "recipe": sent}).json()["plan_key"] == body["plan_key"]
    # "Ask the AI again" once the AI has answered: nothing to ask, no call.
    again = client.post(url, json={**req, "ask_again": True}).json()
    assert again["plan_key"] == body["plan_key"] and [d[0] for d in scene_env["direct"]] == [0]
    # Reordering is worked out here: Code before Free has no meaning-keeping
    # move on this line, so the answer is the question.
    moved = client.post(url, json={**req, "reorder": {"glyph": "code", "to": 0}}).json()
    assert moved["reorder"]["ok"] is False and moved["reorder"]["choice"]["message"].startswith("'Code'")
    off = client.post(url, json={**req, "recipe": rc.preset_42_default("scene_0").model_dump(mode="json")})
    assert off.json()["off"] is True and off.json()["items"] == []
    bad = copy.deepcopy(recipe)
    next(e for e in bad["elements"] if e["kind"] == "icon")["props"] = {"colour": "violet"}
    r = client.post(url, json={**req, "recipe": bad})
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["field"] == "props.colour"
