"""TH v2, round 3 (the live re-test of run 830).

U1: clips made before clips recorded their still (video_source; run 830's
clips 351/354) read as out of date after any Undo, even when the still put
back is byte for byte the one the clip was made from: the user was asked to
pay ~$1.40 a clip and Rebrand was locked. Fixed forward with no migration:
when a still is about to be replaced and its clip records nothing and is up
to date, the still being replaced is recorded as the clip's source first, so
an Undo back to it keeps the clip up to date. A clip that really is out of
date stays so.

U2: a text swap is judged line by line. Run 830's card 835 (card 825 with
the script's words) lost BOTH swaps because its shorter subtitle failed the
fit: for a short line the zone's padding was exactly the mask's feathered
edge, so its safe area was the old words' box to the pixel, and the reader's
box of the new line (a few thousandths taller) "ran outside". The zone now
leaves the new words room above and below (a third of a line, at least 1% of
the card) and a read-back box may run a little past it (15% of a line, no
more than keeps a letter at 90% in the feather). A group that still fails
gets one tighter (or spelling) retry on its own; then the groups that passed
are kept and only the failing line keeps the card's own words, with a
warning naming it.

U3: a card with no motion plan under the current rules (run 830's outro,
card 831) never shows an older plan as the current one: opening the run
plans it once, in the background (one Gemini Flash read), claimed first so
no poll, process or failure asks twice.

U4: a still's background work says what it is: a framing crop (free), a
re-shoot by the image model (paid), or a still made again.

No database and no model calls: the in-memory FakePg and fakes of
test_th_v2_scene_stills (an unexpected model call fails).
"""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.services import card_animation as ca
from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp
from test_th_v2_bookend_swap import (  # noqa: F401
    LOGO, NEW, OLD_HEAD, URL, W, H, _card_of, _card_picture, _img, _new_run, _prev_run, _read, senv,
)
from test_th_v2_bookends import (  # noqa: F401
    ICONS, LOGO_ONLY, REAL_FETCH_RUN, _seed_card, _stills_run, benv,
)
from test_th_v2_round2 import _face_still
from test_th_v2_scene_stills import _md, _seed_old_main, _seed_scene_row, env  # noqa: F401


@pytest.fixture
def r3env(env, monkeypatch):
    """env with a fresh image-gen semaphore (bound to the test's loop)."""
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    return env


# ---------------------------------------------------------------------------
# U1: a clip that predates video_source
# ---------------------------------------------------------------------------

def _legacy_main(env, run_id: int, **md) -> str:
    """A single-scene run whose main clip (77) is up to date and records no
    still (made before 3450001). Returns its still's key."""
    env.db.add_run(run_id, _md(lock=False, main_animation_id=77, veo_animation_id=77,
                               main_video_stale=False, **md))
    _seed_old_main(env, run_id)
    md = env.db.md(run_id)
    assert "main_video_source" not in md
    return th._still_key(env.db.rows[run_id], md, 0)


def _legacy_scenes(env, run_id: int) -> None:
    """A two-scene run, both clips up to date, neither recording a still."""
    md = _md(scenes=2, lock=False)
    md["scenes"][0].update(animation_id=351, video_stale=False)
    md["scenes"][1].update(keyframe_id=555, animation_id=354, video_stale=False)
    env.db.add_run(run_id, md)
    _seed_old_main(env, run_id)
    _seed_scene_row(env, run_id, 1, 555)
    for s in env.db.md(run_id)["scenes"]:
        assert "video_source" not in s


def test_a_legacy_clip_is_fresh_again_after_refine_and_undo(r3env):
    key = _legacy_main(r3env, 301)
    asyncio.run(th.refine_still(301, "main", "warmer light"))
    md = r3env.db.md(301)
    assert md["main_video_stale"] is True, "a new still: the clip is out of date"
    assert md["main_video_source"] == key, "the still the clip was made from, recorded first"
    asyncio.run(th.undo_still(301, "main"))
    assert r3env.db.md(301)["main_video_stale"] is False, "the clip's own still is back"


def test_a_legacy_scene_clip_is_fresh_again_after_refine_and_undo(r3env):
    _legacy_scenes(r3env, 302)
    asyncio.run(th.refine_still(302, "scene_1", "brighter"))
    sc = r3env.db.md(302)["scenes"][1]
    assert sc["video_stale"] is True and sc["video_source"] == "scene:555"
    asyncio.run(th.undo_still(302, "scene_1"))
    sc = r3env.db.md(302)["scenes"][1]
    assert sc["keyframe_id"] == 555 and sc["video_stale"] is False
    # The scene left alone records nothing new.
    assert "video_source" not in r3env.db.md(302)["scenes"][0]


def test_a_legacy_scene_0_clip_on_a_multi_scene_run(r3env):
    _legacy_scenes(r3env, 303)
    key = th._still_key(r3env.db.rows[303], r3env.db.md(303), 0)
    asyncio.run(th.refine_still(303, "main", "warmer light"))
    s0 = r3env.db.md(303)["scenes"][0]
    assert s0["video_stale"] is True and s0["video_source"] == key
    assert "main_video_source" not in r3env.db.md(303), "a multi-scene clip records on its scene"
    asyncio.run(th.undo_still(303, "main"))
    assert r3env.db.md(303)["scenes"][0]["video_stale"] is False


def test_a_legacy_clip_after_a_framing_crop_and_undo(r3env):
    r3env.db.add_run(304, _md(lock=False, framing="close", main_framing_used="above_hip",
                              main_keyframe_generating=True, main_animation_id=77,
                              main_video_stale=False))
    old = _face_still(r3env, 304)
    key = th._still_key(r3env.db.rows[304], r3env.db.md(304), 0)
    asyncio.run(th._run_keyframe_regen(304, [{"scene_index": 0, "reshoot": True}]))
    assert r3env.edit["calls"] == [] and r3env.gen["calls"] == [], "a crop: no model"
    md = r3env.db.md(304)
    assert md["main_video_stale"] is True and md["main_video_source"] == key
    asyncio.run(th.undo_still(304, "main"))
    assert r3env.db.rows[304]["local_path"] == str(old)
    assert r3env.db.md(304)["main_video_stale"] is False


