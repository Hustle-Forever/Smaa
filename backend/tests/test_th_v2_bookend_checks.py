"""TH v2 intro / outro, round 2 (the live QA of run 830): only real icons go
to Veo, a Veo clip is checked pixel by pixel, a swapped headline must fit.

- The motion director: a card whose only graphic is a logo or a wordmark, or
  plain text, has nothing for Veo (the slow zoom). Run 830's picks are not
  icons: the 42 logo frame's "vertical line" (anim 353), "light streaks" and
  a "large '42' number" (anim 352). Plans carry their rules' version: an
  older cached plan is made again.
- The Veo bookend prompt: static camera, background pixels and colours kept,
  no new marks, grids or lines, the logo and every line untouched.
- The pixel check (fixtures: run 830's frames, scaled down, and its cards):
  anim 353 (a grid of marks, a longer divider, a nudged logo) and anim 352 (a
  restyle) fail; faithful clips with moving icons pass.
- A swapped headline must fit: the new words stay inside the old words' zone
  (never widened; never out of a padded card's picture) and every new line
  read back lies in the zone's safe area, else one tighter attempt, else the
  card keeps its words. Checked on the geometry of run 830's intro (card 832,
  made from card 825).

No database and no paid call: the fakes of test_th_v2_scene_stills,
test_th_v2_bookends and test_th_v2_bookend_swap.
"""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp
from test_th_v2_bookend_swap import (  # noqa: F401
    LOGO, NEW, OLD_HEAD, URL, _card_of, _card_picture, _img, _new_run, _prev_run, _read, senv,
)
from test_th_v2_bookends import ICONS, LOGO_ONLY, _bookend_md, benv  # noqa: F401
from test_th_v2_scene_stills import env  # noqa: F401

FIX = Path(__file__).parent / "fixtures" / "th_bookend_drift"
# Where anim 353's divider line is on card 831 (the old plan's one element).
LINE_831 = [504, 554, 568, 558]


def _open(name: str) -> Image.Image:
    return Image.open(FIX / name).convert("RGB")


# ---------------------------------------------------------------------------
# T5: the director keeps only icons
# ---------------------------------------------------------------------------

def test_run_830s_picks_are_not_icons():
    outro_353 = {"has_animatable_elements": True, "card_text": ["42", "ABU DHABI"], "elements": [
        {"what": "vertical line", "where": "between 42 and the text", "motion": "draw_on"}]}
    intro_352 = {"has_animatable_elements": True, "card_text": ["WHY PEER"], "elements": [
        {"what": "light streaks", "where": "top half of the card", "motion": "draw_on"},
        {"what": "large '42' number", "where": "bottom center", "motion": "pulse"}]}
    for answer in (outro_353, intro_352):
        plan = th._bookend_plan_from(answer)
        assert plan["mode"] == "zoom" and plan["elements"] == [], answer["elements"]
        assert plan["v"] == th.BOOKEND_PLAN_VERSION
    for what in ("divider", "the separator", "thin border", "grid of dots", "background pattern",
                 "glow", "42", "number 7", "'Apply'", "logo lockup", "underline"):
        plan = th._bookend_plan_from({"has_animatable_elements": True,
                                      "elements": [{"what": what, "where": "x", "motion": "pop"}]})
        assert plan["mode"] == "zoom", what


def test_an_icon_is_judged_by_what_its_name_names():
    # A line, frame, box or ray in an icon's name does not make it a line:
    # these are icons, and Veo animates them (owner decision 1).
    for what in ("line chart icon", "bar graph with trend line", "gift box", "check box icon",
                 "speech box", "picture frame icon", "lightbulb with rays", "laptop with code lines",
                 "3D cube icon", "graduate's cap", "students' desk", "rocket icon", "star",
                 "speech bubble with text lines", "trophy"):
        plan = th._bookend_plan_from({"has_animatable_elements": True, "elements": [
            {"what": what, "where": "x", "motion": "pop", "box_2d": [100, 100, 300, 300]}]})
        assert plan["mode"] == "veo", what
    # Text, a logo, a number, the background, a person, or a line or effect
    # as the thing named: not icons.
    for what in ("vertical line", "light streaks", "large '42' number", "grid of dots", "text box",
                 "headline text", "logo icon", "background circles", "person on the left",
                 "lines and dots", "thin gold frame around the headline", "rays of light",
                 "“Apply”", "", "the"):
        assert th._not_an_icon(what) is True, what


