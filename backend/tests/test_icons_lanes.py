"""Rebrand studio icons on tracks (elements/icon.py: TRACKS): an icon the user
adds always shows (the owner's call, 2026-10-01).

Track 0 is the AI's: one icon at a time, the clear air, the zones, the caps,
exactly as before. One of the user's that has no room there is never left
out: on its word on a track of its own; its word in a zone, moved to the
nearest time allowed (said, with the way back); no time allowed in the whole
scene, at its word as if timed by hand. A spot the speaker takes never
refuses one of theirs either (behind her, said). The AI's picks give way to
the user's and only ever go on track 0. Tracks are the timeline's: the
frozen render list has none, and the timing key moves only for a plan these
rules changed.

The repros are the investigation's (scratchpad repro2.py): A, the owner's
card ("Future · 'future' · 0.2 s · No room · Said while the pattern comes
in"); B2, two of the user's said 0.74 s apart; B3, one timed by hand over
another of theirs. Words are the REAL 815 Whisper fixture or a char-linear
estimate of a made-up line; the director is scripted, the speaker track
synthetic (tests/test_icons_plan.py's). No model is ever called: an autouse
guard makes every Vertex call raise."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.services import vertex_client
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness.elements import icon as I
from test_icons_plan import (FREE_CODE, H_, W_, _masks, ctx_for, director_of, icon_el, idea, idea_of, item,
                             judged, line_words, shown, track, wi, words)

FPS = 30.0
REPO = Path(__file__).resolve().parents[2]
TIMES_FIX = Path(__file__).parent / "fixtures" / "rebrand_icon_times.json"
PEER = {"glyph": {"source": "custom", "id": "c-0123456789ab"}, "label": "Peer"}
LEARN = {"glyph": {"source": "custom", "id": "c-0123456789ac"}, "label": "Learning"}
TUITION = {"glyph": {"source": "custom", "id": "c-0123456789ad"}, "label": "Tuition"}
FREE = {"glyph": {"source": "library", "id": "piggy-bank"}, "label": "Free"}
FUTURE = {"glyph": {"source": "library", "id": "rocket-launch"}, "label": "Future"}
ZEBRA = {"glyph": {"source": "custom", "id": "c-00000000ffff"}, "label": "Zebra"}


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


def said_at(w: dict, text: str, s: float) -> dict:
    """`w` with the word `text` said at `s` (its end kept after it)."""
    out = copy.deepcopy(w)
    x = next(x for x in out["words"] if x["said"] and x["text"].lower() == text.lower())
    x["e"] = max(float(x.get("e") or s), s + 0.2)
    x["s"] = s
    return out


def future_words() -> dict:
    """The owner's card: "Future" said at 0.2 s, while the pattern comes in
    (the 42 default's entrance: no icon settles in the first 1.5 s)."""
    w = line_words("Future starts here at forty two where you learn to code with real projects and peers every day",
                   8.0)
    return said_at(w, "future", 0.2)


def _hits(a: dict, b: dict) -> bool:
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and \
        b["y"] < a["y"] + a["h"]


def _together(a: dict, b: dict) -> bool:
    return a["from"] < b["to"] and b["from"] < a["to"]


# ---------------------------------------------------------------------------
# Repro A: the owner's "Future" card
# ---------------------------------------------------------------------------

def test_the_owners_future_card_said_at_0_2_s_inside_the_entrance_is_placed():
    w = future_words()
    assert next(x for x in w["words"] if x["text"] == "Future")["s"] == 0.2
    p = I.plan(icon_el(added=[FUTURE]), ctx_for(w, director_of()))
    # Before: nothing shown, "No room · Said while the pattern comes in".
    assert shown(p) == ["Future"]
    fut = item(p, "Future")
    assert idea_of(p, "Future")["state"] == "on" and idea_of(p, "Future")["reason"] is None
    # Moved to the first moment allowed (the pattern is in at 1.5 s), on its
    # own track 0 (nothing else is there), keeping its word.
    assert (fut["settle"], fut["from"], fut["lane"], fut["word"], fut["source"]) == (1.5, 1.233, 0, "Future", "user")
    assert fut["shifted"] == {"at": 0.13, "said": 0.2, "why": "entrance"}
    note = "'Future' moved to 1.5 s, after the pattern comes in"
    assert note in fut["notes"] and note in p.warnings
    # Held as long as an icon off its word is (its word is long said).
    assert fut["hold"] == pytest.approx(I.HOLD_DEFAULT, abs=1e-3)
    # Generate freezes it (and only the render's fields: no track).
    (r,) = p.resolved()
    assert r["glyph"]["id"] == "rocket-launch" and r["settle_sec"] == 1.5
    assert "lane" not in r and "shifted" not in r


def test_show_it_anyway_puts_the_future_card_on_its_word_timed_by_hand():
    w = future_words()
    p = I.plan(icon_el(added=[FUTURE]), ctx_for(w, director_of()))
    at = item(p, "Future")["shifted"]["at"]
    anyway = I.plan(icon_el(added=[FUTURE], tweaks={"rocket-launch": {"off_word": True, "sec": at}}),
                    ctx_for(w, director_of()))
    fut = item(anyway, "Future")
    # As early as it can come in (it starts at the scene's first frame), said.
    assert (fut["from"], fut["settle"], fut["off_word"], fut["shifted"]) == (0.0, round(I.IN_F / FPS, 3), True, None)
    assert fut["notes"] == ["'Future' shows in the opening half second, where you put it",
                            "'Future' shows while the pattern comes in, where you put it"]


def test_an_ai_pick_said_in_the_entrance_is_still_left_out_and_says_why():
    w = future_words()
    fw = next(x["w"] for x in w["words"] if x["text"] == "Future")
    d = director_of({"id": "i0", "cand_id": None, "source": "ai", "concept": "future", "quote": "Future", "w": fw,
                     "w_end": fw, "word": "Future", "s": 0.2, "end": 0.4, "mentions": [], "table_mentions": [],
                     "icon": "rocket-launch", "label": "Future", "purpose": "Why", "priority": 5, "strong": True,
                     "verdict": "keep", "reason": "", "list_span": False, "auto": True})
    p = I.plan(icon_el(), ctx_for(w, d))
    assert shown(p) == []
    fut = idea_of(p, "Future")
    assert (fut["state"], fut["reason"]) == ("zone", I.IN_ENTRANCE), "a zone, not 'No room'"
    assert fut["why"]["by"] == "room"
    # Added by the user, the same idea shows.
    added = I.plan(icon_el(added=[FUTURE]), ctx_for(w, d))
    assert shown(added) == ["Future"] and item(added, "Future")["shifted"]["why"] == "entrance"


# ---------------------------------------------------------------------------
# Repros B2, B3 and three at once: tracks
# ---------------------------------------------------------------------------

def test_two_of_the_users_said_0_74_s_apart_both_show_on_two_tracks_in_two_places():
    w = words(815)
    tr = track()
    p = I.plan(icon_el(added=[FREE, PEER]), ctx_for(w, judged(w, {}), track=tr))
    # Before: "No room right after 'Free'" for Peer.
    assert shown(p) == ["Free", "Peer"]
    free, peer = item(p, "Free"), item(p, "Peer")
    assert (free["lane"], peer["lane"]) == (0, 1)
    assert (peer["word"], peer["shifted"]) == ("peer", None), "on its own word"
    assert _together(free, peer), "on screen together"
    assert not _hits(free["box"], peer["box"]), "never in one place"
    for it in (free, peer):
        assert tr.feasible(it["box"], it["from"] - I.WINDOW_PAD, it["to"] + I.WINDOW_PAD)
    assert idea_of(p, "Peer")["state"] == "on"


def test_one_timed_by_hand_over_another_of_theirs_never_takes_it_off():
    w = words(815)
    p = I.plan(icon_el(added=[FREE, PEER], tweaks={PEER["glyph"]["id"]: {"off_word": True, "sec": 3.6}}),
               ctx_for(w, judged(w, {}), track=track()))
    # Before: Free was left out, "No room right before 'Peer'".
    assert shown(p) == ["Free", "Peer"]
    free, peer = item(p, "Free"), item(p, "Peer")
    assert peer["settle"] == 3.6 and peer["off_word"] and peer["lane"] == 0
    assert free["word"] == "tuition" and free["lane"] == 1 and not _hits(free["box"], peer["box"])


def test_three_of_the_users_said_together_go_on_tracks_0_1_and_2():
    w = words(815)
    tr = track()
    p = I.plan(icon_el(added=[TUITION, PEER, LEARN]), ctx_for(w, judged(w, {}), track=tr))
    assert sorted(shown(p)) == ["Learning", "Peer", "Tuition"]
    lanes = {it["label"]: it["lane"] for it in p.public()["items"]}
    assert lanes == {"Tuition": 0, "Peer": 1, "Learning": 2}
    items = p.public()["items"]
    assert all(_together(a, b) for a in items for b in items), "all three on screen at 4.0 s"
    assert not any(_hits(a["box"], b["box"]) for i, a in enumerate(items) for b in items[i + 1:])
    # No two on one track overlap.
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            assert a["lane"] != b["lane"] or not _together(a, b)


def test_a_track_the_user_picked_is_kept_and_bumped_when_taken():
    w = words(815)
    peer = PEER["glyph"]["id"]
    learn = LEARN["glyph"]["id"]
    ctx = ctx_for(w, judged(w, {}), track=track())
    # Said far apart, Peer would sit on track 0; put on track 1, it stays
    # there (and is still on its word).
    alone = I.plan(icon_el(added=[PEER], lanes={peer: 1}), ctx)
    assert item(alone, "Peer")["lane"] == 1 and item(alone, "Peer")["word"] == "peer"
    # Two put on track 1 at once: the second is bumped to the next free one.
    both = I.plan(icon_el(added=[TUITION, PEER, LEARN], lanes={peer: 1, learn: 1}), ctx)
    lanes = {it["label"]: it["lane"] for it in both.public()["items"]}
    assert lanes["Peer"] == 1 and lanes["Learning"] == 2 and lanes["Tuition"] == 0
    # A track with nothing above it closes up (no empty track between).
    high = I.plan(icon_el(added=[PEER], lanes={peer: 5}), ctx)
    assert item(high, "Peer")["lane"] == 1


def test_an_ai_pick_put_on_a_track_is_the_users_and_shows_there():
    w = words(815)
    base = I.plan(icon_el(), ctx_for(w, judged(w, FREE_CODE), track=track()))
    assert shown(base) == ["Free", "Code"] and item(base, "Code")["settle"] == 4.867
    p = I.plan(icon_el(lanes={"code": 1}), ctx_for(w, judged(w, FREE_CODE), track=track()))
    code = item(p, "Code")
    # On its own track at its own best time on its word (no longer pushed
    # later by Free's clear air)...
    assert (code["lane"], code["word"], code["settle"]) == (1, "coding", 4.6)
    # ...and, the user's now, the AI's own picks give way to it, and say so
    # (track 0 itself has room: not "No room right before 'Code'").
    assert "Free" not in shown(p) and idea_of(p, "Free")["reason"] == I.GIVE_WAY.format(label="Code")
    assert idea_of(p, "Free")["state"] == "no_room"


def test_the_ais_picks_only_ever_go_on_track_0_and_never_overlap_anything():
    w = words(815)
    cases = [
        icon_el(),
        icon_el(added=[TUITION, PEER]),
        icon_el(added=[PEER], tweaks={PEER["glyph"]["id"]: {"off_word": True, "sec": 5.0, "hold_sec": 2.0}}),
        icon_el(added=[TUITION, PEER, LEARN]),
        icon_el(added=[PEER], lanes={PEER["glyph"]["id"]: 2}),
    ]
    seen_ai = 0
    for el in cases:
        p = I.plan(el, ctx_for(w, judged(w, FREE_CODE), track=track()))
        items = p.public()["items"]
        mine = {a["glyph"]["id"] for a in el.props.get("added") or []} | set(el.props.get("tweaks") or {}) | \
            set(el.props.get("lanes") or {})
        for it in items:
            if it["glyph"]["id"] in mine:
                continue
            seen_ai += 1
            assert it["source"] in ("ai", "keyword") and it["lane"] == 0, it["label"]
            for other in items:
                if other is not it:
                    assert not _together(it, other), (it["label"], other["label"])
    assert seen_ai >= 3, "the AI showed some of its own"


def test_on_track_0_the_users_still_keep_the_clear_air_and_their_words():
    """Nothing changes where the user's fit on track 0: the owner's 815
    example and the user's Code in place of the AI's (test_icons_plan)."""
    w = words(815)
    minor = {**FREE_CODE, "code": {"priority": 3, "label": "Code"}}
    d = judged(w, {k: v for k, v in minor.items() if k != "puzzle-piece"})
    add_code = icon_el(added=[{"glyph": {"source": "library", "id": "code"}, "label": "Code"}])
    p = I.plan(add_code, ctx_for(w, d))
    assert shown(p) == ["Free", "Code"] and [it["lane"] for it in p.public()["items"]] == [0, 0]
    assert all(it["shifted"] is None for it in p.public()["items"])


# ---------------------------------------------------------------------------
# Track 0 never moves one of the user's off its word
# ---------------------------------------------------------------------------

CODE_AT = {"glyph": {"source": "library", "id": "code"}, "label": "Code", "at": 1.8}
FREE_AT = {**FREE, "at": 2.2}


def code_twice() -> dict:
    """'code' said at 1.81 s and again at 8.18 s, 'free' at 2.23 s (12 s)."""
    return line_words("so today we learn code free with friends and many other words go on for a while "
                      "then we write code again at the end of this long line ok", 12.0)


def free_pick(w: dict) -> dict:
    return director_of(idea(w, "piggy-bank", [wi(w, "free")], priority=5, label="Free"))


def test_an_ai_pick_gives_way_rather_than_push_one_of_the_users_off_its_word(monkeypatch):
    """Repro (a): the user's Code shows at 1.73 s; the AI's Free (said at
    2.23 s) only fits by moving Code to its second mention (8.1 s). The AI's
    pick gives way: Code stays at the moment it was added, and Free says
    why it is not shown."""
    w = code_twice()
    alone = item(I.plan(icon_el(added=[CODE_AT]), ctx_for(w, director_of(), track=track())), "Code")
    p = I.plan(icon_el(added=[CODE_AT]), ctx_for(w, free_pick(w), track=track()))
    assert shown(p) == ["Code"]
    code = item(p, "Code")
    assert (code["settle"], code["lane"], code["w"]) == (alone["settle"], 0, wi(w, "code"))
    assert idea_of(p, "Free")["state"] == "no_room"
    # A plan these rules changed keys apart from one made before them.
    before = p.timing_key
    monkeypatch.setattr(I, "TRACK_RULES", I.TRACK_RULES + 1)
    assert I.plan(icon_el(added=[CODE_AT]), ctx_for(w, free_pick(w), track=track())).timing_key != before


def test_a_later_add_goes_on_a_new_track_rather_than_move_an_earlier_one():
    """Repro (b): Code added first (1.73 s), then Free while Code is on
    screen: Free goes on a new track at its own word; Code never jumps to
    its other mention (owner decision 6)."""
    w = code_twice()
    p = I.plan(icon_el(added=[CODE_AT, FREE_AT]), ctx_for(w, director_of(), track=track()))
    code, free = item(p, "Code"), item(p, "Free")
    assert (code["settle"], code["lane"], code["w"]) == (1.733, 0, wi(w, "code"))
    assert free["lane"] == 1 and free["w"] == wi(w, "free") and abs(free["settle"] - 2.167) < 1e-3
    assert _together(code, free)


def test_a_never_said_add_keeps_its_moment_when_an_ai_pick_comes():
    """Repro (c): Zebra, never said, added at 2.2 s, shows at 2.17 s; the
    AI's Free said at 2.23 s would push it to 3.97 s: Free gives way."""
    w = code_twice()
    alone = I.plan(icon_el(added=[{**ZEBRA, "at": 2.2}]), ctx_for(w, director_of(), track=track()))
    p = I.plan(icon_el(added=[{**ZEBRA, "at": 2.2}]), ctx_for(w, free_pick(w), track=track()))
    assert item(p, "Zebra")["settle"] == item(alone, "Zebra")["settle"] == 2.167
    assert shown(p) == ["Zebra"] and idea_of(p, "Free")["state"] == "no_room"


def test_the_users_keep_their_words_whatever_is_added_after_them():
    """Each add in turn, with and without the AI's pick: an icon of the
    user's never leaves the word (or moment) it showed at before the next
    one came."""
    w = code_twice()
    adds = [CODE_AT, FREE_AT, {**ZEBRA, "at": 5.0}]
    for d in (director_of(), free_pick(w)):
        for k in range(2, len(adds) + 1):
            before = {it["label"]: it for it in I.plan(icon_el(added=adds[:k - 1]),
                                                        ctx_for(w, d, track=track())).public()["items"]}
            after = {it["label"]: it for it in I.plan(icon_el(added=adds[:k]),
                                                       ctx_for(w, d, track=track())).public()["items"]}
            for label, it in before.items():
                if it["source"] != "user":
                    continue
                assert after[label]["w"] == it["w"], (k, label)
                assert abs(after[label]["settle"] - it["settle"]) <= I.TOL_LATE + 1e-6, (k, label)


@pytest.mark.parametrize("touch", [{"lanes": {"code": 1}},
                                   {"tweaks": {"code": {"off_word": True, "sec": 6.0}}}])
def test_five_adds_always_show_past_an_ai_pick_the_user_moved_or_timed(touch):
    """At the 5-icon cap, an AI pick the user only moved to a track or timed
    by hand gives way to their adds: every add shows, and the AI's says the
    limit."""
    w = words(815)
    d = judged(w, FREE_CODE)
    adds = [{"glyph": {"source": "custom", "id": f"c-0000000000a{k}"}, "label": f"Thing{k}"} for k in range(5)]
    p = I.plan(icon_el(added=adds, **touch), ctx_for(w, d, track=track()))
    assert sorted(shown(p)) == [f"Thing{k}" for k in range(5)]
    assert idea_of(p, "Code")["state"] == "limit"
    assert idea_of(p, "Code")["reason"] == I.OVER_MAX.format(n=I.ITEMS_MAX)
    # With room left (four adds) the moved or timed pick still shows.
    p4 = I.plan(icon_el(added=adds[:4], **touch), ctx_for(w, d, track=track()))
    assert {f"Thing{k}" for k in range(4)} | {"Code"} <= set(shown(p4))


def test_a_shift_out_of_a_zone_is_the_nearest_allowed_time_behind_the_speaker():
    """'Future' said at 0.2 s, inside the entrance, while the speaker covers
    every spot from 1.5 s to 9 s: it shows at 1.5 s (the nearest time
    allowed) behind the speaker, said, so the note's reason (the pattern
    coming in) is true; never 10 s on, in another sentence."""
    w = said_at(line_words("future starts here at forty two where you learn to code with real projects and "
                           "peers every day of the week", 12.0), "future", 0.2)
    m = _masks(26)
    m[3:19] = 255
    p = I.plan(icon_el(added=[FUTURE]), ctx_for(w, director_of(), track=track(26, masks=m)))
    fut = item(p, "Future")
    assert fut["settle"] == 1.5 and fut["covered"] == "speaker"
    assert fut["shifted"] == {"at": 0.13, "said": 0.2, "why": "entrance"}
    assert "'Future' moved to 1.5 s, after the pattern comes in" in fut["notes"]
    assert any("behind the speaker" in n for n in fut["notes"])
    # A clear spot within reach of the nearest time is still taken.
    m2 = _masks(26)
    m2[1:2] = 255                                          # the speaker in the way at 0.5 s only
    near = item(I.plan(icon_el(added=[FUTURE]), ctx_for(w, director_of(), track=track(26, masks=m2))), "Future")
    assert 1.5 < near["settle"] <= 1.5 + I.SHIFT_REACH + 1e-6 and near["covered"] is None


# ---------------------------------------------------------------------------
# Never said: at the playhead
# ---------------------------------------------------------------------------

def test_a_never_said_add_lands_nearest_the_moment_it_was_added_at():
    w = words(815)
    d = judged(w, {})
    first = I.plan(icon_el(added=[ZEBRA]), ctx_for(w, d))
    # No moment: the earliest clear time, as always (after the entrance).
    assert item(first, "Zebra")["settle"] == round(47 / FPS, 3) and item(first, "Zebra")["w"] is None
    at5 = I.plan(icon_el(added=[{**ZEBRA, "at": 5.0}]), ctx_for(w, d))
    assert abs(item(at5, "Zebra")["settle"] - 5.0) <= 2 / FPS
    # A moment where none may settle: the nearest time allowed.
    early = I.plan(icon_el(added=[{**ZEBRA, "at": 0.1}]), ctx_for(w, d))
    assert item(early, "Zebra")["settle"] == round(47 / FPS, 3)
    late = I.plan(icon_el(added=[{**ZEBRA, "at": 7.9}]), ctx_for(w, d))
    z = item(late, "Zebra")
    assert z["to"] <= w["duration"] - I.TAIL_CLEAR + 1e-6 and z["settle"] > 5.5
    # Only an add made with one carries it (the key of every other holds).
    assert "at" not in rc.IconAdd.model_validate(ZEBRA).model_dump(mode="json")
    assert rc.IconAdd.model_validate({**ZEBRA, "at": 5.0}).model_dump(mode="json")["at"] == 5.0


# ---------------------------------------------------------------------------
# Spots and too short a scene: shown, said
# ---------------------------------------------------------------------------

def test_one_of_the_users_with_no_clear_spot_shows_behind_the_speaker_and_says_so():
    w = words(815)
    n = 17
    everywhere = np.full((n, H_ // 8, W_ // 8), 255, np.uint8)         # the speaker over the whole frame
    tr = track(n, masks=everywhere)
    for face in ("shift", "reject"):
        p = I.plan(icon_el(added=[PEER]), ctx_for(w, judged(w, FREE_CODE), track=tr, face=face))
        assert shown(p) == ["Peer"], face
        peer = item(p, "Peer")
        assert peer["covered"] in ("speaker", "face") and peer["clear"] is None, face
        assert any(n_.startswith("'Peer' is ") and "behind the speaker" in n_ for n_ in peer["notes"]), face
        assert any(n_.startswith("'Peer' is ") for n_ in p.warnings), face
        # The AI's own picks are left out there, and say why (Projects, said
        # well after Peer: no spot; Free, said with it: no room).
        assert idea_of(p, "Projects")["state"] == "no_spot", face
        assert idea_of(p, "Projects")["reason"].startswith("No clear spot"), face
        assert idea_of(p, "Free")["state"] == "no_room", face
    # "warn" keeps every icon where it is, and says so (as always).
    p = I.plan(icon_el(added=[PEER]), ctx_for(w, judged(w, FREE_CODE), track=tr, face="warn"))
    assert "Peer" in shown(p) and any(n_.startswith("'Peer' is partly behind") for n_ in p.warnings)


def test_one_of_the_users_shows_on_a_spot_the_ai_vetoed_when_it_is_the_only_clear_one():
    w = words(815)
    tr = track()
    spots = tr.spots
    n_of = {s["id"]: k for k, s in enumerate(spots, 1)}
    room = {"spots": [{"n": n_of[s["id"]], "verdict": "avoid", "why": "a sign"} for s in spots],
            "home": 0, "home_why": "", "_spots": spots}
    p = I.plan(icon_el(added=[PEER]), ctx_for(w, judged(w, FREE_CODE, room=room), track=tr))
    assert shown(p) == ["Peer"]
    assert idea_of(p, "Projects")["reason"] == I.NO_SPOT_AI, "the AI's own picks keep its vetoes"
    peer = item(p, "Peer")
    assert tr.feasible(peer["box"], peer["from"] - I.WINDOW_PAD, peer["to"] + I.WINDOW_PAD), "a clear spot"


def test_a_scene_too_short_for_any_clear_time_shows_it_on_its_word_as_if_timed_by_hand():
    w = said_at(line_words("Code is fun", 2.0), "code", 0.5)
    p = I.plan(icon_el(added=[{"glyph": {"source": "library", "id": "code"}, "label": "Code"}]),
               ctx_for(w, director_of()))
    assert shown(p) == ["Code"]
    code = item(p, "Code")
    assert code["word"] == "Code" and code["settle"] == round(round((0.5 - 2 / FPS) * FPS) / FPS, 3)
    assert code["to"] <= 2.0 + 1e-6
    assert code["notes"] == [I.W_NO_CLEAR_TIME.format(label="Code")]


def test_only_the_scenes_limit_leaves_one_of_the_users_out_and_says_so():
    s = I.Scheduler(duration=20.0, enters=False)
    cues = []
    for k in range(I.ITEMS_MAX + 1):
        at = 2.0 + 0.4 * k                                    # all said together
        cues.append(I.Cue(key=f"u{k}", glyph={"source": "library", "id": f"u{k}"}, label=f"U{k}", tier=1,
                          source="user", rank=(k,), anchors=[(0, k, at, at + 0.3)]))
    slots, drops = s.run(cues)
    assert len(slots) == I.ITEMS_MAX and drops == {"u5": I.OVER_MAX.format(n=I.ITEMS_MAX)}
    assert I._state(drops["u5"]) == "limit"
    # Every one of the rest on its word, each track free of overlaps.
    for a in slots:
        assert a.anchor is not None and abs(a.settle_f - (a.anchor[2] * FPS - I.LAND_F)) <= I.TOL_LATE * FPS + 1
        for b in slots:
            if a is not b and a.lane == b.lane:
                assert a.to_f <= b.from_f or b.to_f <= a.from_f
    assert sorted({x.lane for x in slots}) == list(range(len({x.lane for x in slots}))), "no empty track"


# ---------------------------------------------------------------------------
# The keys, the render, the states, the order
# ---------------------------------------------------------------------------

def test_the_timing_key_moves_only_for_a_plan_the_tracks_changed(monkeypatch):
    w815 = words(815)
    fw = future_words()
    plans = {
        "the AI's": lambda: I.plan(icon_el(), ctx_for(w815, judged(w815, FREE_CODE), track=track())),
        "theirs on track 0": lambda: I.plan(icon_el(added=[{"glyph": {"source": "library", "id": "code"},
                                                                "label": "Code"}]),
                                            ctx_for(w815, judged(w815, FREE_CODE), track=track())),
        "two by hand": lambda: I.plan(icon_el(tweaks={"code": {"off_word": True, "sec": 2.0}}),
                                      ctx_for(w815, judged(w815, FREE_CODE), track=track())),
        "moved out of a zone": lambda: I.plan(icon_el(added=[FUTURE]), ctx_for(fw, director_of())),
        "another track": lambda: I.plan(icon_el(added=[FREE, PEER]), ctx_for(w815, judged(w815, {}), track=track())),
    }
    before = {k: f() for k, f in plans.items()}
    monkeypatch.setattr(I, "TRACK_RULES", I.TRACK_RULES + 1)
    after = {k: f() for k, f in plans.items()}
    moved = [k for k in plans if before[k].timing_key != after[k].timing_key]
    assert moved == ["moved out of a zone", "another track"]
    assert [k for k in plans if before[k].key() != after[k].key()] == moved
    # The pinned plans of test_icons_plan hold (the AI's alone, unchanged).
    assert before["the AI's"].timing_key == "17616569746b1ec0"


def test_a_track_and_a_moment_are_in_the_key_only_when_set():
    w = words(815)
    c = ctx_for(w, judged(w, FREE_CODE), track=track())
    plain = I.plan(icon_el(added=[ZEBRA]), c)
    assert plain.timing_key == I.plan(icon_el(added=[ZEBRA], lanes={}), c).timing_key
    assert plain.timing_key != I.plan(icon_el(added=[{**ZEBRA, "at": 5.0}]), c).timing_key
    assert plain.timing_key != I.plan(icon_el(added=[ZEBRA], lanes={ZEBRA["glyph"]["id"]: 1}), c).timing_key


def test_the_frozen_render_list_has_no_track():
    w = words(815)
    p = I.plan(icon_el(added=[TUITION, PEER, LEARN]), ctx_for(w, judged(w, {}), track=track()))
    assert {it["lane"] for it in p.public()["items"]} == {0, 1, 2}
    for r in p.resolved():
        assert "lane" not in r and "shifted" not in r
        rc.IconResolved.model_validate(r)
    recipe = rc.normalize({"elements": [{"id": "icons", "kind": "icon",
                                         "props": {"added": [TUITION, PEER, LEARN]}}]})
    frozen = rc.icon_props(I.materialise(recipe, p).elements[0]).resolved
    assert len(frozen) == 3 and all("lane" not in r.model_dump(mode="json") for r in frozen)


def test_the_states_say_why_not_no_room_everywhere():
    assert I._state(I.NO_ROOM_AFTER.format(label="Free")) == "no_room"
    assert I._state(I.NO_ROOM_BEFORE.format(label="Free")) == "no_room"
    assert I._state(I.IN_ENTRANCE) == I._state(I.IN_EXIT) == I._state(I.NEAR_START) == "zone"
    assert I._state(I.NEAR_END) == "near_end"
    assert I._state(I.OVER_LIMIT.format(n=2)) == I._state(I.OVER_MAX.format(n=5)) == "limit"
    assert I._state(I.NO_SPOT.format(t="2.5 s")) == I._state(I.NO_SPOT_AI) == "no_spot"
    for state in I.DROP_STATES:
        assert I.idea_why({"source": "ai"}, state, "x")["by"] == "room"
    from app.services.rebrand_harness import director_dryrun as dr
    from app.services.rebrand_harness import icon_feedback as fb

    assert set(I.DROP_STATES) <= set(fb.OFFERED) and set(I.DROP_STATES) <= set(dr._STATE)


def test_show_this_one_first_is_track_0s_order():
    w = words(815)
    el = icon_el(added=[FREE, PEER])
    c = ctx_for(w, judged(w, {}), track=track())
    got = I.reorder(el, c, PEER["glyph"]["id"], 0)
    assert got == {"ok": False, "message": I.OWN_TRACK, "tweaks": {}}
    assert I.reorder(el, c, "piggy-bank", 0) == {"ok": True, "tweaks": {}, "message": None}


# ---------------------------------------------------------------------------
# Round 2: the live QA of run 830's first scene (2026-10-01)
# ---------------------------------------------------------------------------
#
# Its plan request, as the studio sent it: Team (the library's users-three,
# which the AI picked too, for "Peer" said at 0.0 s, inside the entrance)
# added at 0.2 s, then Idea and Brain, never said, both added at 4.0 s. The
# plan it got back: Brain on track 0 at 5.8 s with no note (R3), "Show at
# 0.0 s anyway" for Team (R4), and Team labelled the AI's pick, kept (R6).

QA_LINE = "Peer learning 42 Abu Dhabi no teachers no lectures you by building projects with your peers"
TEAM_AT = {"glyph": {"source": "library", "id": "users-three"}, "label": "Team", "at": 0.2}
IDEA_AT = {"glyph": {"source": "library", "id": "lightbulb"}, "label": "Idea", "at": 4.0}
BRAIN_AT = {"glyph": {"source": "library", "id": "brain"}, "label": "Brain", "at": 4.0}


def qa_words() -> dict:
    """The scene's line over its 8 s, "Peer" said at 0.0 s."""
    return said_at(line_words(QA_LINE, 8.0), "peer", 0.0)


def qa_director(w: dict) -> dict:
    """The AI's picks there: Peer (users-three) and Projects."""
    return director_of(idea(w, "users-three", [wi(w, "peer")], priority=5, label="Peer"),
                       idea(w, "puzzle-piece", [wi(w, "projects")], priority=4, label="Projects"))


def qa_plan(*adds: dict, **props) -> I.IconPlan:
    w = qa_words()
    return I.plan(icon_el(added=list(adds), style="circle", **props), ctx_for(w, qa_director(w), track=track()))


def test_two_never_said_adds_at_one_moment_both_show_there_on_two_tracks():
    """R3. Before: Idea at 4.0 s on track 0, Brain after it at 5.8 s on track
    0 too, with no note. Now both at 4.0 s: Brain on a track of its own."""
    p = qa_plan(TEAM_AT, IDEA_AT, BRAIN_AT)
    assert sorted(shown(p)) == ["Brain", "Idea", "Team"]
    idea_, brain = item(p, "Idea"), item(p, "Brain")
    for it in (idea_, brain):
        assert abs(it["settle"] - 4.0) <= 2 / FPS, (it["label"], it["settle"])
        assert (it["word"], it["shifted"], it["notes"], it["source"]) == (None, None, [], "user"), it["label"]
    assert (idea_["lane"], brain["lane"]) == (0, 1)
    assert _together(idea_, brain) and not _hits(idea_["box"], brain["box"])
    # Brain is held as long as one off its word is (it is not cut short).
    assert brain["hold"] == pytest.approx(I.HOLD_DEFAULT, abs=1e-3)
    # The AI's Projects, said where Brain still shows, gives way to it, and
    # says so (not "No room right after 'Brain'": track 0 has room there).
    proj = idea_of(p, "Projects")
    assert "Projects" not in shown(p)
    assert (proj["state"], proj["reason"]) == ("no_room", I.GIVE_WAY.format(label="Brain"))


def test_an_ai_pick_gives_way_only_when_track_0_has_room_for_it():
    """R3 (review): Idea at 5.6 s on track 0 leaves the AI's Projects (said at
    5.75 s) no room: "No room right after 'Idea'". Brain added at 5.6 s too
    goes on track 1; Projects still has no room on track 0 (Idea), so it
    does not "give way to your 'Brain'"."""
    idea_56, brain_56 = {**IDEA_AT, "at": 5.6}, {**BRAIN_AT, "at": 5.6}
    alone = qa_plan(idea_56)
    assert idea_of(alone, "Projects")["reason"] == I.NO_ROOM_AFTER.format(label="Idea")
    p = qa_plan(idea_56, brain_56)
    assert (item(p, "Idea")["lane"], item(p, "Brain")["lane"]) == (0, 1)
    proj = idea_of(p, "Projects")
    assert (proj["state"], proj["reason"]) == ("no_room", I.NO_ROOM_AFTER.format(label="Idea"))


def test_three_never_said_adds_at_one_moment_go_on_tracks_0_1_and_2():
    yak = {"glyph": {"source": "custom", "id": "c-00000000fffe"}, "label": "Yak", "at": 4.0}
    quill = {"glyph": {"source": "custom", "id": "c-00000000fffd"}, "label": "Quill", "at": 4.0}
    p = qa_plan(TEAM_AT, {**ZEBRA, "at": 4.0}, yak, quill)
    items = [it for it in p.public()["items"] if it["label"] != "Team"]
    assert {it["label"]: it["lane"] for it in items} == {"Zebra": 0, "Yak": 1, "Quill": 2}
    assert all(abs(it["settle"] - 4.0) <= 2 / FPS and it["shifted"] is None for it in items)
    assert not any(_hits(a["box"], b["box"]) for i, a in enumerate(items) for b in items[i + 1:])


def test_a_never_said_add_keeps_its_moment_over_a_later_one_on_track_0():
    """Idea added at 4.0 s and Brain at 4.5 s: Brain is not pushed past Idea
    on track 0 (to 5.8 s, as before), it shows at 4.5 s on a track of its
    own; one added where track 0 is free there still goes there."""
    p = qa_plan(TEAM_AT, IDEA_AT, {**BRAIN_AT, "at": 4.5})
    idea_, brain = item(p, "Idea"), item(p, "Brain")
    assert (idea_["lane"], brain["lane"]) == (0, 1) and abs(brain["settle"] - 4.5) <= 2 / FPS
    far = qa_plan(TEAM_AT, IDEA_AT, {**BRAIN_AT, "at": 6.4})
    assert {it["label"]: it["lane"] for it in far.public()["items"]} == {"Team": 0, "Idea": 0, "Brain": 0}
    assert abs(item(far, "Brain")["settle"] - 6.4) <= 2 / FPS
    # Adds with no moment (from before moments) take the earliest clear
    # time on track 0, after the others, as they always have.
    yak = {"glyph": {"source": "custom", "id": "c-00000000fffe"}, "label": "Yak"}
    old = qa_plan(TEAM_AT, ZEBRA, yak)
    assert [(it["label"], it["lane"]) for it in old.public()["items"]] == [("Team", 0), ("Zebra", 0), ("Yak", 0)]


def test_a_said_add_whose_word_is_busy_shows_there_on_a_track_of_its_own():
    """The same for one that is said: Free added at 2.2 s, then Code at 1.8 s
    (said at 1.81 s and again at 8.18 s). Before: Code went to its other
    mention, 8.1 s, on track 0, 6 s from where it was added, with no note.
    Now on its word at 1.7 s, on a track of its own; an add with no moment
    (from before moments) is placed as it always was."""
    w = code_twice()
    for d in (director_of(), free_pick(w)):
        p = I.plan(icon_el(added=[FREE_AT, CODE_AT]), ctx_for(w, d, track=track()))
        code, free = item(p, "Code"), item(p, "Free")
        assert (free["lane"], free["w"]) == (0, wi(w, "free"))
        assert (code["lane"], code["w"], code["shifted"]) == (1, wi(w, "code"), None)
        assert code["settle"] == 1.733 and _together(code, free)
    old = I.plan(icon_el(added=[FREE, {k: v for k, v in CODE_AT.items() if k != "at"}]),
                 ctx_for(w, director_of(), track=track()))
    assert [(it["label"], it["lane"], it["w"]) for it in old.public()["items"]] == [
        ("Free", 0, wi(w, "free")), ("Code", 0, wi(w, "code", 1))]


def test_a_never_said_add_moved_off_its_moment_says_so_with_the_way_back():
    """R3: any time shift of one of the user's carries a "moved" note. Added
    at 0.1 s (inside the entrance) it shows at 1.6 s; at 7.9 s (too near the
    end) earlier; at 5.0 s exactly there, said nothing."""
    w = words(815)
    d = judged(w, {})
    p = I.plan(icon_el(added=[{**ZEBRA, "at": 0.1}]), ctx_for(w, d))
    z = item(p, "Zebra")
    assert (z["settle"], z["lane"], z["word"]) == (round(47 / FPS, 3), 0, None)
    assert z["shifted"] == {"at": 0.1, "said": None, "asked": 0.1, "why": "entrance"}
    note = "'Zebra' moved to 1.6 s, after the pattern comes in"
    assert note in z["notes"] and note in p.warnings
    late = item(I.plan(icon_el(added=[{**ZEBRA, "at": 7.9}]), ctx_for(w, d)), "Zebra")
    assert late["shifted"] == {"at": 7.9, "said": None, "asked": 7.9, "why": "end"}
    assert any(n.endswith("so it's gone before the end") for n in late["notes"])
    at5 = item(I.plan(icon_el(added=[{**ZEBRA, "at": 5.0}]), ctx_for(w, d)), "Zebra")
    assert (at5["shifted"], at5["notes"]) == (None, [])
    # No pattern coming in: moved past the opening half second only.
    still = item(I.plan(icon_el(added=[{**ZEBRA, "at": 0.1}]), ctx_for(w, d, enters=False)), "Zebra")
    assert still["shifted"]["why"] == "start" and still["from"] >= I.HEAD_CLEAR - 1e-6
    # "Show at 0.1 s anyway": timed by hand there (as early as it can come in).
    anyway = item(I.plan(icon_el(added=[{**ZEBRA, "at": 0.1}],
                                 tweaks={ZEBRA["glyph"]["id"]: {"off_word": True, "sec": 0.1}}), ctx_for(w, d)),
                  "Zebra")
    assert (anyway["from"], anyway["off_word"], anyway["shifted"]) == (0.0, True, None)


def test_a_never_said_add_stays_at_its_moment_behind_the_speaker():
    """The speaker over every spot from 2.0 s to 5.0 s: one added at 4.0 s
    shows there, behind her, said (as one on its word does), not at 6.1 s,
    the nearest clear time (as before)."""
    w = words(815)
    m = _masks(17)
    m[4:11] = 255
    p = I.plan(icon_el(added=[{**ZEBRA, "at": 4.0}]), ctx_for(w, judged(w, {}), track=track(17, masks=m)))
    z = item(p, "Zebra")
    assert abs(z["settle"] - 4.0) <= I.TOL_LATE + 1e-6 and z["shifted"] is None
    assert z["covered"] in ("speaker", "face") and any("behind the speaker" in n for n in z["notes"])


def test_show_it_anyway_offers_the_moment_it_was_added_at():
    """R4: Team, added at 0.2 s, lands on the AI's word for it ('Peer', said
    at 0.0 s inside the entrance) and moves to 1.5 s. "Show at … anyway"
    offers 0.2 s, the moment it was added at (not 0.0 s); an add made with
    no moment still offers its word (the owner's Future card)."""
    team = item(qa_plan(TEAM_AT), "Team")
    assert (team["settle"], team["word"], team["lane"]) == (1.5, "Peer", 0)
    assert team["shifted"] == {"at": 0.0, "said": 0.0, "asked": 0.2, "why": "entrance"}
    anyway = item(qa_plan(TEAM_AT, tweaks={"users-three": {"off_word": True, "sec": team["shifted"]["asked"]}}),
                  "Team")
    assert (anyway["off_word"], anyway["shifted"], anyway["settle"]) == (True, None, round(I.IN_F / FPS, 3))
    fut = item(I.plan(icon_el(added=[FUTURE]), ctx_for(future_words(), director_of())), "Future")
    assert "asked" not in fut["shifted"]


def test_show_it_anyway_offers_the_word_when_the_moment_is_not_why_it_moved():
    """R4 (review): the add's moment is offered only when the move crossed
    it. A word the user picked is their choice, not the moment (Zebra, added
    at 4.0 s, dropped on 'Peer': "Show at 4.0 s anyway" took it off that
    word, back where it was). A moment far from the word and the zone has
    nothing to do with the move (Team at 5.0 s, its word 'Peer' at 0.0 s;
    Team at 0.2 s pinned to 'peers' near the end): the word is offered."""
    w = qa_words()
    zebra = {**ZEBRA, "at": 4.0}
    on_peer = item(qa_plan(zebra, tweaks={ZEBRA["glyph"]["id"]: {"w": wi(w, "peer"), "words_digest": w["digest"]}}),
                   "Zebra")
    assert (on_peer["word"], on_peer["settle"]) == ("Peer", 1.5)
    assert on_peer["shifted"] == {"at": 0.0, "said": 0.0, "why": "entrance"}
    far = item(qa_plan({**TEAM_AT, "at": 5.0}), "Team")
    assert (far["word"], far["settle"]) == ("Peer", 1.5)
    assert far["shifted"] == {"at": 0.0, "said": 0.0, "why": "entrance"}
    both = director_of(idea(w, "users-three", [wi(w, "peer"), wi(w, "peers")], priority=5, label="Peer"),
                       idea(w, "puzzle-piece", [wi(w, "projects")], priority=4, label="Projects"))
    end = item(I.plan(icon_el(added=[TEAM_AT], style="circle",
                              tweaks={"users-three": {"w": wi(w, "peers"), "words_digest": w["digest"]}}),
                      ctx_for(w, both, track=track())), "Team")
    assert end["word"] == "peers" and end["shifted"]["why"] == "end" and "asked" not in end["shifted"]
    # The QA's own Team (0.2 s, by its word in the entrance) still offers 0.2 s.
    assert item(qa_plan(TEAM_AT), "Team")["shifted"]["asked"] == 0.2


def test_a_library_add_the_ai_picked_too_is_the_users():
    """R6: Team from the library is the AI's Peer pick's icon. It is the
    user's add ("yours": Added by you), counted as theirs, with every
    guarantee of one: never left out, a track of its own when its moment is
    busy. ✓ Keep on the AI's pick (added as it is, with no moment) is still
    the AI's pick, kept."""
    p = qa_plan(TEAM_AT)
    team = item(p, "Team")
    assert (team["source"], team["why"]["by"]) == ("user", "you")
    assert team["why"]["text"].startswith(f"{I.WHY_YOU} · said 'Peer'") and "key idea" not in team["why"]["text"]
    assert idea_of(p, "Team")["added"] is True
    kept = item(qa_plan({"glyph": {"source": "library", "id": "users-three"}, "label": "Peer"}), "Peer")
    assert (kept["source"], kept["why"]["by"]) == ("user", "kept")
    # Its moment taken on track 0 by another of theirs added before it: a
    # track of its own, there, never left out.
    zebra = {**ZEBRA, "at": 1.5}
    both = qa_plan(zebra, TEAM_AT)
    assert {"Team", "Zebra"} <= set(shown(both))
    assert (item(both, "Zebra")["lane"], item(both, "Team")["lane"]) == (0, 1)
    assert item(both, "Team")["settle"] == 1.5 and item(both, "Team")["why"]["by"] == "you"
    # Counted as one of the user's for the scene's limit: five adds with it
    # all show, and the AI's own pick is the one over the limit.
    more = [{"glyph": {"source": "custom", "id": f"c-0000000000b{k}"}, "label": f"Thing{k}", "at": 1.0 + 1.5 * k}
            for k in range(4)]
    full = qa_plan(TEAM_AT, *more)
    assert sorted(shown(full)) == ["Team", "Thing0", "Thing1", "Thing2", "Thing3"]
    assert idea_of(full, "Projects")["state"] in ("limit", "no_room")


def test_the_timing_key_moves_only_for_a_plan_a_kept_moment_changed(monkeypatch):
    """A plan where one never said is kept at its moment on a track of its
    own (MOMENT_RULES) keys apart from one made before (which had it on
    track 0, later); every other plan keeps its key."""
    w815 = words(815)
    plans = {
        "the AI's": lambda: I.plan(icon_el(), ctx_for(w815, judged(w815, FREE_CODE), track=track())),
        "one at its moment": lambda: I.plan(icon_el(added=[{**ZEBRA, "at": 5.0}]),
                                            ctx_for(w815, judged(w815, {}), track=track())),
        "moved out of a zone": lambda: I.plan(icon_el(added=[{**ZEBRA, "at": 0.1}]),
                                              ctx_for(w815, judged(w815, {}), track=track())),
        "two at one moment": lambda: qa_plan(TEAM_AT, IDEA_AT, BRAIN_AT),
        "a said one kept on its word": lambda: I.plan(icon_el(added=[FREE_AT, CODE_AT]),
                                                      ctx_for(code_twice(), director_of(), track=track())),
        "said, in order": lambda: I.plan(icon_el(added=[CODE_AT, {**FREE_AT, "at": 6.0}]),
                                         ctx_for(code_twice(), director_of(), track=track())),
    }
    before = {k: f() for k, f in plans.items()}
    monkeypatch.setattr(I, "MOMENT_RULES", I.MOMENT_RULES + 1)
    after = {k: f() for k, f in plans.items()}
    assert [k for k in plans if before[k].timing_key != after[k].timing_key] == [
        "two at one moment", "a said one kept on its word"]
    assert before["the AI's"].timing_key == "17616569746b1ec0"


# ---------------------------------------------------------------------------
# R5: one time per icon, the planner's and the studio's (a shared fixture)
# ---------------------------------------------------------------------------

_TIMES_KEYS = ("glyph", "label", "source", "pinned", "off_word", "from", "settle", "to", "hold", "moment_t", "lane",
               "word", "w", "shifted", "notes")


def _qa(*adds: dict, **props) -> tuple[rc.Element, I.IconCtx]:
    """qa_plan's icon element and context (the plan is I.plan of them)."""
    w = qa_words()
    return icon_el(added=list(adds), style="circle", **props), ctx_for(w, qa_director(w), track=track())


def _times_plans() -> list[tuple[str, rc.Element, I.IconCtx]]:
    w815 = words(815)
    yak = {"glyph": {"source": "custom", "id": "c-00000000fffe"}, "label": "Yak", "at": 4.0}
    quill = {"glyph": {"source": "custom", "id": "c-00000000fffd"}, "label": "Quill", "at": 4.0}
    return [
        ("run 830 scene_0, the round-2 QA: Team at 0.2 s, Idea and Brain at 4.0 s",
         *_qa(TEAM_AT, IDEA_AT, BRAIN_AT)),
        ("never said, added at 0.1 s inside the entrance", icon_el(added=[{**ZEBRA, "at": 0.1}]),
         ctx_for(w815, judged(w815, {}))),
        ("the owner's Future card, added with no moment", icon_el(added=[FUTURE]),
         ctx_for(future_words(), director_of())),
        ("Keep on the AI's Peer pick", *_qa({"glyph": {"source": "library", "id": "users-three"}, "label": "Peer"})),
        ("never said, added at 4.0 s, then dropped on the word 'Peer'",
         *_qa({**ZEBRA, "at": 4.0}, tweaks={ZEBRA["glyph"]["id"]: {"w": wi(qa_words(), "peer"),
                                                                  "words_digest": qa_words()["digest"]}})),
        # The tracks a version's timeline shows them on (round 3, V1).
        ("three never said, added at one moment: tracks 0, 1 and 2", *_qa(TEAM_AT, {**ZEBRA, "at": 4.0}, yak, quill)),
        ("a said add whose word is busy: a track of its own", icon_el(added=[FREE_AT, CODE_AT]),
         ctx_for(code_twice(), director_of(), track=track())),
        ("one timed by hand over another of theirs",
         icon_el(added=[FREE, PEER], tweaks={PEER["glyph"]["id"]: {"off_word": True, "sec": 3.6}}),
         ctx_for(w815, judged(w815, {}), track=track())),
        ("two put on track 1: the second bumped to track 2",
         icon_el(added=[TUITION, PEER, LEARN], lanes={PEER["glyph"]["id"]: 1, LEARN["glyph"]["id"]: 1}),
         ctx_for(w815, judged(w815, {}), track=track())),
        ("an AI pick put on a track", icon_el(lanes={"code": 1}), ctx_for(w815, judged(w815, FREE_CODE), track=track())),
        ("the AI's own, on track 0", icon_el(), ctx_for(w815, judged(w815, FREE_CODE), track=track())),
        # The AI's pick the user made theirs keeps the AI's rank, ahead of
        # their adds (Scheduler.run): it takes its track first.
        ("an AI pick put on track 1, two never said added at its moment",
         icon_el(lanes={"code": 1}, added=[{**ZEBRA, "at": 4.6}, {**yak, "at": 4.6}]),
         ctx_for(w815, judged(w815, FREE_CODE), track=track())),
        ("the AI's Peer and an add, both pinned to the word 'Peer'",
         *_qa({**ZEBRA, "at": 4.0}, tweaks={
             gid: {"w": wi(qa_words(), "peer"), "words_digest": qa_words()["digest"]}
             for gid in (ZEBRA["glyph"]["id"], "users-three")})),
    ]


def _times_body() -> dict:
    plans = []
    for name, el, ctx in _times_plans():
        p = I.plan(el, ctx)
        items = [{**{k: it[k] for k in _TIMES_KEYS}, "why": {"by": it["why"]["by"]}}
                 for it in p.public()["items"]]
        ideas = [{k: i[k] for k in ("glyph", "label", "word", "w", "at", "state", "reason", "added")}
                 for i in p.ideas]
        # As a version holds them: the icon element's props (what the user
        # did), the icons Generate froze (props.resolved, in its order: what
        # a version's lane reads of each) and the tracks it keeps beside
        # them (options.icon_lanes).
        dump = rc.icon_props(el).model_dump(mode="json", exclude={"resolved"})
        props = {k: dump[k] for k in ("added", "tweaks", "lanes") if dump[k]}
        frozen = [{k: r[k] for k in ("glyph", "label", "from_sec", "settle_sec", "to_sec")} for r in p.resolved()]
        plans.append({"name": name, "items": items, "ideas": ideas, "props": props, "frozen": frozen,
                      "icon_lanes": I.lanes_of(p)})
    return {"about": "The icon plans of tests/test_icons_lanes.py (elements/icon.plan, the real planner) the "
                     "studio says the times of: frontend/scripts/check-rebrand-icon-times.mjs checks that the "
                     "list, the Moments chips, the notes and the block's settle mark all say one time per icon "
                     "(its settle), what 'Show at ... anyway' offers, who each one is, the time Suggested says "
                     "for an idea, and that a version's read-only timeline has its icons on the tracks its "
                     "draft had. Written with REBRAND_ICON_TIMES=write, asserted otherwise.", "plans": plans}


def test_one_time_per_icon_the_planner_and_the_studio_agree():
    """R5: the list said "Moved to 1.5 s", the Moments chip 1.9 s and the
    block started at 1.23 s for one icon (Team). The time said is its
    settle everywhere: the planner's notes say it (here), and the studio's
    list, chips, notes and the block's settle mark (the check, on the same
    plans)."""
    body = _times_body()
    for plan_ in body["plans"]:
        for it in plan_["items"]:
            assert it["from"] < it["settle"] < it["to"] and it["moment_t"] > it["settle"]
            if it["shifted"] is not None:
                assert any(n.startswith(f"'{it['label']}' moved to {it['settle']:.1f} s, ") for n in it["notes"])
    if os.environ.get("REBRAND_ICON_TIMES") == "write":
        TIMES_FIX.write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert json.loads(TIMES_FIX.read_text(encoding="utf-8")) == body
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    if tuple(int(x) for x in version.lstrip("v").split(".")[:2]) < (22, 6):
        pytest.skip(f"node {version} cannot strip types")
    out = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "scripts/check-rebrand-icon-times.mjs"],
                         capture_output=True, text=True, timeout=120, cwd=REPO / "frontend")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cases agree" in out.stdout and "FAIL" not in out.stdout