def test_a_legacy_clip_after_a_regenerate_and_undo(r3env):
    _legacy_scenes(r3env, 305)
    asyncio.run(th.regenerate_still(305, "scene_1"))
    assert r3env.db.md(305)["scenes"][1]["video_source"] == "scene:555"
    asyncio.run(th.undo_still(305, "scene_1"))
    assert r3env.db.md(305)["scenes"][1]["video_stale"] is False


def test_a_legacy_clip_after_a_new_background_regen_and_undo(r3env):
    # An edit's regen ("New bg" on a still that is the photo) replaces the
    # still in the background: the clip was made from the one replaced.
    _legacy_scenes(r3env, 306)
    asyncio.run(th._regen_scene_keyframe(306, scene_index=1, keep_subject=True,
                                         has_char_ref=False, has_bg_ref=False))
    sc = r3env.db.md(306)["scenes"][1]
    assert sc["video_stale"] is True and sc["video_source"] == "scene:555"
    asyncio.run(th.undo_still(306, "scene_1"))
    assert r3env.db.md(306)["scenes"][1]["video_stale"] is False


def test_a_new_background_for_scene_0_of_a_multi_scene_run_marks_its_clip(r3env):
    # Scene 0's clip on a multi-scene run lives on scenes[0]: a background
    # regen of its still used to mark only the one-scene key, so the clip
    # read up to date with the old still.
    _legacy_scenes(r3env, 312)
    key = th._still_key(r3env.db.rows[312], r3env.db.md(312), 0)
    asyncio.run(th._regen_scene_keyframe(312, scene_index=0, keep_subject=True,
                                         has_char_ref=False, has_bg_ref=False))
    s0 = r3env.db.md(312)["scenes"][0]
    assert s0["video_stale"] is True and s0["video_source"] == key
    asyncio.run(th.undo_still(312, "main"))
    assert r3env.db.md(312)["scenes"][0]["video_stale"] is False
    # A scene with no clip gets no stale mark.
    md = _md(scenes=2, lock=False)
    r3env.db.add_run(313, md)
    _seed_old_main(r3env, 313)
    asyncio.run(th._regen_scene_keyframe(313, scene_index=0, keep_subject=True,
                                         has_char_ref=False, has_bg_ref=False))
    assert r3env.db.md(313)["scenes"][0]["video_stale"] is False


def test_a_legacy_clip_that_is_really_out_of_date_stays_so(r3env):
    # Out of date already (its words changed before 3450001): nothing is
    # recorded, and an Undo never makes it fresh.
    _legacy_main(r3env, 307)
    r3env.db.md(307)["main_video_stale"] = True
    asyncio.run(th.refine_still(307, "main", "warmer light"))
    assert r3env.db.md(307).get("main_video_source") is None
    asyncio.run(th.undo_still(307, "main"))
    assert r3env.db.md(307)["main_video_stale"] is True


def test_a_legacy_clip_an_edit_made_stale_stays_stale(r3env):
    _legacy_main(r3env, 308)
    asyncio.run(th.edit_run(run_id=308, video_quality="quality"))
    assert r3env.db.md(308)["main_video_stale"] is True
    asyncio.run(th.refine_still(308, "main", "warmer light"))
    asyncio.run(th.undo_still(308, "main"))
    assert r3env.db.md(308)["main_video_stale"] is True, "the clip is still on the old tier"


def test_a_legacy_clip_two_changes_one_undo_stays_stale(r3env):
    key = _legacy_main(r3env, 309)
    asyncio.run(th.refine_still(309, "main", "warmer"))
    asyncio.run(th.refine_still(309, "main", "cooler"))
    assert r3env.db.md(309)["main_video_source"] == key, "recorded once, from the first change"
    asyncio.run(th.undo_still(309, "main"))
    assert r3env.db.md(309)["main_video_stale"] is True, "the still back is not the clip's"
    asyncio.run(th.undo_still(309, "main"))
    assert r3env.db.md(309)["main_video_stale"] is False


def test_no_clip_records_nothing(r3env):
    r3env.db.add_run(310, _md(lock=False))
    _seed_old_main(r3env, 310)
    asyncio.run(th.refine_still(310, "main", "warmer light"))
    assert r3env.db.md(310).get("main_video_source") is None


def test_a_recorded_source_is_never_overwritten(r3env):
    r3env.db.add_run(311, _md(lock=False, main_animation_id=77, main_video_stale=False,
                              main_video_source="main:older|"))
    _seed_old_main(r3env, 311)
    asyncio.run(th.refine_still(311, "main", "warmer light"))
    assert r3env.db.md(311)["main_video_source"] == "main:older|"


def test_the_source_to_record():
    md = {"main_animation_id": 77, "main_video_stale": False}
    assert th._clip_source_to_record(md, 0, "main:a|") == "main:a|"
    assert th._clip_source_to_record({**md, "veo_animation_id": 77, "main_animation_id": None},
                                     0, "main:a|") == "main:a|", "a legacy clip id"
    assert th._clip_source_to_record({**md, "main_video_stale": True}, 0, "main:a|") is None
    assert th._clip_source_to_record({**md, "main_video_source": "main:b|"}, 0, "main:a|") is None
    assert th._clip_source_to_record({}, 0, "main:a|") is None, "no clip"
    assert th._clip_source_to_record(md, 0, None) is None, "no still replaced"
    multi = {"scene_count": 2, "scenes": [{"animation_id": 1, "video_stale": False},
                                          {"animation_id": 2, "video_stale": True}]}
    assert th._clip_source_to_record(multi, 0, "main:a|") == "main:a|"
    assert th._clip_source_to_record(multi, 1, "scene:5") is None, "out of date"
    # The scene as read under the lock wins over the one in `md`.
    assert th._clip_source_to_record(multi, 1, "scene:5", scene={"animation_id": 2}) == "scene:5"