def test_an_icon_keeps_its_box_and_a_tiny_or_text_covered_one_is_dropped():
    answer = {"has_animatable_elements": True, "card_text": [], "elements": [
        {"what": "rocket icon", "where": "top left", "motion": "float", "box_2d": [100, 80, 260, 300]},
        {"what": "star", "where": "corner", "motion": "pulse", "box_2d": [10, 10, 30, 40]},
        {"what": "lightbulb icon", "where": "on the headline", "motion": "pop", "box_2d": [320, 120, 400, 880]},
    ]}
    text_lines = [{"text": "Decode Your Tech Future", "kind": "headline", "box": [312, 100, 407, 900]}]
    plan = th._bookend_plan_from(answer, text_lines=text_lines)
    assert plan["mode"] == "veo"
    assert [(e["what"], e["box"]) for e in plan["elements"]] == [("rocket icon", [100, 80, 260, 300])]
    # Without the card's text read the lightbulb stays (it is an icon).
    assert len(th._bookend_plan_from(answer)["elements"]) == 2
    # A box that makes no sense is no box.
    plan = th._bookend_plan_from({"has_animatable_elements": True, "elements": [
        {"what": "rocket icon", "where": "x", "motion": "float", "box_2d": [500, 500, 100, 100]}]})
    assert plan["elements"][0]["box"] is None


def test_an_old_cached_plan_is_planned_again(benv):
    card = benv.pick("asset", 330)
    old = {"mode": "veo", "elements": [{"what": "vertical line", "where": "middle", "motion": "draw_on"}],
           "motion_plan": "", "card_text": [], "reason": "graphic elements to animate"}
    bk = {"path": card, "card_id": None, "card": {"metadata": {"motion": old}}, "kept": True}
    benv.gem.plan = LOGO_ONLY
    job = asyncio.run(th._bookend_job(role="outro", bookend=bk, aspect="9:16", quality=None))
    assert job["style"] == "slow_zoom" and job["coro"] is None
    assert len(benv.gem.of(thp.BOOKEND_MOTION_DIRECTOR_SYSTEM)) == 1, "asked again under the new rules"
    assert th._plan_ok(old) is False and th._plan_ok(th._bookend_plan_from(LOGO_ONLY)) is True
    # A plan under the current rules is used as it is.
    bk["card"] = {"metadata": {"motion": th._bookend_plan_from(ICONS)}}
    job = asyncio.run(th._bookend_job(role="outro", bookend=bk, aspect="9:16", quality=None))
    assert job["style"] == "animate_icons" and len(benv.gem.of(thp.BOOKEND_MOTION_DIRECTOR_SYSTEM)) == 1
    job["coro"].close()


def test_the_director_is_told_what_is_never_an_icon():
    s = thp.BOOKEND_MOTION_DIRECTOR_SYSTEM
    for rule in ("a big number such as '42' is text", "divider or separator line",
                 "whose only graphic is a logo or a wordmark", "box_2d"):
        assert rule in s, rule
    item = thp.BOOKEND_MOTION_DIRECTOR_SCHEMA["properties"]["elements"]["items"]
    assert "box_2d" in item["required"] and item["properties"]["box_2d"]["type"] == "ARRAY"


