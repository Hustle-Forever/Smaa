"""Rebrand studio icons by hand (elements/icon.py place + Scheduler, the
lane's and the stage's numbers in the studio).

A box the user put an icon in is kept EXACTLY — over the speaker too: behind
the speaker and said, with the clear spot nearest to it offered ("Move to a
clear spot"), in front of the speaker only off the face ("Bring to front"). A
time set by hand (off its word) is a FIXED slot: placed first, exactly there,
inside the scene, the zones only warned about; two may overlap, never in one
place the planner picked, and every other icon keeps clear of them. The lane
writes a block as a pin the planner times back within a frame, and the stage's
outline is the planner's own speaker rule — both through shared fixtures the
TypeScript is checked against too (frontend/scripts/check-rebrand-span.mjs).
The icon plan's masks carry the heads, the bars and the samples' timing.

Words are the REAL 815 Whisper fixture, the director is scripted, the
speaker track synthetic (tests/test_icons_plan.py's). No model is called:
an autouse guard makes every Vertex call raise. No database, no bucket."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.services import vertex_client
from app.services.rebrand_harness import speaker_track as T
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import icon as I
from test_icons_plan import FACE, FREE_CODE, _cue, ctx_for, icon_el, item, judged, shown, track, words

FIX = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]
BODY = {"x": 0.35, "y": 0.6, "w": 0.16, "h": 0.09}         # on the speaker's body, off the head
FPS = 30.0


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    calls: list[str] = []

    def refuse(name):
        async def _refuse(*a, **k):
            calls.append(name)
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse("gemini_multimodal"))
    monkeypatch.setattr(vertex_client, "gemini_image", refuse("gemini_image"))
    yield
    assert not calls, f"unexpected paid calls: {calls}"


def plan_815(**props) -> I.IconPlan:
    w = words(815)
    return I.plan(icon_el(**props), ctx_for(w, judged(w, FREE_CODE), track=track()))


def _hits(a: dict, b: dict) -> bool:
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and \
        b["y"] < a["y"] + a["h"]


# ---------------------------------------------------------------------------
# A box put by hand
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("face", ["shift", "reject", "warn"])
def test_a_box_over_the_speaker_is_kept_exactly_said_and_offers_the_nearest_clear_spot(face):
    w = words(815)
    tr = track()
    p = I.plan(icon_el(tweaks={"piggy-bank": {"box": BODY}}), ctx_for(w, judged(w, FREE_CODE), track=tr, face=face))
    free = item(p, "Free")
    assert free["box"] == BODY and free["spot"] is None and free["layer"] == "behind"
    assert free["covered"] == "speaker" and free["max_overlap"] > T.OVERLAP_MAX
    assert "'Free' is partly behind the speaker at 2.5 s" in p.warnings and free["notes"] == [
        "'Free' is partly behind the speaker at 2.5 s"]
    # Generate freezes it where it was put.
    assert next(r for r in p.resolved() if r["label"] == "Free")["box"] == BODY
    # "Move to a clear spot": clear for its window, at its size, and no clear
    # spot is nearer to where it was put.
    clear = free["clear"]
    t0, t1 = free["from"] - I.WINDOW_PAD, free["to"] + I.WINDOW_PAD
    assert tr.feasible(clear, t0, t1) and (clear["w"], clear["h"]) == (BODY["w"], BODY["h"])

    def dist(b):
        return np.hypot(b["x"] + b["w"] / 2 - (BODY["x"] + BODY["w"] / 2), b["y"] + b["h"] / 2 - (BODY["y"] + BODY["h"] / 2))

    others = [s["box"] for s in tr.spots if tr.feasible(s["box"], t0, t1)]
    assert others and all(dist(clear) <= dist(b) + 1e-9 for b in others)
    # Moved there, it is clear and says nothing.
    moved = I.plan(icon_el(tweaks={"piggy-bank": {"box": clear}}), ctx_for(w, judged(w, FREE_CODE), track=tr, face=face))
    assert item(moved, "Free")["box"] == clear and item(moved, "Free")["covered"] is None
    assert not [x for x in moved.warnings if "behind the speaker" in x]


def test_bring_to_front_holds_over_her_body_and_over_her_face():
    front = {"piggy-bank": {"layer": "front"}}
    body = plan_815(style="circle", looks=front, tweaks={"piggy-bank": {"box": BODY}})
    free = item(body, "Free")
    assert free["layer"] == "front" and free["box"] == BODY and free["covered"] == "speaker"
    assert next(r for r in body.resolved() if r["label"] == "Free")["layer"] == "front"
    assert not [x for x in body.warnings if "behind the speaker" in x], "in front of the speaker: not behind"
    face = plan_815(style="circle", looks=front, tweaks={"piggy-bank": {"box": FACE}})
    free = item(face, "Free")
    assert free["box"] == FACE and free["layer"] == "front" and free["covered"] == "face"
    assert I.W_FRONT_FACE.format(label="Free") not in face.warnings
    assert not [x for x in face.warnings if "behind the speaker" in x], "in front of the speaker: not behind"


def test_a_box_outside_the_frame_is_kept_inside_at_its_size_and_a_clear_one_says_nothing():
    p = plan_815(tweaks={"piggy-bank": {"box": {"x": 0.9, "y": -0.05, "w": 0.16, "h": 0.09}}})
    assert item(p, "Free")["box"] == {"x": 0.84, "y": 0.0, "w": 0.16, "h": 0.09}
    clear = {"x": 0.05, "y": 0.62, "w": 0.16, "h": 0.09}
    p = plan_815(tweaks={"code": {"box": clear}})
    code = item(p, "Code")
    assert code["box"] == clear and code["covered"] is None and code["clear"] is None and code["notes"] == []


# ---------------------------------------------------------------------------
# A time set by hand
# ---------------------------------------------------------------------------

def _fixed(key: str, sec: float, hold: float | None = None, **kw) -> I.Cue:
    c = _cue(key, sec, tier=0, **kw)
    c.fixed, c.off_word, c.pinned, c.anchors, c.hold = sec, True, True, [], hold
    return c


def test_a_time_set_by_hand_is_a_fixed_slot_inside_the_scene_the_zones_only_said():
    s = I.Scheduler(duration=8.0, enters=True)
    slots, drops = s.run([_fixed("early", 0.2, 0.5), _fixed("late", 7.5, 2.0)])
    assert drops == {}
    early, late = sorted(slots, key=lambda x: x.settle_f)
    assert (early.from_f, early.settle_f, early.hold_f) == (0, I.IN_F, 15), "comes in from the first frame"
    assert (late.settle_f, late.to_f) == (225, 240), "gone by the last frame: its hold trimmed"
    assert s.hand_notes["early"] == [I.W_HAND_START.format(label="Early"), I.W_HAND_ENTRANCE.format(label="Early")]
    assert s.hand_notes["late"] == [I.W_HAND_END.format(label="Late")]
    # Right at the end, it still shows for the shortest length a hand sets.
    last = I.Scheduler(duration=8.0, enters=False).fixed_slot(_fixed("x", 7.99, 5.0))
    assert last.to_f == 240 and last.hold_f == round(0.2 * FPS)
    # Through the plan: Code off its word in the opening, where it was put.
    p = plan_815(tweaks={"code": {"off_word": True, "sec": 0.4, "hold_sec": 0.5}})
    code = item(p, "Code")
    assert (code["settle"], code["hold"], code["off_word"], code["pinned"]) == (0.4, 0.5, True, True)
    assert code["notes"] == ["'Code' shows in the opening half second, where you put it",
                             "'Code' shows while the pattern comes in, where you put it"]
    assert all(n in p.warnings for n in code["notes"])


def test_fixed_slots_may_overlap_and_everything_else_keeps_clear_of_them():
    s = I.Scheduler(duration=12.0, enters=False)
    a, b = _fixed("a", 2.0, 2.0), _fixed("b", 3.0, 1.0)
    inside, after = _cue("inside", 3.5), _cue("after", 7.0)
    slots, drops = s.run([inside, after, a, b])
    got = {x.cue.key: x for x in slots}
    assert set(got) == {"a", "b", "after"} and drops == {"inside": "No room right after 'B'"}
    assert got["a"].from_f < got["b"].to_f and got["b"].from_f < got["a"].to_f, "the two timed by hand overlap"
    gap = s.gap(got["after"].cue, got["a"].cue)
    assert all(got["after"].from_f >= f.to_f + gap for f in (got["a"], got["b"]))
    # An icon before a fixed slot is held no further than its clear air.
    s = I.Scheduler(duration=12.0, enters=False)
    slots, _ = s.run([_cue("before", 5.0, end=9.0), _fixed("f", 7.0, 1.0)])
    before, fixed = sorted(slots, key=lambda x: x.settle_f)
    assert before.to_f + s.gap(before.cue, fixed.cue) <= fixed.from_f
    # Still at most ITEMS_MAX on screen in a scene (said as the scene's
    # limit, not "over the limit for this length").
    s = I.Scheduler(duration=12.0, enters=False)
    times = (("p", 2.0), ("q", 3.0), ("r", 4.0), ("t", 5.0), ("u", 6.0), ("v", 7.0))
    slots, drops = s.run([_fixed(k, t, 1.0) for k, t in times])
    assert len(slots) == I.ITEMS_MAX == 5 and list(drops.values()) == [I.OVER_MAX.format(n=I.ITEMS_MAX)]
    assert I._state(I.OVER_MAX.format(n=I.ITEMS_MAX)) == "limit"


def test_icons_timed_by_hand_together_never_share_a_place_the_planner_picked():
    both = {"piggy-bank": {"off_word": True, "sec": 2.0, "hold_sec": 2.0},
            "code": {"off_word": True, "sec": 2.5, "hold_sec": 2.0}}
    p = plan_815(tweaks=both)
    free, code = item(p, "Free"), item(p, "Code")
    assert free["from"] < code["to"] and code["from"] < free["to"]
    assert not _hits(free["box"], code["box"]) and not [x for x in p.warnings if "overlap" in x]
    tr = track()
    for it in (free, code):
        assert tr.feasible(it["box"], it["from"] - I.WINDOW_PAD, it["to"] + I.WINDOW_PAD)
    # Two the user put in one place: kept, and said.
    same = {"x": 0.04, "y": 0.62, "w": 0.16, "h": 0.09}
    p = plan_815(tweaks={k: {**v, "box": same} for k, v in both.items()})
    assert item(p, "Free")["box"] == item(p, "Code")["box"] == same
    # Said at Code's settle (2.5 s, as the list says it), not its entrance.
    assert "'Free' and 'Code' overlap at 2.5 s" in p.warnings


def test_the_hand_rules_are_in_the_timing_key_only_when_something_is_set_by_hand(monkeypatch):
    w = words(815)
    c = ctx_for(w, judged(w, FREE_CODE), track=track())
    cases = {
        "auto": {}, "word pin": {"tweaks": {"code": {"w": 3, "words_digest": w["digest"]}}},
        "a length": {"tweaks": {"code": {"hold_sec": 2.5}}},
        "a box": {"tweaks": {"code": {"box": BODY}}},
        "off its word": {"tweaks": {"code": {"off_word": True, "sec": 2.0}}},
    }
    before = {k: I.plan(icon_el(**v), c).timing_key for k, v in cases.items()}
    monkeypatch.setattr(I, "HAND_RULES", I.HAND_RULES + 1)
    after = {k: I.plan(icon_el(**v), c).timing_key for k, v in cases.items()}
    assert [k for k in cases if before[k] != after[k]] == ["a box", "off its word"]


# ---------------------------------------------------------------------------
# The lane's numbers and the stage's outline: the planner's own
# ---------------------------------------------------------------------------

def test_a_lane_block_set_by_hand_is_timed_back_within_a_frame():
    fx = json.loads((FIX / "rebrand_icon_span.json").read_text(encoding="utf-8"))
    clock = fx["clock"]
    assert (clock["fps"], clock["in_f"], clock["out_f"]) == (I.FPS, I.IN_F, I.OUT_F)
    frame = 1.0 / I.FPS
    for c in fx["tweaks"]:
        s = I.Scheduler(duration=40.0, enters=False)
        slot = s.fixed_slot(_fixed("x", c["sec"], c["hold_sec"]))
        assert abs(slot.from_f / I.FPS - c["from"]) <= frame + 1e-9, c
        assert abs(slot.to_f / I.FPS - c["to"]) <= frame + 1e-9, c
    # Through the plan and its frozen item, the same.
    c = fx["tweaks"][1]
    p = plan_815(tweaks={"code": {"off_word": True, "sec": c["sec"], "hold_sec": c["hold_sec"]}})
    r = next(x for x in p.resolved() if x["label"] == "Code")
    assert abs(r["from_sec"] - c["from"]) <= frame and abs(r["to_sec"] - c["to"]) <= frame


def _track_of(tr: dict) -> T.Track:
    n = len(tr["times"])
    t = T.Track(width=tr["frame"][0], height=tr["frame"][1], fps=tr["fps"], frames=10_000,
                step=tr["sample_frames"], times=np.asarray(tr["times"], np.float64),
                heads=np.asarray([h if h is not None else [-1, -1, -1, -1] for h in tr["heads"]], np.int32),
                masks=np.asarray(tr["masks"], np.uint8).reshape(n, tr["h"], tr["w"]), cuts=list(tr["cut_frames"]))
    bars = np.zeros((tr["h"], tr["w"]), bool)
    for y, x0, x1 in tr["bars"]:
        bars[y, x0:x1] = True
    t._bars_small = bars
    return t


def test_the_outline_is_the_planners_own_speaker_rule():
    fx = json.loads((FIX / "rebrand_icon_cover.json").read_text(encoding="utf-8"))
    assert fx["rules"] == {"overlap_max": T.OVERLAP_MAX, "head_grow": T.HEAD_GROW, "person": T.PERSON}
    seen = set()
    for tr in fx["tracks"]:
        t = _track_of(tr)
        for c in tr["cases"]:
            box, t0, t1 = c["box"], c["t0"], c["t1"]
            assert t.samples_in(t0, t1) == c["samples"]
            assert t.overlap(box, t0, t1) == c["overlap"]
            assert t.head_clear(box, t0, t1) is c["head_clear"]
            got = {None: "clear", "speaker": "near", "face": "over"}[t.cover(box, t0, t1)]
            assert got == c["cover"]
            seen.add(got)
    assert seen == {"clear", "near", "over"}
    rules = I.rules()
    assert (rules["overlap_max"], rules["head_grow"], rules["person"]) == (T.OVERLAP_MAX, T.HEAD_GROW, T.PERSON)


def test_the_studio_agrees_with_the_shared_fixtures():
    """frontend/scripts/check-rebrand-span.mjs runs lib/rebrandSpan.ts,
    rebrandCover.ts and rebrandTransform.ts on both fixtures (node 22.6+;
    skipped where there is none)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    if tuple(int(x) for x in version.lstrip("v").split(".")[:2]) < (22, 6):
        pytest.skip(f"node {version} cannot strip types")
    out = subprocess.run([node, "--experimental-strip-types", "scripts/check-rebrand-span.mjs"],
                         capture_output=True, text=True, timeout=120, cwd=REPO / "frontend")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cases agree" in out.stdout and "FAIL" not in out.stdout


def test_the_icon_plans_masks_carry_the_heads_the_bars_and_the_timing(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    n = 5
    bars = np.zeros((1280, 720), bool)
    bars[:400] = True
    bars[880:] = True
    heads = np.tile(np.array([[240, 420, 480, 700]], np.int32), (n, 1))
    heads[2] = -1
    tr = track(n, heads=heads, layout="letterbox", bars=bars, picture=(400, 880, 0, 720))
    body = studio._track_masks(SimpleNamespace(work=tmp_path / "s"), tr)
    assert body["heads"] == [[240, 420, 480, 700], [240, 420, 480, 700], None, [240, 420, 480, 700],
                             [240, 420, 480, 700]]
    assert body["frame"] == [720, 1280] and body["sample_frames"] == 15 and body["fps"] == 30.0
    assert "step" not in body, "`step` is the matte bank's (held samples), never the track's"
    back = np.zeros((body["height"], body["width"]), bool)
    for y, x0, x1 in body["bars"]:
        back[y, x0:x1] = True
    assert np.array_equal(back, tr._bars8()) and back.any()
    assert (tmp_path / "s" / "icons").is_dir()