# ---------------------------------------------------------------------------
# U2: a swap judged line by line, and a calibrated fit (card 825 -> 835)
# ---------------------------------------------------------------------------

# Card 835 (1026 x 1824) is card 825 (1024 x 1024) padded to 9:16; its read
# of card 825 and its geometry, as on prod (yas_generations 835).
C835 = (1026, 1824)
CONTENT_835 = (1, 400, 1025, 1424)
SRC_835 = th._card_lines_from({"lines": [
    {"text": "42 Abu Dhabi", "kind": "logo", "box_2d": [265, 404, 289, 595]},
    {"text": "DECODE YOUR", "kind": "headline", "box_2d": [376, 257, 424, 742]},
    {"text": "TECH FUTURE", "kind": "headline", "box_2d": [431, 275, 479, 724]},
    {"text": "Start your journey in tech today", "kind": "subtitle", "box_2d": [492, 286, 519, 713]},
]})
SUB_835 = "No teachers, just collaboration"


def _read_835(sub_box: list[int], head2_box: list[int] | None = None) -> list[dict]:
    """A read-back of the swapped card: the new headline in its old place,
    the new subtitle (one character shorter) at `sub_box`."""
    return th._card_lines_from({"lines": [
        {"text": "42 Abu Dhabi", "kind": "logo", "box_2d": [265, 404, 289, 595]},
        {"text": "WHY PEER", "kind": "headline", "box_2d": [377, 300, 423, 700]},
        {"text": "LEARNING WORKS", "kind": "headline", "box_2d": head2_box or [430, 262, 478, 738]},
        {"text": SUB_835, "kind": "subtitle", "box_2d": sub_box},
    ]})


def _plan_835():
    return th._text_swap_plan(SRC_835, "Why Peer Learning Works", SUB_835)


def test_run_830s_shorter_subtitle_fits_card_835():
    plan = _plan_835()
    safe = th._swap_safe_rects(C835, th._swap_zone_rects(C835, SRC_835, plan, CONTENT_835))
    # The reader's box of the new subtitle, two thousandths taller each side
    # than the old line's: the old geometry had no room at all for it.
    after = _read_835([490, 296, 521, 704])
    assert th._swap_verdict(SRC_835, plan, after)[0] is True
    assert th._swap_fits(C835, SRC_835, plan, after, safe) == (True, "the new words fit inside the text area")
    # The safe area leaves 1% of the card above and below a short line (it
    # was the old words' box, to the pixel).
    sub = th._swap_text_boxes(C835, SRC_835, plan)[1]
    assert safe[1][1] <= sub[1] - 0.01 * 1824 + 0.5 and safe[1][3] >= sub[3] + 0.01 * 1824 - 0.5
    # A real spill still fails: wider than the old words' box...
    wide = _read_835([492, 200, 519, 800])
    ok, why = th._swap_fits(C835, SRC_835, plan, wide, safe)
    assert ok is False and SUB_835 in why and "ran outside" in why
    # ...and run 830's headline over the art panel (card 832).
    spill = _read_835([492, 296, 519, 704], head2_box=[428, 202, 476, 785])
    ok, why = th._swap_fits(C835, SRC_835, plan, spill, safe)
    assert ok is False and "LEARNING WORKS" in why


def test_the_fit_tolerance_is_glyph_relative_and_keeps_letters_at_full_strength():
    plan = _plan_835()
    tol = th._swap_tolerances(C835, SRC_835, plan)
    f = th._swap_feather(C835)
    _head, sub = th._swap_text_boxes(C835, SRC_835, plan)
    assert tol[1] == pytest.approx(min(0.15 * (sub[3] - sub[1]), 0.36 * f + 2))
    assert all(t <= 0.36 * f + 2 for t in tol)
    # Where a read-back box may end (the safe area plus its tolerance), the
    # paste is at least 90% strong: a letter there is never faded.
    alpha, _zone = th._text_swap_mask(C835, SRC_835, plan, CONTENT_835)
    zones = th._swap_zone_rects(C835, SRC_835, plan, CONTENT_835)
    (sx0, sy0, sx1, sy1), = th._swap_safe_rects(C835, zones[1:])
    cy = round((sy0 + sy1) / 2)
    assert alpha.getpixel((int(sx0 - tol[1]), cy)) >= 0.9 * 255
    assert alpha.getpixel((int(sx1 + tol[1]), cy)) >= 0.9 * 255
    # Beyond it, the feather: a letter would fade.
    assert alpha.getpixel((int(zones[1][0] + f), cy)) < 0.9 * 255


def test_each_group_is_judged_on_its_own():
    plan = _plan_835()
    zones = th._swap_zone_rects(C835, SRC_835, plan, CONTENT_835)
    safe = th._swap_safe_rects(C835, zones)

    def judge(after, groups=(0, 1)):
        return th._swap_judge(C835, SRC_835, plan, after, zones, safe, list(groups))

    assert judge(_read_835([490, 296, 521, 704])) == {0: None, 1: None}
    got = judge(_read_835([492, 200, 519, 800]))
    assert got[0] is None and "ran outside" in got[1], "the headline passes, the subtitle spills"
    typo = _read_835([492, 296, 519, 704])
    typo[3] = {**typo[3], "text": "No techers, just collaboration"}
    got = judge(typo)
    assert got[0] is None and "missing or misspelled" in got[1] and "teachers" in got[1]
    extra = _read_835([492, 296, 519, 704])
    extra[1] = {**extra[1], "text": "WHY PEER PEERS"}
    got = judge(extra)
    assert "should not be there" in got[0] and got[1] is None, "an extra word goes to the line it is on"
    lost = [ln for ln in _read_835([492, 296, 519, 704]) if ln["kind"] != "logo"]
    assert all("abu" in w for w in judge(lost).values()), "a kept line lost: no group can be trusted"
    assert set(judge(None).values()) == {"the swapped card could not be read back"}
    # Only the groups asked about are judged.
    assert judge(_read_835([492, 200, 519, 800]), groups=(1,)).keys() == {1}