def test_the_veo_bookend_prompt_keeps_every_pixel_but_the_icons():
    p = thp.build_bookend_motion_veo_prompt(role="outro", elements=ICONS["elements"][:1])
    for rule in ("CAMERA: locked off", "The frame is static", "Every background pixel keeps its colour",
                 "add no marks, dots, grids, patterns, lines", "every line, divider, rule and border stay untouched",
                 "Keep every letter, word, number, logo and colour exactly as on the card"):
        assert rule in p, rule
    neg = thp.BOOKEND_MOTION_NEGATIVE_PROMPT
    assert len(neg) < 1000
    for word in ("grid", "dots", "longer lines", "moving logo", "camera drift", "background change"):
        assert word in neg, word


# ---------------------------------------------------------------------------
# T6: the pixel check of a Veo bookend
# ---------------------------------------------------------------------------

def test_anim_353_fails_the_pixel_check():
    """The 42 logo frame: a faint grid on the white, a longer divider, a
    nudged logo. The text read passed it; the pixels do not."""
    card = _open("card831_360.png")
    frames = [_open(f"anim353_f{k}_360.png") for k in range(5)]
    for boxes in ([LINE_831], []):
        r = th._bookend_drift(card, frames, boxes)
        assert r["ok"] is False, boxes
        assert r["blob"] >= th.DRIFT_BLOB_MIN and r["marks"] > th.DRIFT_MARKS_MAX
        assert r["changed"] > th.DRIFT_CHANGED_MAX
        assert "frame 2" in r["reason"], "caught one second in, where the divider grows"


def test_anim_352_fails_the_pixel_check():
    """The intro: Veo zoomed the card and grew its lines."""
    card = _open("card832_180.png")
    frames = [_open(f"anim352_f{k}_180.png") for k in (0, 2, 4)]
    r = th._bookend_drift(card, frames, [])
    assert r["ok"] is False and "look changed" in r["reason"] and r["changed"] > 0.05


def test_the_first_frames_alone_are_the_card():
    assert th._bookend_drift(_open("card831_360.png"), [_open("anim353_f0_360.png")], [])["ok"] is True
    assert th._bookend_drift(_open("card832_180.png"), [_open("anim352_f0_180.png")], [])["ok"] is True


def _icon_card(size=(360, 640)) -> tuple[Image.Image, list[int]]:
    """A white card with dark text bars, a logo block, a divider and one
    round icon; returns it and the icon's box (0..1000)."""
    w, h = size
    im = Image.new("RGB", size, (250, 250, 250))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 330, 320, 360), fill=(25, 25, 25))     # headline
    d.rectangle((60, 375, 300, 390), fill=(90, 90, 90))      # subtitle
    d.rectangle((140, 560, 220, 600), fill=(10, 10, 10))     # logo
    d.line((180, 420, 180, 470), fill=(30, 30, 30), width=2)  # divider
    d.ellipse((140, 120, 220, 200), fill=(0, 170, 200))      # icon
    d.rectangle((165, 145, 195, 175), fill=(255, 255, 255))
    return im, [int(110 / h * 1000), int(130 / w * 1000), int(210 / h * 1000), int(230 / w * 1000)]


def _jpeg(im: Image.Image, q: int = 85) -> Image.Image:
    buf = BytesIO()
    im.save(buf, "JPEG", quality=q)
    return Image.open(BytesIO(buf.getvalue())).convert("RGB")