SUB = {"text": "Start your journey in tech today", "kind": "subtitle", "box_2d": [436, 150, 466, 850]}
NEW_SUB = "No teachers, just collaboration"
HEAD_PX = (360, 470)   # inside the headline's band
SUB_PX = (360, 577)    # inside the subtitle's band


def _sub_read(head: dict | None = None, sub: dict | None = None) -> dict:
    return _read(LOGO, head or OLD_HEAD, sub or SUB, URL)


def _new_head(**kw) -> dict:
    return {**OLD_HEAD, "text": NEW.upper(), **kw}


def _new_sub(**kw) -> dict:
    return {**SUB, "text": NEW_SUB, **kw}


def _two_group_run(env, run_id: int, src_run: int, seed: int) -> tuple[Image.Image, int]:
    card = _card_picture(seed=seed)
    card.paste((30, 220, 120), (108, 558, 612, 596))   # the subtitle
    src = _prev_run(env, src_run, picture=card)
    _new_run(env, run_id)
    env.db.md(run_id)["script"]["intro_subtitle"] = NEW_SUB
    return card, src


@pytest.fixture
def edits(senv, monkeypatch):
    """The swap's image edits: each answers a solid colour of its own (the
    first (10, 20, 30), the second (200, 40, 40)) and records its directive
    and the card it was given (IMAGE 1) as it was at the call."""
    calls: list[dict] = []
    colours = [(10, 20, 30), (200, 40, 40)]

    async def render(parts, *, system_instruction, timeout, aspect_ratio=None):
        img = next(Path(v) for k, v in parts if k == "image")
        with Image.open(img) as im:
            canvas = im.convert("RGB").copy()
        calls.append({"directive": parts[-1][1], "canvas": canvas})
        buf = BytesIO()
        Image.new("RGB", (768, 1344), colours[min(len(calls), 2) - 1]).save(buf, "PNG")
        return buf.getvalue()

    monkeypatch.setattr(ca, "render_card_image", render)
    return calls


def test_a_subtitle_that_keeps_spilling_keeps_its_words_and_the_headline_is_swapped(senv, edits):
    card, src = _two_group_run(senv, 880, 780, seed=31)
    spill = _new_sub(box_2d=[436, 10, 466, 990])
    senv.gem.reads = [_sub_read(), _sub_read(_new_head(), spill), _sub_read(_new_head(), spill),
                      _sub_read(_new_head())]
    asyncio.run(th.generate_previews(880, look={"refs": {"intro_card_id": src}}))
    first, second = edits
    assert f'"Decode Your Tech Future" becomes exactly "{NEW}"' in first["directive"]
    assert f'"Start your journey in tech today" becomes exactly "{NEW_SUB}"' in first["directive"]
    # The retry is the subtitle's alone, tighter, on the card with the new
    # headline already in.
    assert "Decode Your Tech Future" not in second["directive"] and NEW_SUB in second["directive"]
    assert "The subtitle sits in the box" in second["directive"]
    assert "The headline sits in the box" not in second["directive"]
    assert "ran outside its box" in second["directive"] and "Check every letter" not in second["directive"]
    assert second["canvas"].getpixel(HEAD_PX) == (10, 20, 30), "the passed headline is on the canvas"
    assert second["canvas"].getpixel(SUB_PX) == card.getpixel(SUB_PX), "the subtitle's own words"
    # The card: the new headline, the card's own subtitle, every other pixel kept.
    row = _card_of(senv, 880)
    out = _img(row)
    assert out.getpixel(HEAD_PX) == (10, 20, 30)
    assert out.getpixel(SUB_PX) == card.getpixel(SUB_PX)
    assert out.crop((0, 0, W, 300)).tobytes() == card.crop((0, 0, W, 300)).tobytes()
    assert out.crop((0, 1100, W, H)).tobytes() == card.crop((0, 1100, W, H)).tobytes()
    swap = row["metadata"]["text_swap"]
    assert swap["status"] == "swapped"
    assert swap["swapped"] == [{"kind": "headline", "old": "Decode Your Tech Future", "new": NEW}]
    kept, = swap["kept"]
    assert kept["kind"] == "subtitle" and kept["old"] == "Start your journey in tech today"
    assert kept["new"] == NEW_SUB and "ran outside" in kept["why"]
    warning = swap["warning"]
    assert "subtitle" in warning and '"Start your journey in tech today"' in warning and NEW_SUB in warning
    assert chr(0x2014) not in warning
    # Read four times: the pick, each attempt, then the card as it is kept.
    assert len(senv.gem.of(thp.CARD_TEXT_READ_SYSTEM)) == 4
    assert row["metadata"]["card_text"] == \
        f"42 ABU DHABI\n{NEW.upper()}\nStart your journey in tech today\n42abudhabi.ae"
    # The script keeps its subtitle words, so making the card again tries again.
    script = senv.db.md(880)["script"]
    assert script["intro_headline"] == NEW and script["intro_subtitle"] == NEW_SUB


def test_a_subtitle_retry_that_fits_swaps_both(senv, edits):
    _card, src = _two_group_run(senv, 881, 781, seed=32)
    spill = _new_sub(box_2d=[436, 10, 466, 990])
    senv.gem.reads = [_sub_read(), _sub_read(_new_head(), spill), _sub_read(_new_head(), _new_sub())]
    asyncio.run(th.generate_previews(881, look={"refs": {"intro_card_id": src}}))
    assert len(edits) == 2
    row = _card_of(senv, 881)
    out = _img(row)
    assert out.getpixel(HEAD_PX) == (10, 20, 30), "the headline of the first attempt"
    assert out.getpixel(SUB_PX) == (200, 40, 40), "the subtitle of the second"
    swap = row["metadata"]["text_swap"]
    assert swap["status"] == "swapped" and swap["warning"] is None and swap["kept"] == []
    assert [s["kind"] for s in swap["swapped"]] == ["headline", "subtitle"]
    assert len(senv.gem.of(thp.CARD_TEXT_READ_SYSTEM)) == 3, "no extra read when every line passed"


def test_a_misspelt_headline_is_retried_alone_with_the_spelling_check(senv, edits):
    card, src = _two_group_run(senv, 882, 782, seed=33)
    typo = _new_head(text="LEARN FROM YOUR PEARS")
    senv.gem.reads = [_sub_read(), _sub_read(typo, _new_sub()), _sub_read(typo, _new_sub()),
                      _sub_read(None, _new_sub())]
    asyncio.run(th.generate_previews(882, look={"refs": {"intro_card_id": src}}))
    second = edits[1]["directive"]
    assert "Check every letter" in second and "ran outside its box" not in second
    assert '"Decode Your Tech Future" becomes exactly' in second and NEW_SUB not in second
    row = _card_of(senv, 882)
    out = _img(row)
    assert out.getpixel(SUB_PX) == (10, 20, 30) and out.getpixel(HEAD_PX) == card.getpixel(HEAD_PX)
    swap = row["metadata"]["text_swap"]
    assert [s["kind"] for s in swap["swapped"]] == ["subtitle"]
    assert swap["kept"][0]["kind"] == "headline" and "headline" in swap["warning"]


def test_a_kept_card_that_does_not_read_back_is_the_original(senv, edits):
    card, src = _two_group_run(senv, 883, 783, seed=34)
    spill = _new_sub(box_2d=[436, 10, 466, 990])
    # The card as it would be kept reads wrong: nothing of the swap is kept.
    senv.gem.reads = [_sub_read(), _sub_read(_new_head(), spill), _sub_read(_new_head(), spill),
                      _sub_read(_new_head(text="WHY PEER"))]
    asyncio.run(th.generate_previews(883, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 883)
    assert _img(row).tobytes() == card.tobytes()
    swap = row["metadata"]["text_swap"]
    assert swap["status"] == "failed" and swap["swapped"] == [] and "keeps its own words" in swap["warning"]


def test_the_warning_of_a_line_that_keeps_its_words():
    plan = _plan_835()
    why = 'the new words "No teachers, just collaboration" ran outside the card\'s text area'
    w = th._partial_swap_warning("intro", [{**plan[1], "why": why}])
    assert w.startswith("The intro card's subtitle couldn't be swapped cleanly")
    assert '"Start your journey in tech today"' in w and SUB_835 in w and chr(0x2014) not in w


# The zones of card 835's headline and subtitle overlap (the headline's runs
# to y 929, the subtitle's starts at y 857). Solid boxes stand for letters:
# the card's old words in black, the edit's new ones in blue.
BG_835, OLD_INK, NEW_INK = (240, 240, 240), (0, 0, 0), (0, 0, 200)
NEW_L1, NEW_L2 = [377, 280, 423, 720], [430, 400, 478, 600]   # "WORKS": narrower than "TECH FUTURE"
SPILL_SUB, FIT_SUB = [492, 150, 519, 850], [492, 290, 519, 710]


def _px_835(b: list[int]) -> tuple[float, float, float, float]:
    y0, x0, y1, x1 = b
    w, h = C835
    return (x0 * w / 1000, y0 * h / 1000, x1 * w / 1000, y1 * h / 1000)


def _card_835(old: list[list[int]], new: list[list[int]]) -> bytes:
    from PIL import ImageDraw

    im = Image.new("RGB", C835, BG_835)
    d = ImageDraw.Draw(im)
    for b in old:
        d.rectangle(_px_835(b), fill=OLD_INK)
    for b in new:
        d.rectangle(_px_835(b), fill=NEW_INK)
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _read_of(sub_box: list[int], sub_text: str = SUB_835) -> list[dict]:
    return [{"text": "42 Abu Dhabi", "kind": "logo", "box_2d": [265, 404, 289, 595]},
            {"text": "WHY PEER LEARNING", "kind": "headline", "box_2d": NEW_L1},
            {"text": "WORKS", "kind": "headline", "box_2d": NEW_L2},
            {"text": sub_text, "kind": "subtitle", "box_2d": sub_box}]


def _swap_835(monkeypatch, edits: list[bytes], reads: list[list[dict]], plan: list[dict] | None = None):
    """th._swap_card_text on card 835's lines (both its groups, or `plan`),
    the edits and reads given in turn (nothing else is called)."""
    async def edit(parts, **kw):
        return edits.pop(0)

    async def read(data):
        return th._card_lines_from({"lines": reads.pop(0)})

    monkeypatch.setattr(ca, "_render_edit_escalating", edit)
    monkeypatch.setattr(th, "_read_card_bytes", read)
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    original = _card_835([ln["box"] for ln in SRC_835], [])
    out, _after, done, left = asyncio.run(
        th._swap_card_text(original, SRC_835, plan or _plan_835(), "9:16", CONTENT_835))
    assert edits == [] and reads == []
    with Image.open(BytesIO(out)) as im:
        return im.convert("RGB"), [p["kind"] for p in done], [p["kind"] for p in left]


def _old_ink_left(out: Image.Image) -> int:
    """Pixels of the old headline's last line ("TECH FUTURE") with more than
    a fifth of the old (black) ink still in them, on the background or the
    new (blue) words alike: blue under 80% of the new ink's."""
    x0, y0, x1, y1 = _px_835(SRC_835[2]["box"])
    return sum(1 for y in range(int(y0) + 1, int(y1)) for x in range(int(x0) + 1, int(x1))
               if out.getpixel((x, y))[2] < 0.8 * NEW_INK[2])


def _new_ink_outside(out: Image.Image, allowed: list[tuple]) -> int:
    """New (blue) pixels outside the `allowed` rects."""
    a = out.load()
    w, h = out.size
    return sum(1 for y in range(600, 1000) for x in range(w)
               if a[x, y][2] > 120 and a[x, y][0] < 120
               and not any(r[0] - 1 <= x <= r[2] + 1 and r[1] - 1 <= y <= r[3] + 1 for r in allowed))


def test_a_headline_kept_beside_a_subtitle_that_spilt_leaves_no_old_letters_and_no_spill(monkeypatch):
    # The subtitle spills on both attempts and keeps its words. The kept
    # headline's paste once left out the subtitle's whole zone: the bottom
    # of "TECH FUTURE" stayed beside the narrower "WORKS" (6280 px), and the
    # spill's ends past the subtitle's zone were pasted in (1122 px).
    spilt = _card_835([SRC_835[0]["box"]], [NEW_L1, NEW_L2, SPILL_SUB])
    out, done, left = _swap_835(
        monkeypatch, [spilt, spilt],
        [_read_of(SPILL_SUB), _read_of(SPILL_SUB), _read_of(SRC_835[3]["box"], SRC_835[3]["text"])])
    assert done == ["headline"] and left == ["subtitle"]
    assert _old_ink_left(out) == 0
    assert _new_ink_outside(out, [_px_835(NEW_L1), _px_835(NEW_L2)]) == 0, "no piece of the spilt subtitle"
    x0, y0, x1, y1 = _px_835(SRC_835[3]["box"])
    assert out.getpixel((round((x0 + x1) / 2), round((y0 + y1) / 2))) == OLD_INK, "the subtitle's own words"


def test_a_subtitle_retry_that_fits_leaves_no_old_letters_and_no_spill(monkeypatch):
    spilt = _card_835([SRC_835[0]["box"]], [NEW_L1, NEW_L2, SPILL_SUB])
    fits = _card_835([SRC_835[0]["box"]], [NEW_L1, NEW_L2, FIT_SUB])
    out, done, left = _swap_835(monkeypatch, [spilt, fits], [_read_of(SPILL_SUB), _read_of(FIT_SUB)])
    assert done == ["headline", "subtitle"] and left == []
    assert _old_ink_left(out) == 0
    assert _new_ink_outside(out, [_px_835(NEW_L1), _px_835(NEW_L2), _px_835(FIT_SUB)]) == 0


def test_a_line_swapped_beside_a_kept_one_is_pasted_at_full_strength():
    # Only the subtitle (or only the headline) changes: the other line's
    # cut-out used to reach into it (24 px apart on card 835), so the new
    # subtitle's top was pasted at 46%, the old letters showing through.
    head, sub = th._swap_text_boxes(C835, SRC_835, _plan_835())
    for words, box in ((("Decode Your Tech Future", SUB_835), sub),
                       (("Why Peer Learning Works", SRC_835[3]["text"]), head)):
        plan = th._text_swap_plan(SRC_835, *words)
        assert len(plan) == 1
        alpha, _zone = th._text_swap_mask(C835, SRC_835, plan, CONTENT_835)
        assert not th._swap_weak(alpha, [box[:4]], 0.0), plan[0]["kind"]
        # The kept line itself is still cut out whole.
        kept = th._line_px_box(SRC_835[3 if plan[0]["kind"] == "headline" else 2], *C835)
        assert alpha.crop(kept).getextrema()[1] == 0


def test_a_new_line_in_a_kept_lines_cut_out_does_not_fit():
    # Only the headline is swapped and the subtitle keeps its words: a new
    # last line read lower than the old one ends in the subtitle's cut-out.
    # The safe area alone took it (the zone runs to y 929); the paste fades
    # it there.
    plan = th._text_swap_plan(SRC_835, "Why Peer Learning Works", SRC_835[3]["text"])
    zones = th._swap_zone_rects(C835, SRC_835, plan, CONTENT_835)
    safe = th._swap_safe_rects(C835, zones)
    tol = th._swap_tolerances(C835, SRC_835, plan)
    alpha, _zone = th._text_swap_mask(C835, SRC_835, plan, CONTENT_835)

    def read(last: list[int]) -> list[dict]:
        return th._card_lines_from({"lines": [
            {"text": "42 Abu Dhabi", "kind": "logo", "box_2d": [265, 404, 289, 595]},
            {"text": "WHY PEER", "kind": "headline", "box_2d": [377, 300, 423, 700]},
            {"text": "LEARNING WORKS", "kind": "headline", "box_2d": last},
            {"text": SRC_835[3]["text"], "kind": "subtitle", "box_2d": SRC_835[3]["box"]}]})

    at_old = read([431, 262, 479, 738])
    assert th._swap_fits(C835, SRC_835, plan, at_old, safe)[0] is True
    assert th._swap_faded(C835, SRC_835, plan, at_old, zones, [0], alpha, tol) == {}
    lower = read([442, 262, 490, 738])
    assert th._swap_fits(C835, SRC_835, plan, lower, safe)[0] is True, "inside the safe area"
    got = th._swap_faded(C835, SRC_835, plan, lower, zones, [0], alpha, tol)
    assert "LEARNING WORKS" in got[0] and "ran outside" in got[0]


def test_a_swap_whose_new_line_would_be_faded_keeps_the_card(monkeypatch):
    # The same, made: both attempts put the last line there, so the card
    # keeps its own words (it was swapped with that line faded).
    plan = th._text_swap_plan(SRC_835, "Why Peer Learning Works", SRC_835[3]["text"])
    low = [442, 262, 490, 738]
    edit = _card_835([SRC_835[0]["box"], SRC_835[3]["box"]], [[377, 300, 423, 700], low])
    reads = [[{"text": "42 Abu Dhabi", "kind": "logo", "box_2d": [265, 404, 289, 595]},
              {"text": "WHY PEER", "kind": "headline", "box_2d": [377, 300, 423, 700]},
              {"text": "LEARNING WORKS", "kind": "headline", "box_2d": low},
              {"text": SRC_835[3]["text"], "kind": "subtitle", "box_2d": SRC_835[3]["box"]}]] * 2
    with pytest.raises(th._SwapFailed, match="LEARNING WORKS"):
        _swap_835(monkeypatch, [edit, edit], [list(r) for r in reads], plan)


# ---------------------------------------------------------------------------
# U3: a card with no current plan is planned once, in the background
# ---------------------------------------------------------------------------

def _director_calls(env) -> int:
    return len(env.gem.of(thp.BOOKEND_MOTION_DIRECTOR_SYSTEM))


def _old_plan_run(env, run_id: int, **kw) -> tuple[int, int]:
    """A run whose intro card has a plan under the current rules and whose
    outro card (run 830's 42 logo frame, card 831) has one from before them."""
    _stills_run(env, run_id, **kw)
    intro = _seed_card(env, run_id, "intro", source_ref=None)
    outro = _seed_card(env, run_id, "outro", source_ref=None)
    env.db.rows[intro]["metadata"]["motion"] = th._bookend_plan_from(ICONS)
    env.db.rows[outro]["metadata"]["motion"] = {"mode": "veo", "elements": [{"what": "vertical line"}]}
    env.db.md(run_id).update(intro_image_id=intro, outro_image_id=outro)
    return intro, outro


def _open(run_id: int, times: int = 1, *, together: bool = False, with_reel: bool = True) -> list[dict]:
    """fetch_run as the page polls it, then the background plans finished."""
    async def go():
        if together:
            got = await asyncio.gather(*(th.fetch_run(run_id, with_reel=with_reel) for _ in range(times)))
        else:
            got = [await th.fetch_run(run_id, with_reel=with_reel) for _ in range(times)]
        while th._CARD_PLAN_TASKS:
            await asyncio.gather(*list(th._CARD_PLAN_TASKS))
        return list(got)

    return asyncio.run(go())


@pytest.fixture
def fenv(benv, monkeypatch):
    """benv with the real fetch_run."""
    monkeypatch.setattr(th, "fetch_run", REAL_FETCH_RUN)
    return benv


def test_a_card_from_before_the_rules_is_planned_once_on_opening(fenv):
    intro, outro = _old_plan_run(fenv, 890)
    fenv.gem.plan = LOGO_ONLY
    first, = _open(890)
    assert first["outro"]["motion"] is None, "the old plan is never shown as the plan"
    assert first["intro"]["motion"] == "veo"
    assert _director_calls(fenv) == 1, "one look, at the outro card only"
    cmd = fenv.db.rows[outro]["metadata"]
    assert th._plan_ok(cmd["motion"]) and cmd["motion"]["mode"] == "zoom"
    assert cmd["motion_warm"] == th.BOOKEND_PLAN_VERSION
    assert "motion_warm" not in fenv.db.rows[intro]["metadata"]
    # Polled again and again: never asked again; the tile now has the plan.
    again = _open(890, times=3)
    assert _director_calls(fenv) == 1
    assert again[-1]["outro"]["motion"] == "zoom"
    assert again[-1]["videos_plan"]["clips"][-1]["engine"] == "zoom", "the price knows it too"


def test_polls_at_the_same_moment_plan_a_card_once(fenv):
    _old_plan_run(fenv, 891)
    _open(891, times=4, together=True)
    assert _director_calls(fenv) == 1


def test_a_failed_plan_is_never_retried_by_polling(fenv):
    _intro, outro = _old_plan_run(fenv, 892)
    fenv.gem.plan = RuntimeError("vision down")
    _open(892, times=3)
    assert _director_calls(fenv) == 1, "claimed before the call: a failure never loops"
    cmd = fenv.db.rows[outro]["metadata"]
    assert not th._plan_ok(cmd["motion"]) and cmd["motion_warm"] == th.BOOKEND_PLAN_VERSION
    got, = _open(892)
    assert got["outro"]["motion"] is None and _director_calls(fenv) == 1


def test_no_background_plan_where_none_is_needed(fenv):
    # A list of runs, an excluded bookend, a card from another image, a
    # card whose plan is current: nothing is planned.
    _old_plan_run(fenv, 893)
    _open(893, with_reel=False)
    assert _director_calls(fenv) == 0
    _old_plan_run(fenv, 894, outro_excluded=True)
    _open(894)
    assert _director_calls(fenv) == 0
    _old_plan_run(fenv, 895, refs={"outro_asset_id": 242})
    _open(895)
    assert _director_calls(fenv) == 0, "made from another image: it is made again, then planned"
    _intro, outro = _old_plan_run(fenv, 896)
    fenv.db.rows[outro]["metadata"]["motion"] = th._bookend_plan_from(LOGO_ONLY)
    _open(896)
    assert _director_calls(fenv) == 0


def test_the_claim_is_one_atomic_statement(env):
    env.db.add_run(897, {"card_type": "outro"})
    assert asyncio.run(th._claim_card_plan(897)) is True
    assert asyncio.run(th._claim_card_plan(897)) is False
    s = next(x for x in env.db.log if "motion_warm" in x)
    assert "COALESCE(metadata->>'motion_warm', '') <> %s" in s and "RETURNING id" in s


# ---------------------------------------------------------------------------
# U4: what a still's background work is (a free crop is never "Regenerating")
# ---------------------------------------------------------------------------

@pytest.fixture
def dispatched(monkeypatch):
    """edit_run's background regen, recorded instead of run."""
    specs: list[list[dict]] = []
    monkeypatch.setattr(th, "_dispatch_keyframe_regen", lambda run_id, s: specs.append(list(s)))
    return specs


def _above_hip_run(env, run_id: int, **kw) -> None:
    env.db.add_run(run_id, _md(lock=False, framing="above_hip", main_framing_used="above_hip", **kw))
    _seed_old_main(env, run_id)


def test_a_tighter_framing_is_marked_a_crop_a_wider_one_a_reshoot(r3env, dispatched):
    _above_hip_run(r3env, 320)
    asyncio.run(th.edit_run(run_id=320, scene_framings=["close"]))
    md = r3env.db.md(320)
    assert md["main_keyframe_generating"] is True and md["main_keyframe_work"] == "crop"
    _above_hip_run(r3env, 321)
    asyncio.run(th.edit_run(run_id=321, scene_framings=["full_body"]))
    assert r3env.db.md(321)["main_keyframe_work"] == "reshoot"
    # A later scene, on a multi-scene run: on its own scene.
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=760, framing_used="above_hip")
    r3env.db.add_run(322, md)
    _seed_scene_row(r3env, 322, 1, 760)
    asyncio.run(th.edit_run(run_id=322, scene_framings=[None, "close"]))
    sc = r3env.db.md(322)["scenes"][1]
    assert sc["keyframe_generating"] is True and sc["keyframe_work"] == "crop"
    assert "main_keyframe_work" not in r3env.db.md(322)