def _move_icon(card: Image.Image, scale: float = 1.0, dy: int = 0) -> Image.Image:
    im = card.copy()
    box = (135, 115, 225, 205)
    patch = card.crop(box)
    ImageDraw.Draw(im).rectangle(box, fill=(250, 250, 250))
    pw, ph = patch.size
    p2 = patch.resize((int(pw * scale), int(ph * scale)), Image.BICUBIC)
    im.paste(p2, (box[0] - (p2.width - pw) // 2, box[1] - (p2.height - ph) // 2 + dy))
    return im


def test_a_faithful_clip_with_moving_icons_passes():
    card, icon = _icon_card()
    clips = {
        "still, with codec noise": [_jpeg(card) for _ in range(5)],
        "the icon pulses": [_jpeg(_move_icon(card, s)) for s in (1.0, 1.04, 1.08, 1.04, 1.0)],
        "the icon floats": [_jpeg(_move_icon(card, 1.0, dy)) for dy in (0, -4, 0, 4, 0)],
    }
    for name, frames in clips.items():
        r = th._bookend_drift(card, frames, [icon])
        assert r["ok"] is True, (name, r)
        assert r["blob"] < th.DRIFT_BLOB_MIN and r["marks"] <= th.DRIFT_MARKS_MAX


def test_marks_a_grown_line_a_nudged_logo_and_new_colours_fail():
    card, icon = _icon_card()

    def marked(im):
        a = np.asarray(im).copy()
        for y in range(10, a.shape[0], 12):
            for x in range(10, a.shape[1], 12):
                if a[y, x].mean() > 240:
                    a[y:y + 3, x:x + 3] = a[y, x] - 3
        return Image.fromarray(a)

    def grown(im):
        im = im.copy()
        ImageDraw.Draw(im).line((180, 400, 180, 500), fill=(30, 30, 30), width=2)
        return im

    def nudged(im):
        im = im.copy()
        d = ImageDraw.Draw(im)
        d.rectangle((140, 560, 220, 600), fill=(250, 250, 250))
        d.rectangle((146, 560, 226, 600), fill=(10, 10, 10))
        return im

    def dimmed(im):
        return Image.fromarray(np.clip(np.asarray(im).astype(int) - 5, 0, 255).astype(np.uint8))

    for name, bad, word in (("a grid of faint marks", marked, "marks"), ("a longer divider", grown, "added"),
                            ("a nudged logo", nudged, "added or moved"),
                            ("a darker background", dimmed, "brightness")):
        frames = [card, card, bad(card), bad(card), bad(card)]
        r = th._bookend_drift(card, frames, [icon])
        assert r["ok"] is False and word in r["reason"], (name, r)


def test_a_new_hue_at_the_same_brightness_fails():
    """Grayscale alone misses it: a blue card turned red at the same light."""
    card = Image.new("RGB", (360, 640), (110, 130, 170))

    def tinted(im):
        a = np.asarray(im).astype(int) + np.array([12, -5, -1])  # about the same gray
        return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))

    r = th._bookend_drift(card, [card, tinted(card), tinted(card)], [])
    assert r["ok"] is False and "colours" in r["reason"] and r["shift"] < th.DRIFT_SHIFT_MAX


def test_a_failed_pixel_check_replaces_the_clip_without_a_text_read(benv, tmp_path, monkeypatch):
    card = benv.pick("asset", 331)
    clip = tmp_path / "anim-610.mp4"
    clip.write_bytes(b"veo")
    seen: dict = {}

    def drift(card_path, clip_path, boxes):
        seen["boxes"] = boxes
        return {"ok": False, "reason": "faint marks appeared on the card's background (1.4% of it, frame 2)"}

    monkeypatch.setattr(th, "_bookend_clip_drift", drift)
    benv.db.add_run(860, _bookend_md())
    benv.anims[610] = {"id": 610, "status": "generating", "veo_operation_name": "op", "local_path": None,
                       "storage_bucket": None, "storage_path": None, "duration_sec": 4, "with_audio": False,
                       "cost_usd": 0.4, "error": None,
                       "metadata": {"parent_talking_head_id": 860, "role": "outro", "bookend_check": True,
                                    "bookend_motion": {"mode": "veo", "elements": [
                                        {"what": "rocket icon", "box": [100, 100, 200, 200]}]},
                                    "bookend_card": {"card_id": None, "path": str(card), "kept": True,
                                                     "aspect": "9:16"}}}
    rec = asyncio.run(th.check_bookend_clip(610, clip))
    assert rec["ok"] is False and rec["fallback"] == "slow_zoom" and "marks" in rec["reason"]
    assert rec["pixels"]["ok"] is False and seen["boxes"] == [[100, 100, 200, 200]]
    assert benv.gem.of(thp.BOOKEND_TEXT_CHECK_SYSTEM) == [], "no paid read once the pixels failed"
    assert benv.zooms[-1]["out"] == clip
    assert benv.db.md(860)["outro_animation_style"] == "slow_zoom"