def test_a_new_picture_is_marked_made_again(r3env, dispatched):
    r3env.ref_file("asset", 7)
    _above_hip_run(r3env, 323)
    asyncio.run(th.edit_run(run_id=323, refs_patch={"character_asset_id": 7}, lock_character=True))
    assert r3env.db.md(323)["main_keyframe_work"] == "regen"


def test_a_crop_that_cannot_be_made_says_it_is_a_reshoot(r3env, monkeypatch):
    _above_hip_run(r3env, 324, main_keyframe_generating=True, main_keyframe_work="crop")
    r3env.db.md(324)["framing"] = "close"
    monkeypatch.setattr(th, "_face_box", lambda im: None)   # no face to crop around
    seen: list = []

    async def refine(run_id, which, change, **kw):
        seen.append(r3env.db.md(run_id).get("main_keyframe_work"))

    monkeypatch.setattr(th, "refine_still", refine)
    asyncio.run(th._run_keyframe_regen(324, [{"scene_index": 0, "reshoot": True}]))
    assert seen == ["reshoot"], "marked before the paid re-shoot runs"
    md = r3env.db.md(324)
    assert md["main_keyframe_generating"] is False and md["main_keyframe_work"] is None


def test_the_work_unit(r3env):
    md = _md(lock=False, framing="close", main_framing_used="above_hip")
    assert th._keyframe_work({"scene_index": 0, "reshoot": True}, md, md) == "crop"
    wide = {**md, "framing": "full_body"}
    assert th._keyframe_work({"scene_index": 0, "reshoot": True}, wide, md) == "reshoot"
    both = {"scene_index": 0, "keep_subject": True, "has_char_ref": False, "has_bg_ref": True, "reshoot": True}
    assert th._keyframe_work(both, md, md) == "regen", "a new background first: a paid still"


def test_full_body_to_close_up_is_a_reshoot_not_a_free_crop(r3env, monkeypatch):
    # A crop never enlarges more than 3x: a full-body face (about 7% of the
    # frame) cut to a close-up (33%) would need ~5x, so that still is re-shot
    # (paid) and its tile, toast and hint must not say "free".
    full = _md(lock=False, framing="close", main_framing_used="full_body")
    assert th._keyframe_work({"scene_index": 0, "reshoot": True}, full, full) == "reshoot"
    assert th._keyframe_work({"scene_index": 0, "reshoot": True}, {**full, "framing": "above_hip"}, full) == "crop"
    framings = fb, hip, close = ("full_body", "above_hip", "close")
    assert {(a, b): th._crop_likely(b, a) for a in framings for b in framings if a != b} == {
        (fb, hip): True, (fb, close): False, (hip, close): True,
        (hip, fb): False, (close, fb): False, (close, hip): False}
    # The crop itself agrees: a typical full-body still is refused for a
    # close-up and cropped to above the hip.
    still = Image.new("RGB", (768, 1344), (90, 90, 90))
    buf = BytesIO()
    still.save(buf, "PNG")
    monkeypatch.setattr(th, "_face_box", lambda im: (350, 200, 70, round(0.07 * im.height)))
    assert th._tight_crop_png(buf.getvalue(), close, "9:16") is None
    assert th._tight_crop_png(buf.getvalue(), hip, "9:16") is not None