# ---------------------------------------------------------------------------
# T8: a swapped headline must fit (run 830's intro: card 832 from card 825)
# ---------------------------------------------------------------------------

C832 = (1026, 1824)
# Card 825 (1024x1024) sits centred on the padded 9:16 card.
CONTENT_832 = (1, 400, 1025, 1424)
SRC_LINES = th._card_lines_from({"lines": [
    {"text": "42 Abu Dhabi", "kind": "logo", "box_2d": [265, 404, 289, 595]},
    {"text": "DECODE YOUR", "kind": "headline", "box_2d": [376, 257, 424, 742]},
    {"text": "TECH FUTURE", "kind": "headline", "box_2d": [431, 275, 479, 724]},
    {"text": "Start your journey in tech today", "kind": "subtitle", "box_2d": [492, 286, 519, 713]},
]})
# Card 832 as the read-back saw it: the right words, the L of LEARNING cut at
# the left and the S faded at the right.
READ_832 = th._card_lines_from({"lines": [
    {"text": "42 Abu Dhabi", "kind": "logo", "box_2d": [265, 403, 289, 596]},
    {"text": "WHY PEER", "kind": "headline", "box_2d": [375, 325, 420, 674]},
    {"text": "LEARNING WORKS", "kind": "headline", "box_2d": [428, 202, 476, 785]},
    {"text": "No teachers, just collaboration", "kind": "subtitle", "box_2d": [497, 294, 517, 705]},
]})


def _plan_830():
    return th._text_swap_plan(SRC_LINES, "Why Peer Learning Works", "No teachers, just collaboration")


def test_run_830s_headline_reads_right_but_does_not_fit():
    plan = _plan_830()
    assert th._swap_verdict(SRC_LINES, plan, READ_832)[0] is True, "the read-back passed it"
    zones = th._swap_zone_rects(C832, SRC_LINES, plan, CONTENT_832)
    safe = th._swap_safe_rects(C832, zones)
    ok, why = th._swap_fits(C832, SRC_LINES, plan, READ_832, safe)
    assert ok is False and "LEARNING WORKS" in why and "ran outside" in why
    # The same words inside the old words' box fit.
    fitted = [dict(ln) for ln in READ_832]
    fitted[2]["box"] = [428, 262, 476, 738]
    assert th._swap_fits(C832, SRC_LINES, plan, fitted, safe) == (True, "the new words fit inside the text area")
    # A new line the reader gave no box for cannot be checked: no.
    blind = [dict(ln) for ln in READ_832]
    blind[1]["box"] = None
    assert th._swap_fits(C832, SRC_LINES, plan, blind, safe)[0] is False


def test_the_zone_is_never_wider_than_the_old_words_and_stays_on_the_picture():
    plan = _plan_830()
    longer = [dict(p, new=p["new"] + " and much, much more than that") for p in plan]
    zones = th._swap_zone_rects(C832, SRC_LINES, longer, CONTENT_832)
    head = th._swap_text_boxes(C832, SRC_LINES, longer)[0]
    line_h = (head[3] - head[1]) / head[4]
    assert zones[0][0] >= head[0] - 0.5 * line_h - 1 and zones[0][2] <= head[2] + 0.5 * line_h + 1, \
        "longer words do not widen the zone"
    assert zones == th._swap_zone_rects(C832, SRC_LINES, plan, CONTENT_832)
    # A line near the picture's edge: its zone stops at the picture.
    edge = th._card_lines_from({"lines": [
        {"text": "DECODE YOUR TECH FUTURE", "kind": "headline", "box_2d": [380, 2, 420, 998]}]})
    p = th._text_swap_plan(edge, "Why Peer Learning Works", "")
    z, = th._swap_zone_rects(C832, edge, p, (60, 400, 966, 1424))
    assert z[0] >= 60 and z[2] <= 966
    alpha, zone = th._text_swap_mask(C832, edge, p, (60, 400, 966, 1424))
    assert zone.getbbox()[0] >= 60 and zone.getbbox()[2] <= 966


def test_the_swap_directive_gives_the_box_and_the_line_count():
    plan = _plan_830()
    fit = th._swap_fit_spec(C832, SRC_LINES, plan)
    d = thp.build_card_text_swap_directive(plan, aspect="9:16", fit=fit)
    assert "FIT: the card is 1026 x 1824 pixels." in d
    x0, y0, x1, y1, n = th._swap_text_boxes(C832, SRC_LINES, plan)[0]
    assert f"The headline sits in the box x {round(x0)} to {round(x1)}" in d and "on 2 lines" in d
    assert "never wider than the box" in d and "may wrap onto 2 lines" in d
    assert "ran outside its box" not in d
    tight = thp.build_card_text_swap_directive(plan, aspect="9:16", fit=fit, tighter=True)
    assert "ran outside its box" in tight and "about 80%" in tight
    # Its headline has two lines, its subtitle one: only the headline may wrap.
    assert "two lines only in a box of more than one line" in tight
    assert "on 1 line. " in d and "on ONE line" in d
    assert chr(0x2014) not in tight


def test_a_one_line_headline_is_never_asked_to_wrap():
    # One line on a 1080 x 1920 card: its zone's safe area is the line plus
    # room for the reader's boxes (a third of a line above and below, round
    # 3), never a second line of its size: the words keep their line's place.
    size = (1080, 1920)
    one = th._card_lines_from({"lines": [
        {"text": "DECODE YOUR TECH FUTURE", "kind": "headline", "box_2d": [312, 150, 354, 850]}]})
    plan = th._text_swap_plan(one, "Why Peer Learning Works For You", "")
    sx0, sy0, sx1, sy1 = th._swap_safe_rects(size, th._swap_zone_rects(size, one, plan))[0]
    line_h = (354 - 312) / 1000 * 1920
    assert sy1 - sy0 < 2 * line_h, "no room for a second line"
    fit = th._swap_fit_spec(size, one, plan)
    d = thp.build_card_text_swap_directive(plan, aspect="9:16", fit=fit)
    assert "on 1 line." in d and "on ONE line" in d and "may wrap" not in d
    tight = thp.build_card_text_swap_directive(plan, aspect="9:16", fit=fit, tighter=True)
    assert "about 80%" in tight and "keep a one-line box's words on one line" in tight
    assert "two lines" not in tight and chr(0x2014) not in tight


def _spill(head: str = NEW.upper()) -> dict:
    """A read-back with the new headline wider than its zone."""
    return _read(LOGO, {**OLD_HEAD, "text": head, "box_2d": [312, 8, 407, 992]}, URL)


def test_a_headline_that_spills_is_made_tighter_then_the_card_keeps_its_words(senv):
    card = _card_picture(seed=21)
    src = _prev_run(senv, 740, picture=card)
    _new_run(senv, 840)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL), _spill(), _spill()]
    asyncio.run(th.generate_previews(840, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 840)
    first, second = senv.edit["calls"]
    assert "FIT: the card is 720 x 1280 pixels." in first["parts"][-1][1]
    assert "ran outside its box" not in first["parts"][-1][1]
    assert "ran outside its box" in second["parts"][-1][1], "the second attempt is tighter"
    assert "Check every letter" not in second["parts"][-1][1], "its words were right"
    assert _img(row).tobytes() == card.tobytes(), "never clipped letters: the original card"
    swap = row["metadata"]["text_swap"]
    assert swap["status"] == "failed" and "ran outside" in swap["warning"]


def test_a_tighter_second_attempt_that_fits_is_used(senv):
    src = _prev_run(senv, 741, picture=_card_picture(seed=22))
    _new_run(senv, 841)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL), _spill(), _read(LOGO, {**OLD_HEAD, "text": NEW.upper()}, URL)]
    asyncio.run(th.generate_previews(841, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 841)
    assert len(senv.edit["calls"]) == 2 and row["metadata"]["text_swap"]["status"] == "swapped"