def test_the_still_price_is_the_image_model_in_use(fenv, monkeypatch):
    # Refine, Regenerate and a re-shoot call the Pro image model, metered at
    # $0.13; a project without it falls back to the standard one ($0.04, all
    # prod has today). The Stills labels say the price of the one in use.
    from app.config import config
    from app.services import vertex_client as vc

    assert th.GEMINI_PRO_MODEL == config.vertex_image_pro_model
    _stills_run(fenv, 899, intro_excluded=True, outro_excluded=True)
    monkeypatch.setattr(vc, "_pro_image_unavailable", False)
    got, = _open(899)
    assert got["still_image_usd"] == vc._IMAGE_PRICE_USD.get(config.vertex_image_pro_model, 0.05)
    monkeypatch.setattr(vc, "_pro_image_unavailable", True)
    got, = _open(899)
    assert got["still_image_usd"] == vc._IMAGE_PRICE_USD.get(config.vertex_image_model, 0.05)
    if (config.vertex_image_pro_model, config.vertex_image_model) == \
            ("gemini-3-pro-image-preview", "gemini-2.5-flash-image"):
        assert th._still_image_usd() == 0.04
        monkeypatch.setattr(vc, "_pro_image_unavailable", False)
        assert th._still_image_usd() == 0.13


def test_fetch_run_sends_the_work(fenv):
    _stills_run(fenv, 898, intro_excluded=True, outro_excluded=True,
                main_keyframe_generating=True, main_keyframe_work="crop")
    got, = _open(898)
    assert got["scenes"][0]["keyframe_status"] == "generating" and got["scenes"][0]["keyframe_work"] == "crop"
    fenv.db.md(898).update(main_keyframe_generating=False)
    got, = _open(898)
    assert got["scenes"][0]["keyframe_work"] is None
    # An older marker with no work: a still made again.
    fenv.db.md(898).update(main_keyframe_generating=True, main_keyframe_work=None)
    got, = _open(898)
    assert got["scenes"][0]["keyframe_work"] == "regen"
