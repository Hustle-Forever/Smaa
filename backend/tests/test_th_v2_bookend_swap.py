"""TH v2 intro / outro (#63 part 1, Part B): previous reels' cards, and a
kept card's words swapped only when needed.

- "Previous videos": every earlier run's CURRENT intro/outro card can be
  picked (refs.{role}_card_id); the list gives its thumbnail URL, text, shape
  and the finished clip that is reused when the card is copied as it is (same
  shape, words unchanged): the run gets its own copy of that MP4, no Veo.
- A kept card is read once (Gemini Flash: each line's text, kind and box),
  cached on the card. Logo / URL / CTA lines are never changed. A headline or
  subtitle that differs from the script is swapped by ONE image edit
  (card_animation, the run's aspect forced) pasted back through a feathered
  mask of the dilated text zone, so every pixel outside it is the card's own;
  the result is read back, tried once more on a mismatch, then the original
  is kept with a warning.
- "Keep card text": no swap, and the script takes the card's words. An edit
  never pays for the swap: it drops the card, which the Stills step (or the
  reel's start) makes with the swap. A kept bookend gets no anti-repetition.

No database and no paid call: the in-memory FakePg and fake models of
test_th_v2_scene_stills / test_th_v2_bookends; an unexpected model call fails.
"""
from __future__ import annotations

import asyncio
import copy
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp
from test_th_v2_bookends import LOGO_ONLY, REAL_FETCH_RUN, _bookend_md, benv  # noqa: F401
from test_th_v2_scene_stills import _norm, _scene, env  # noqa: F401

W, H = 720, 1280

# The card's lines as the reader gives them (box_2d on 0..1000).
LOGO = {"text": "42 ABU DHABI", "kind": "logo", "box_2d": [78, 300, 141, 700]}
URL = {"text": "42abudhabi.ae", "kind": "url", "box_2d": [900, 250, 940, 750]}
OLD_HEAD = {"text": "Decode Your Tech Future", "kind": "headline", "box_2d": [312, 100, 407, 900]}
NEW = "Learn from your peers"


def _read(*lines) -> dict:
    return {"lines": [dict(ln) for ln in lines]}


def _readback(head: str = NEW.upper()) -> dict:
    return _read(LOGO, {**OLD_HEAD, "text": head}, URL)


def _card_picture(seed: int = 3) -> Image.Image:
    """A card whose every pixel matters: a gradient, a logo block, a headline
    band and a URL band."""
    im = Image.new("RGB", (W, H))
    px = im.load()
    for y in range(H):
        for x in range(W):
            px[x, y] = ((x * 3 + seed * 17) % 256, (y * 2 + seed * 9) % 256, ((x + y) + seed) % 256)
    im.paste((250, 250, 250), (216, 100, 504, 180))     # the logo
    im.paste((240, 200, 20), (72, 400, 648, 520))       # the headline
    im.paste((200, 200, 200), (180, 1152, 540, 1204))   # the URL
    return im


def _png(im: Image.Image) -> bytes:
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixture: the run store with script patches, clip lookups and the card list
# ---------------------------------------------------------------------------

@pytest.fixture
def senv(benv, monkeypatch):
    db = benv.db
    base = db.run_sql
    script_writes: list[dict] = []

    def run_sql(sql, p):
        s = _norm(sql)
        if s.startswith("UPDATE yas_generations SET metadata = jsonb_set(") and "'{script}'" in s:
            fields, rid = p
            assert "%s::jsonb" in s and "COALESCE(metadata->'script'" in s, "one atomic statement"
            md = db.rows[rid]["metadata"]
            md["script"] = {**(md.get("script") or {}), **copy.deepcopy(fields.obj)}
            script_writes.append({"run": rid, **copy.deepcopy(fields.obj)})
            return None
        if s.startswith("SELECT id, aspect_ratio, veo_model, duration_sec, local_path, metadata "
                        "FROM yas_animations WHERE status = %s AND metadata->'bookend_card'->>'card_id' = ANY(%s)"):
            status, ids = p
            out = []
            for a in sorted(benv.anims.values(), key=lambda a: -a["id"]):
                cid = ((a.get("metadata") or {}).get("bookend_card") or {}).get("card_id")
                if a.get("status") == status and str(cid) in ids:
                    out.append(copy.deepcopy({k: a.get(k) for k in (
                        "id", "aspect_ratio", "veo_model", "duration_sec", "local_path", "metadata")}))
            return out
        if s.startswith("SELECT c.id, c.local_path, c.width, c.height, c.created_at, c.metadata, "
                        "r.id AS run_id, r.topic AS run_topic FROM yas_generations c JOIN yas_generations r"):
            assert "r.metadata->>(%s::text) = c.id::text" in s
            run_mode, key, mode, status, ex1, ex2, lim = p
            out = []
            for c in sorted(db.rows.values(), key=lambda r: -r["id"]):
                cmd = c.get("metadata") or {}
                if c.get("mode") != mode or c.get("status") != status or not c.get("local_path"):
                    continue
                if (cmd.get("category") or "talking_head_card") != "talking_head_card":
                    continue
                r = db.rows.get(cmd.get("parent_talking_head_id"))
                if not r or r.get("mode") != run_mode or (r.get("metadata") or {}).get(key) != c["id"]:
                    continue
                if ex1 is not None and r["id"] == ex1:
                    continue
                out.append({"id": c["id"], "local_path": c["local_path"], "width": c.get("width"),
                            "height": c.get("height"), "created_at": None,
                            "metadata": copy.deepcopy(cmd), "run_id": r["id"], "run_topic": r.get("topic")})
            return out[:lim]
        return base(sql, p)

    monkeypatch.setattr(db, "run_sql", run_sql)
    benv.script_writes = script_writes
    return benv


def _prev_run(env, run_id: int, role: str = "intro", *, picture: Image.Image | None = None,
              current: bool = True, **card_md) -> int:
    """An earlier run whose current `role` card is `picture` (a drawn card
    unless card_md says otherwise). Returns the card id."""
    if run_id not in env.db.rows:
        env.db.add_run(run_id, _bookend_md(), topic=f"Earlier reel {run_id}")
    extra = {"source_ref": None, "as_is": False, "ref_mode": None, "card_text": "Decode Your Tech Future",
             "card_text_source": "planned", "aspect": "9:16", **card_md}
    cid = asyncio.run(th._insert_card_row(
        parent_run_id=run_id, topic="t", image_model="gemini_pro",
        card_mode=th.MODE_INTRO if role == "intro" else th.MODE_OUTRO,
        image_bytes=_png(picture or _card_picture()), mime="image/png", used_prompt="[new_vibe]\nx",
        card_type=role, extra_md=extra,
    ))
    if current:
        env.db.md(run_id)[f"{role}_image_id"] = cid
    return cid


def _clip(env, anim_id: int, card_id: int, *, aspect="9:16", status="ready", **meta) -> Path:
    """A finished bookend clip of `card_id` (an MP4 on disk)."""
    path = env.tmp / "clips" / f"anim-{anim_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MP4-OF-CARD-" + str(card_id).encode())
    env.anims[anim_id] = {
        "id": anim_id, "status": status, "veo_operation_name": None, "veo_model": th.BOOKEND_ZOOM_MODEL,
        "duration_sec": 4, "with_audio": False, "aspect_ratio": aspect, "cost_usd": 0.0, "error": None,
        "local_path": str(path), "storage_bucket": None, "storage_path": None,
        "metadata": {"parent_talking_head_id": 1, "role": "intro",
                     "bookend_card": {"card_id": card_id, "path": "x", "kept": False, "aspect": aspect},
                     "bookend_motion": th._bookend_plan_from(LOGO_ONLY), **meta},
    }
    return path


def _new_run(env, run_id: int, *, refs: dict | None = None, **md) -> None:
    base = _bookend_md(outro_excluded=True, refs=dict(refs or {}), **md)
    base["script"]["intro_headline"] = NEW
    env.db.add_run(run_id, base)


def _card_of(env, run_id: int, role: str = "intro") -> dict:
    return env.db.rows[env.db.md(run_id)[f"{role}_image_id"]]


def _img(row: dict) -> Image.Image:
    return Image.open(Path(row["local_path"])).convert("RGB")


def _reads(env) -> list[dict]:
    return env.gem.of(thp.CARD_TEXT_READ_SYSTEM)


# ---------------------------------------------------------------------------
# The read, the plan, the verdict (pure)
# ---------------------------------------------------------------------------

def test_a_read_is_cleaned_into_lines():
    got = th._card_lines_from({"lines": [
        {"text": "  Decode   Your Tech Future ", "kind": "headline", "box_2d": [312, 100, 407, 900]},
        {"text": "", "kind": "logo", "box_2d": [1, 2, 3, 4]},
        {"text": "small print", "kind": "banner", "box_2d": [500, 900, 400, 100]},
        {"text": "x", "kind": "cta", "box_2d": [10, 10, 20]},
        {"text": "y", "kind": "url", "box_2d": [-5, 20, 1200, 30.6]},
        "junk",
    ]})
    assert got == [
        {"text": "Decode Your Tech Future", "kind": "headline", "box": [312, 100, 407, 900]},
        {"text": "small print", "kind": "other", "box": None},
        {"text": "x", "kind": "cta", "box": None},
        {"text": "y", "kind": "url", "box": [0, 20, 1000, 31]},
    ]
    assert th._card_lines_from({"lines": []}) == [], "a card with no text"
    assert th._card_lines_from(None) is None and th._card_lines_from({"nope": 1}) is None


def test_only_a_headline_or_subtitle_that_differs_is_swapped():
    lines = th._card_lines_from(_read(LOGO, OLD_HEAD, URL,
                                      {"text": "Join 42", "kind": "cta", "box_2d": [800, 300, 840, 700]}))
    plan = th._text_swap_plan(lines, NEW, "")
    assert plan == [{"kind": "headline", "lines": [1], "old": "Decode Your Tech Future", "new": NEW}]
    # The same words in another case or punctuation: nothing to do.
    assert th._text_swap_plan(lines, "DECODE your tech future!", "") == []
    # Logo, URL and CTA lines are never swapped, whatever the script says.
    logo_only = th._card_lines_from(_read(LOGO, URL))
    assert th._text_swap_plan(logo_only, "Apply Today", "42abudhabi.ae") == []
    # A subtitle is swapped only when the script has one.
    two = th._card_lines_from(_read(OLD_HEAD, {"text": "at 42 Abu Dhabi", "kind": "subtitle",
                                               "box_2d": [420, 200, 450, 800]}))
    assert [p["kind"] for p in th._text_swap_plan(two, NEW, "")] == ["headline"]
    assert [p["kind"] for p in th._text_swap_plan(two, NEW, "with your peers")] == ["headline", "subtitle"]
    # A headline on two lines is one group.
    split = th._card_lines_from(_read({**OLD_HEAD, "text": "Decode Your"},
                                      {**OLD_HEAD, "text": "Tech Future", "box_2d": [410, 100, 500, 900]}))
    p, = th._text_swap_plan(split, NEW, "")
    assert p["lines"] == [0, 1] and p["old"] == "Decode Your Tech Future"


def test_the_words_a_kept_card_gives_the_script():
    lines = th._card_lines_from(_read(LOGO, OLD_HEAD, URL))
    assert th._card_script_words(lines, "intro") == {"headline": "Decode Your Tech Future"}
    assert th._card_script_words(th._card_lines_from(_read(LOGO, URL)), "intro") == {}, "logo-only: none"
    cta = th._card_lines_from(_read(LOGO, {"text": "Apply now", "kind": "cta", "box_2d": [1, 1, 2, 2]}))
    assert th._card_script_words(cta, "outro") == {"headline": "Apply now"}, "an outro's CTA is its headline"
    assert th._card_script_words(cta, "intro") == {}
    md = {"script": {"intro_headline": "decode your TECH future."}}
    assert th._script_fields_from_card(md, "intro", {"headline": "Decode Your Tech Future"}) == {}, \
        "the same words are not rewritten"
    assert th._script_fields_from_card(md, "intro", {"headline": "Other"}) == {"intro_headline": "Other"}


def test_the_read_back_verdict():
    before = th._card_lines_from(_read(LOGO, OLD_HEAD, URL))
    plan = th._text_swap_plan(before, NEW, "")
    ok = th._card_lines_from(_readback())
    assert th._swap_verdict(before, plan, ok) == (True, "the new words read back exactly")
    good, why = th._swap_verdict(before, plan, th._card_lines_from(_readback("LEARN FROM YOUR PEARS")))
    assert not good and "peers" in why
    good, why = th._swap_verdict(before, plan, th._card_lines_from(_readback("Learn from your peers future")))
    assert not good and "future" in why, "an old word left behind"
    no_logo = th._card_lines_from(_read({**OLD_HEAD, "text": NEW}, URL))
    good, why = th._swap_verdict(before, plan, no_logo)
    assert not good and "abu" in why, "a kept line lost"
    assert th._swap_verdict(before, plan, None)[0] is False


# ---------------------------------------------------------------------------
# The mask: every pixel outside the dilated text zone is the card's own
# ---------------------------------------------------------------------------

def test_the_mask_covers_the_dilated_text_zone_and_nothing_else():
    lines = th._card_lines_from(_read(LOGO, OLD_HEAD, URL))
    plan = th._text_swap_plan(lines, NEW, "")
    alpha, zone = th._text_swap_mask((W, H), lines, plan)
    # The headline box is 72..648 x 399..521; a line is 122 px: half a line
    # at the sides (61 px); above and below the feathered edge (16 px) plus a
    # third of a line (42.7 px) of room for the new words (round 3, U2).
    l, t, r, b = zone.getbbox()
    assert (l, t) == (11, 340) and r in (709, 710) and b in (580, 581)
    assert alpha.getbbox() and alpha.getbbox()[1] >= t and alpha.getbbox()[3] <= b
    a = alpha.load()
    z = zone.load()
    assert a[360, 460] == 255, "full strength in the middle of the zone"
    assert 0 < a[14, 460] < 255, "feathered at its edge"
    for x, y in ((360, 140), (360, 1180), (5, 460), (715, 460), (360, 300), (360, 600)):
        assert a[x, y] == 0 and z[x, y] == 0, (x, y)
    # alpha is never above 0 outside the zone.
    assert all(av == 0 for av, zv in zip(alpha.getdata(), zone.getdata()) if zv == 0)


def test_a_line_kept_inside_the_band_is_cut_out_of_the_zone():
    beside = {"text": "42", "kind": "logo", "box_2d": [330, 820, 390, 980]}
    head = {**OLD_HEAD, "box_2d": [312, 100, 407, 700]}
    lines = th._card_lines_from(_read(head, beside))
    plan = th._text_swap_plan(lines, "Learn from your peers today and every day", "")
    alpha, zone = th._text_swap_mask((W, H), lines, plan)
    # The longer words widen the zone over the logo's columns, but the logo
    # (and a margin round it) stays out.
    assert zone.getpixel((560, 460)) == 255
    for x in range(int(820 * W / 1000), int(980 * W / 1000)):
        assert zone.getpixel((x, 460)) == 0 and alpha.getpixel((x, 460)) == 0


def test_no_mask_when_a_line_to_change_has_no_box():
    lines = th._card_lines_from(_read({**OLD_HEAD, "box_2d": []}, LOGO))
    assert th._text_swap_mask((W, H), lines, th._text_swap_plan(lines, NEW, "")) is None


def test_the_paste_back_keeps_every_pixel_outside_the_zone():
    card = _card_picture()
    lines = th._card_lines_from(_read(LOGO, OLD_HEAD, URL))
    plan = th._text_swap_plan(lines, NEW, "")
    alpha, zone = th._text_swap_mask((W, H), lines, plan)
    edited = _png(Image.new("RGB", (768, 1344), (10, 20, 30)))   # the model's size for 9:16
    out = Image.open(BytesIO(th._composite_swap(card, edited, alpha))).convert("RGB")
    assert out.size == (W, H)
    same = sum(1 for o, c, z in zip(out.getdata(), card.getdata(), zone.getdata()) if z == 0 and o == c)
    outside = sum(1 for z in zone.getdata() if z == 0)
    assert same == outside, "every pixel outside the zone is the card's own, exactly"
    assert out.getpixel((360, 460)) == (10, 20, 30), "inside, the edit"


# ---------------------------------------------------------------------------
# A kept card's words: swapped when needed, else kept
# ---------------------------------------------------------------------------

def test_a_previous_reels_card_gets_the_new_headline_swapped_in(senv):
    card = _card_picture()
    src = _prev_run(senv, 700, picture=card)
    _new_run(senv, 801)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL), _readback()]
    asyncio.run(th.generate_previews(801, look={"refs": {"intro_card_id": src}}))
    md = senv.db.md(801)
    row = _card_of(senv, 801)
    cmd = row["metadata"]
    assert cmd["source_ref"] == {"card_id": src} and cmd["as_is"] is True and md["intro_error"] is None
    assert cmd["text_swap"] == {"status": "swapped", "warning": None, "kept": [], "swapped": [
        {"kind": "headline", "old": "Decode Your Tech Future", "new": NEW}]}
    assert cmd["card_text"] == f"42 ABU DHABI\n{NEW.upper()}\n42abudhabi.ae"
    assert cmd["card_text_source"] == "detected"
    # ONE image edit, through card_animation, the aspect forced, no logo part.
    call, = senv.edit["calls"]
    assert call["system"] == thp.CARD_TEXT_SWAP_SYSTEM and call["aspect"] == "9:16"
    assert [k for k, _ in call["parts"]] == ["text", "image", "text"]
    directive = call["parts"][-1][1]
    assert '"Decode Your Tech Future" becomes exactly "Learn from your peers"' in directive
    assert "Change nothing else" in directive and "logo" in directive
    # Pixels outside the dilated headline zone are the card's own; inside, the edit.
    out = _img(row)
    lines = th._card_lines_from(_read(LOGO, OLD_HEAD, URL))
    _alpha, zone = th._text_swap_mask((W, H), lines, th._text_swap_plan(lines, NEW, ""))
    assert all(o == c for o, c, z in zip(out.getdata(), card.getdata(), zone.getdata()) if z == 0)
    assert out.crop((0, 0, W, 330)).tobytes() == card.crop((0, 0, W, 330)).tobytes(), "the logo, untouched"
    assert out.getpixel((360, 460)) == (10, 20, 30)
    # The source card was read once and that read is cached on it; the
    # script keeps its words (they are on the card now).
    assert senv.db.rows[src]["metadata"]["text_lines"] == th._card_lines_from(_read(LOGO, OLD_HEAD, URL))
    assert md["script"]["intro_headline"] == NEW and senv.script_writes == []
    # A swapped card is new pixels: no previous clip comes with it.
    assert not md.get("intro_animation_id")


def test_a_logo_only_card_is_never_edited(senv):
    card = _card_picture(seed=5)
    src = _prev_run(senv, 702, picture=card)
    _new_run(senv, 802)
    senv.gem.reads = [_read(LOGO, URL)]
    asyncio.run(th.generate_previews(802, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 802)
    assert senv.edit["calls"] == [] and _img(row).tobytes() == card.tobytes()
    assert row["metadata"]["text_swap"]["status"] == "unchanged"
    assert senv.db.md(802)["script"]["intro_headline"] == NEW and senv.script_writes == []


def test_keep_card_text_never_edits_and_the_script_takes_the_cards_words(senv):
    card = _card_picture(seed=6)
    src = _prev_run(senv, 703, picture=card)
    _new_run(senv, 803)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL)]
    asyncio.run(th.generate_previews(803, look={"refs": {"intro_card_id": src, "intro_keep_text": True}}))
    row = _card_of(senv, 803)
    assert senv.edit["calls"] == [] and _img(row).tobytes() == card.tobytes()
    assert row["metadata"]["keep_text"] is True and row["metadata"]["text_swap"]["status"] == "kept"
    assert senv.db.md(803)["script"]["intro_headline"] == "Decode Your Tech Future"
    assert senv.script_writes == [{"run": 803, "intro_headline": "Decode Your Tech Future"}]
    assert senv.db.md(803)["script"]["outro_headline"] == "Apply Today", "only the intro's fields"


def test_a_read_back_mismatch_is_tried_once_more_then_the_original_is_kept(senv):
    card = _card_picture(seed=7)
    src = _prev_run(senv, 704, picture=card)
    _new_run(senv, 804)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL), _readback("LEARN FROM YOUR PEARS"),
                      _readback("LEARN FRM YOUR PEERS")]
    asyncio.run(th.generate_previews(804, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 804)
    assert len(senv.edit["calls"]) == 2, "one retry, never more"
    assert "Check every letter" in senv.edit["calls"][1]["parts"][-1][1]
    assert "Check every letter" not in senv.edit["calls"][0]["parts"][-1][1]
    assert _img(row).tobytes() == card.tobytes(), "the original card is kept"
    swap = row["metadata"]["text_swap"]
    assert swap["status"] == "failed" and "keeps its own words" in swap["warning"]
    assert row["metadata"]["card_text"] == "42 ABU DHABI\nDecode Your Tech Future\n42abudhabi.ae"
    # The script keeps the new words, so making the card again tries again.
    assert senv.db.md(804)["script"]["intro_headline"] == NEW and senv.script_writes == []
    assert senv.db.md(804)["intro_error"] is None, "a kept card, not a failed one"


def test_a_second_attempt_that_reads_back_right_is_used(senv):
    src = _prev_run(senv, 705, picture=_card_picture(seed=8))
    _new_run(senv, 805)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL), _readback("LEARN FROM YOUR PEARS"), _readback()]
    asyncio.run(th.generate_previews(805, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 805)
    assert len(senv.edit["calls"]) == 2 and row["metadata"]["text_swap"]["status"] == "swapped"
    assert _img(row).getpixel((360, 460)) == (10, 20, 30)


def test_an_edit_that_fails_outright_is_not_asked_again(senv, monkeypatch):
    from app.services import card_animation as ca

    calls: list = []

    async def refuse(parts, **kw):
        calls.append(parts)
        raise RuntimeError("RESOURCE_EXHAUSTED")

    monkeypatch.setattr(ca, "render_card_image", refuse)
    card = _card_picture(seed=14)
    src = _prev_run(senv, 708, picture=card)
    _new_run(senv, 816)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL)]
    asyncio.run(th.generate_previews(816, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 816)
    assert len(calls) == 1 and _img(row).tobytes() == card.tobytes()
    assert row["metadata"]["text_swap"]["status"] == "failed"
    assert "RuntimeError" in row["metadata"]["text_swap"]["warning"]
    assert len(_reads(senv)) == 1, "nothing to read back"


def test_an_edit_of_another_shape_is_never_stretched_onto_the_card(senv):
    card = _card_picture(seed=15)
    src = _prev_run(senv, 709, picture=card)
    _new_run(senv, 817)
    senv.edit["size"] = (1024, 1024)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL)]
    asyncio.run(th.generate_previews(817, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 817)
    assert len(senv.edit["calls"]) == 2 and _img(row).tobytes() == card.tobytes()
    assert "1024x1024" in row["metadata"]["text_swap"]["warning"]


def test_text_that_cannot_be_located_is_kept_without_an_edit(senv):
    card = _card_picture(seed=9)
    src = _prev_run(senv, 706, picture=card)
    _new_run(senv, 806)
    senv.gem.reads = [_read(LOGO, {**OLD_HEAD, "box_2d": [0, 0, 0, 0]})]
    asyncio.run(th.generate_previews(806, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 806)
    assert senv.edit["calls"] == [] and _img(row).tobytes() == card.tobytes()
    assert row["metadata"]["text_swap"]["status"] == "failed"
    assert "located" in row["metadata"]["text_swap"]["warning"]


def test_an_unreadable_card_is_kept_as_it_is(senv):
    card = _card_picture(seed=10)
    src = _prev_run(senv, 707, picture=card)
    _new_run(senv, 807)
    senv.gem.reads = [RuntimeError("vision down")]
    asyncio.run(th.generate_previews(807, look={"refs": {"intro_card_id": src}}))
    row = _card_of(senv, 807)
    assert senv.edit["calls"] == [] and _img(row).tobytes() == card.tobytes()
    assert row["metadata"]["text_swap"]["status"] == "unread"
    # The director still read its words (Part A's fallback).
    assert row["metadata"]["card_text"] == "42\nABU DHABI\nأبوظبي"


def test_an_uploaded_card_with_a_headline_is_swapped_too(senv):
    up = senv.pick("upload", 91, size=(W, H), seed=2)
    _new_run(senv, 808)
    senv.gem.reads = [_read(LOGO, OLD_HEAD), _read(LOGO, {**OLD_HEAD, "text": NEW})]
    asyncio.run(th.generate_previews(808, look={"refs": {"intro_upload_id": 91}}))
    row = _card_of(senv, 808)
    assert row["metadata"]["source_ref"] == {"upload_id": 91}
    assert row["metadata"]["text_swap"]["status"] == "swapped" and len(senv.edit["calls"]) == 1
    assert _img(row).crop((0, 0, W, 300)).tobytes() == \
        Image.open(up).convert("RGB").crop((0, 0, W, 300)).tobytes()


def test_a_drawn_card_is_never_read_or_swapped(senv):
    senv.pick("asset", 243)
    _new_run(senv, 809)
    asyncio.run(th.generate_previews(809, look={"refs": {"intro_asset_id": 243, "intro_ref_mode": "new_vibe"}}))
    assert _reads(senv) == [] and len(senv.edit["calls"]) == 1, "the drawing call only"
    assert "text_swap" not in _card_of(senv, 809)["metadata"]


# ---------------------------------------------------------------------------
# A previous reel's clip, reused when the card is copied as it is
# ---------------------------------------------------------------------------

def test_a_card_copied_as_it_is_reuses_its_finished_clip(senv):
    card = _card_picture(seed=11)
    plan = th._bookend_plan_from(LOGO_ONLY)
    src = _prev_run(senv, 710, picture=card, motion=plan,
                    text_lines=th._card_lines_from(_read(LOGO, URL)))
    clip = _clip(senv, 400, src, slow_zoom=True)
    _new_run(senv, 810)
    senv.db.rows[810]["local_path"] = senv.main_still(810)
    asyncio.run(th.generate_previews(810, look={"refs": {"intro_card_id": src}}))
    md = senv.db.md(810)
    row = _card_of(senv, 810)
    assert _img(row).tobytes() == card.tobytes()
    assert _reads(senv) == [], "the read cached on the previous card is used"
    assert senv.gem.of(thp.BOOKEND_MOTION_DIRECTOR_SYSTEM) == [], "its plan came with it"
    anim = senv.anims[md["intro_animation_id"]]
    assert anim["id"] != 400 and anim["status"] == "ready" and anim["cost_usd"] == 0.0
    assert anim["yas_generation_id"] == 810, "the run's own clip row"
    assert anim["metadata"]["reused_from"] == 400 and anim["metadata"]["bookend_card"]["card_id"] == row["id"]
    assert Path(anim["local_path"]).read_bytes() == clip.read_bytes()
    assert Path(anim["local_path"]) != clip, "a copy: deleting the earlier run keeps it"
    assert md["intro_video_stale"] is False and md["intro_animation_style"] == "slow_zoom"
    assert row["metadata"]["reused_clip_from"] == 400
    # Making the videos makes no new intro clip: no Veo card, no zoom.
    senv.db.rows[810]["local_path"] = senv.main_still(810)
    asyncio.run(th.start_videos(810))
    assert senv.veo["cards"] == [] and senv.zooms == []
    assert senv.db.md(810)["intro_animation_id"] == anim["id"]


def test_a_veo_clip_whose_read_back_held_is_reused_as_icons(senv):
    src = _prev_run(senv, 711, picture=_card_picture(seed=12),
                    text_lines=th._card_lines_from(_read(LOGO, OLD_HEAD)))
    _clip(senv, 401, src, bookend_text_check={"ok": True, "reason": "fine", "pixels": {"ok": True}})
    _new_run(senv, 811, refs={"intro_keep_text": True})
    asyncio.run(th.generate_previews(811, look={"refs": {"intro_card_id": src, "intro_keep_text": True}}))
    md = senv.db.md(811)
    assert senv.anims[md["intro_animation_id"]]["metadata"]["reused_from"] == 401
    assert md["intro_animation_style"] == "animate_icons"


def test_a_veo_clip_checked_by_its_text_only_is_never_reused(senv):
    """Anim 353 (the 42 logo frame) passed the text read with a grid of marks
    on its white and a longer divider: a Veo clip checked before the pixel
    check existed is made again, not copied."""
    src = _prev_run(senv, 714, picture=_card_picture(seed=16),
                    text_lines=th._card_lines_from(_read(LOGO, OLD_HEAD)))
    _clip(senv, 405, src, bookend_text_check={"ok": True, "reason": "the text and the look match the card"})
    _new_run(senv, 818, refs={"intro_keep_text": True})
    asyncio.run(th.generate_previews(818, look={"refs": {"intro_card_id": src, "intro_keep_text": True}}))
    assert not senv.db.md(818).get("intro_animation_id")
    assert th._clip_reusable({"bookend_text_check": {"ok": True}}) is False
    assert th._clip_reusable({"bookend_text_check": {"ok": True, "pixels": {"ok": False}}}) is False
    assert th._clip_reusable({"bookend_text_check": {"ok": True, "pixels": {"ok": True}}}) is True


def test_no_clip_is_reused_at_another_size_after_a_swap_or_when_unfaithful(senv):
    card = _card_picture(seed=13)
    src = _prev_run(senv, 712, picture=card, text_lines=th._card_lines_from(_read(LOGO, OLD_HEAD)))
    _clip(senv, 402, src, slow_zoom=True)
    # 16:9: the card is padded, so it is not the clip's picture.
    _new_run(senv, 812, aspect_ratio="16:9")
    senv.gem.reads = [_read(LOGO, OLD_HEAD)]
    asyncio.run(th.generate_previews(812, look={"refs": {"intro_card_id": src, "intro_keep_text": True}}))
    assert not senv.db.md(812).get("intro_animation_id")
    # Swapped: new pixels.
    _new_run(senv, 813)
    senv.gem.reads = [_read(LOGO, {**OLD_HEAD, "text": NEW})]
    asyncio.run(th.generate_previews(813, look={"refs": {"intro_card_id": src}}))
    assert _card_of(senv, 813)["metadata"]["text_swap"]["status"] == "swapped"
    assert not senv.db.md(813).get("intro_animation_id")
    # A clip whose read-back failed (and was not replaced), or a legacy clip.
    src2 = _prev_run(senv, 713, picture=card, text_lines=th._card_lines_from(_read(LOGO)))
    _clip(senv, 403, src2, bookend_text_check={"ok": False, "fallback": None})
    _clip(senv, 404, src2, status="failed", slow_zoom=True)
    _new_run(senv, 814)
    asyncio.run(th.generate_previews(814, look={"refs": {"intro_card_id": src2}}))
    assert not senv.db.md(814).get("intro_animation_id")
    assert th._clip_reusable({"bookend_text_check": {"ok": False, "fallback": "slow_zoom"}}) is True
    assert th._clip_reusable({"bookend_text_check": {"ok": None}}) is False
    assert th._clip_reusable({}) is False


def test_a_missing_previous_card_fails_the_tile(senv):
    _new_run(senv, 815)
    asyncio.run(th.generate_previews(815, look={"refs": {"intro_card_id": 999_999}}))
    md = senv.db.md(815)
    assert md["intro_image_id"] is None and "not an intro or outro card" in md["intro_error"]
    assert senv.edit["calls"] == []


# ---------------------------------------------------------------------------
# The "Previous videos" list
# ---------------------------------------------------------------------------

def test_the_list_gives_each_earlier_reels_current_card(senv):
    a = _prev_run(senv, 720, picture=_card_picture(seed=1))
    _prev_run(senv, 720, current=False, card_text="an older draft")            # replaced draft
    _clip(senv, 410, a, slow_zoom=True)
    senv.db.add_run(721, _bookend_md(), topic="Kept logo")
    senv.pick("asset", 243)
    kept_asset = _prev_run(senv, 721, as_is=True, source_ref={"asset_id": 243}, ref_mode="as_is")
    c = _prev_run(senv, 722, picture=_card_picture(seed=2), as_is=True, source_ref={"upload_id": 5},
                  ref_mode="as_is", card_text="Join 42")
    dup = _prev_run(senv, 723, picture=_card_picture(seed=2), as_is=True, source_ref={"upload_id": 5},
                    ref_mode="as_is", card_text="Join 42")
    swapped = _prev_run(senv, 724, as_is=True, source_ref={"asset_id": 243}, ref_mode="as_is",
                        text_swap={"status": "swapped"}, card_text=NEW)
    outro = _prev_run(senv, 725, role="outro")
    editor = asyncio.run(th._insert_card_row(
        parent_run_id=726, topic="t", image_model="gemini_pro", card_mode=th.MODE_INTRO,
        image_bytes=_png(_card_picture()), mime="image/png", used_prompt="x", card_type="intro",
        category="video_edit_bookend_card"))
    senv.db.add_run(726, _bookend_md(intro_image_id=editor))
    mine = _prev_run(senv, 727)
    items = asyncio.run(th.list_previous_cards("intro", exclude_run_id=727))
    ids = [it["card_id"] for it in items]
    assert ids == [swapped, dup, a], "newest first, the current card of each run once"
    assert kept_asset not in ids and c not in ids and outro not in ids and editor not in ids and mine not in ids
    first = items[-1]
    assert first == {
        "card_id": a, "role": "intro", "run_id": 720, "topic": "Earlier reel 720",
        "url": f"/api/yas-producer/talking-head-v2/cards/{a}/file",
        "card_text": "Decode Your Tech Future", "aspect": "9:16", "width": W, "height": H,
        "as_is": False, "clip_animation_id": 410, "clip_style": "slow_zoom", "created_at": None,
    }
    assert items[0]["clip_animation_id"] is None
    assert [it["card_id"] for it in asyncio.run(th.list_previous_cards("outro"))] == [outro]
    assert len(asyncio.run(th.list_previous_cards("intro", limit=1))) == 1
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.list_previous_cards("main"))
    assert e.value.status_code == 400


def test_the_list_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import yas_producer as router_mod

    seen: dict = {}

    async def fake_list(role, *, exclude_run_id=None, limit=30):
        seen.update(role=role, exclude=exclude_run_id, limit=limit)
        return [{"card_id": 1}]

    monkeypatch.setattr(router_mod.th_v2_service, "list_previous_cards", fake_list)
    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app)
    r = client.get("/api/yas-producer/talking-head-v2/cards/previous?role=outro&exclude_run_id=9&limit=500")
    assert r.status_code == 200 and r.json() == {"items": [{"card_id": 1}]}
    assert seen == {"role": "outro", "exclude": 9, "limit": 60}
    assert client.get("/api/yas-producer/talking-head-v2/cards/previous?role=scene").status_code == 422


# ---------------------------------------------------------------------------
# Edits: never a paid swap; the card follows its pick, its words and the toggle
# ---------------------------------------------------------------------------

def _stills(env, run_id: int, **kw) -> None:
    _new_run(env, run_id, **kw)
    env.db.rows[run_id]["local_path"] = env.main_still(run_id)


def _make_intro(env, run_id: int) -> int:
    """The Stills step's "Make card" for the intro, then its clip made (the
    flags a finished clip leaves). Returns the card id."""
    asyncio.run(th.retry_preview(run_id=run_id, which="intro", feedback_text=""))
    env.db.md(run_id)["intro_video_stale"] = False
    return env.db.md(run_id)["intro_image_id"]


def test_an_edit_picking_a_card_that_needs_a_swap_drops_it_for_the_stills_step(senv):
    src = _prev_run(senv, 730, picture=_card_picture(seed=3))
    _stills(senv, 830)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL)]
    asyncio.run(th.edit_run(run_id=830, refs_patch={"intro_card_id": src, "intro_asset_id": None,
                                                    "intro_ref_mode": "as_is"}))
    md = senv.db.md(830)
    assert md["intro_image_id"] is None and md["intro_video_stale"] is True and md["intro_error"] is None
    assert senv.edit["calls"] == [], "an edit never pays for the swap"
    # The Stills step's "Make card" makes it, with the swap, reading nothing twice.
    senv.gem.reads = [_readback()]
    asyncio.run(th.retry_preview(run_id=830, which="intro", feedback_text=""))
    md = senv.db.md(830)
    assert md["intro_image_id"] and len(senv.edit["calls"]) == 1
    assert _card_of(senv, 830)["metadata"]["text_swap"]["status"] == "swapped"
    assert len(_reads(senv)) == 2, "the source once (cached on its card), the read-back once"


def test_an_edit_picking_a_card_with_keep_card_text_makes_it_at_once(senv):
    card = _card_picture(seed=4)
    src = _prev_run(senv, 731, picture=card)
    _stills(senv, 831)
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL)]
    asyncio.run(th.edit_run(run_id=831, refs_patch={"intro_card_id": src, "intro_keep_text": True}))
    md = senv.db.md(831)
    assert md["intro_image_id"] and _img(_card_of(senv, 831)).tobytes() == card.tobytes()
    assert md["script"]["intro_headline"] == "Decode Your Tech Future"
    assert md["refs"]["intro_keep_text"] is True and senv.edit["calls"] == []


def test_toggling_keep_card_text_rebuilds_or_drops_the_kept_card(senv):
    card = _card_picture(seed=5)
    src = _prev_run(senv, 732, picture=card)
    _stills(senv, 832, refs={"intro_card_id": src, "intro_keep_text": True})
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL)]
    asyncio.run(th.retry_preview(run_id=832, which="intro", feedback_text=""))
    kept = senv.db.md(832)["intro_image_id"]
    assert senv.db.md(832)["script"]["intro_headline"] == "Decode Your Tech Future"
    # The script says a new headline, and the card should now say it: off.
    senv.db.md(832)["script"]["intro_headline"] = NEW
    asyncio.run(th.edit_run(run_id=832, refs_patch={"intro_keep_text": False}))
    md = senv.db.md(832)
    assert md["intro_image_id"] is None and md["intro_video_stale"] is True
    assert len(_reads(senv)) == 1, "the read cached on the card was reused"
    # Back on: made at once, free.
    asyncio.run(th.edit_run(run_id=832, refs_patch={"intro_keep_text": True}))
    md = senv.db.md(832)
    assert md["intro_image_id"] not in (None, kept) and senv.edit["calls"] == []
    assert md["script"]["intro_headline"] == "Decode Your Tech Future"


def test_keep_card_text_with_no_pick_changes_nothing(senv):
    _stills(senv, 833)
    voxel = _prev_run(senv, 833)
    asyncio.run(th.edit_run(run_id=833, refs_patch={"intro_keep_text": True}))
    md = senv.db.md(833)
    assert md["intro_image_id"] == voxel and not md.get("intro_video_stale")


def test_new_words_on_a_kept_card(senv):
    card = _card_picture(seed=6)
    src = _prev_run(senv, 734, picture=card)
    # Keep card text on: the card's words stay the script's, the card stays.
    _stills(senv, 834, refs={"intro_card_id": src, "intro_keep_text": True})
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL)]
    kept = _make_intro(senv, 834)
    asyncio.run(th.edit_run(run_id=834, intro_headline="Something else"))
    md = senv.db.md(834)
    assert md["intro_image_id"] == kept and not md.get("intro_video_stale")
    assert md["script"]["intro_headline"] == "Decode Your Tech Future"
    # Off: new words that differ from the card's headline drop it.
    _stills(senv, 835, refs={"intro_card_id": src})
    senv.gem.reads = [_readback()]
    swapped = _make_intro(senv, 835)
    assert _card_of(senv, 835)["metadata"]["text_swap"]["status"] == "swapped"
    asyncio.run(th.edit_run(run_id=835, intro_headline=NEW.lower() + "!"))
    assert senv.db.md(835)["intro_image_id"] == swapped, "the same words: nothing to do"
    asyncio.run(th.edit_run(run_id=835, intro_headline="Peers teach best"))
    md = senv.db.md(835)
    assert md["intro_image_id"] is None and md["intro_video_stale"] is True
    assert md["script"]["intro_headline"] == "Peers teach best"


def test_new_words_never_touch_a_logo_only_kept_card(senv):
    senv.pick("asset", 242)
    _stills(senv, 836, refs={"intro_asset_id": 242})
    kept = _make_intro(senv, 836)
    asyncio.run(th.edit_run(run_id=836, intro_headline="Anything at all"))
    md = senv.db.md(836)
    assert md["intro_image_id"] == kept and not md.get("intro_video_stale")
    assert md["script"]["intro_headline"] == "Anything at all", "a logo-only card has no headline"


def test_the_reels_start_makes_a_dropped_card_with_the_swap(senv):
    src = _prev_run(senv, 737, picture=_card_picture(seed=7))
    _stills(senv, 837, refs={"intro_card_id": src})
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL), _readback()]
    asyncio.run(th.start_videos(837))
    md = senv.db.md(837)
    assert md["intro_image_id"] and _card_of(senv, 837)["metadata"]["text_swap"]["status"] == "swapped"
    assert senv.zooms and senv.zooms[-1]["card"] == Path(_card_of(senv, 837)["local_path"])


def test_the_router_takes_the_new_ref_fields():
    from pydantic import ValidationError

    from app.routers.yas_producer import _TH_V2_REF_FIELDS, THV2EditRequest, THV2GeneratePreviewsRequest

    for k in ("intro_card_id", "outro_card_id", "intro_keep_text", "outro_keep_text"):
        assert k in _TH_V2_REF_FIELDS
    for model in (THV2EditRequest, THV2GeneratePreviewsRequest):
        body = model(intro_card_id=825, outro_keep_text=True)
        assert body.intro_card_id == 825 and body.outro_keep_text is True
        with pytest.raises(ValidationError):
            model(intro_keep_text="perhaps")
    assert th.TalkingHeadRefs(intro_card_id=825, intro_keep_text=True).to_dict()["intro_card_id"] == 825
    assert th.TalkingHeadRefs().to_dict()["outro_keep_text"] is False
    with pytest.raises(HTTPException) as e:
        th.TalkingHeadRefs(intro_keep_text="yes")
    assert e.value.status_code == 400


def test_an_edit_refuses_a_bad_keep_text_and_writes_nothing(senv):
    _stills(senv, 838)
    before = copy.deepcopy(senv.db.md(838))
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.edit_run(run_id=838, refs_patch={"intro_keep_text": "on"}))
    assert e.value.status_code == 400 and senv.db.md(838) == before
    with pytest.raises(HTTPException):
        asyncio.run(th._apply_look_patch(838, {"refs": {"outro_keep_text": 1}}))
    assert senv.db.md(838) == before


# ---------------------------------------------------------------------------
# Scripts: a kept bookend's words, and no anti-repetition for it
# ---------------------------------------------------------------------------

def test_a_kept_bookend_gets_no_anti_repetition():
    recent = [{"intro_headline": "Decode Your Tech Future", "outro_headline": "Apply Today"},
              {"intro_headline": "Peer power", "outro_headline": None}]
    assert th._kept_bookend_roles({"intro_card_id": 825}) == {"intro"}
    assert th._kept_bookend_roles({"intro_card_id": 825, "intro_ref_mode": "new_vibe"}) == set()
    assert th._kept_bookend_roles({"outro_asset_id": 242, "intro_video_upload_id": 4}) == {"outro"}
    got = th._recent_without_kept(recent, {"intro"})
    assert got == [{"intro_headline": None, "outro_headline": "Apply Today"},
                   {"intro_headline": None, "outro_headline": None}]
    assert th._recent_without_kept(recent, set()) is recent
    prompt = thp.build_script_user_prompt(
        topic="t", context_block="", character_brief=None, bg_brief=None, intro_brief=None,
        outro_brief=None, recent_bookends=got)
    assert "Decode Your Tech Future" not in prompt and "outro: Apply Today" in prompt


def test_a_rescript_leaves_the_kept_intro_out_of_the_anti_repetition(senv, monkeypatch):
    seen: dict = {}

    async def inputs(topic, user_dialogue=None):
        return "", None, [{"intro_headline": "Decode Your Tech Future", "outro_headline": "Apply Today"}]

    def user_prompt(**kw):
        seen["recent"] = kw["recent_bookends"]
        return "prompt"

    async def llm(prompt):
        return {"intro_headline": "x"}

    async def finalize(script, **kw):
        seen["kept_words"] = kw.get("kept_words")
        return script, {}

    monkeypatch.setattr(th, "_build_script_inputs", inputs)
    monkeypatch.setattr(thp, "build_script_user_prompt", user_prompt)
    monkeypatch.setattr(th, "_script_llm", llm)
    monkeypatch.setattr(th, "_finalize_script", finalize)
    asyncio.run(th._gpt_script_dict(topic="t", refs={"intro_card_id": 825}, scene_count=1, dlgs=[None],
                                    kept_words={"intro_headline": "Decode Your Tech Future"}))
    assert seen["recent"] == [{"intro_headline": None, "outro_headline": "Apply Today"}]
    assert seen["kept_words"] == {"intro_headline": "Decode Your Tech Future"}


def test_finalize_puts_the_kept_words_in_the_script(monkeypatch):
    async def speaker(script, **kw):
        return {"subject": "s", "gender": None, "source": "user"}

    monkeypatch.setattr(th, "_script_speaker", speaker)
    script, _ = asyncio.run(th._finalize_script(
        {"intro_headline": "Model words", "outro_headline": "Apply", "dialogue": "Hi there", "hook": "Hi"},
        topic="t", scene_count=1, dlgs=[None], has_character=True,
        kept_words={"intro_headline": "Decode Your Tech Future", "dialogue": "ignored"}))
    assert script["intro_headline"] == "Decode Your Tech Future" and script["outro_headline"] == "Apply"
    assert script["dialogue"] == "Hi there", "only bookend words"


def test_a_rescript_keeps_the_cards_words_or_drops_a_card_that_needs_new_ones(senv, monkeypatch):
    src = _prev_run(senv, 740, picture=_card_picture(seed=8))
    captured: dict = {}
    current = {"run": 840}

    async def gpt_script(**kw):
        # Stands in for the LLM: new words, then _finalize_script's kept words.
        captured.update(kw)
        script = dict(senv.db.md(current["run"])["script"], intro_headline="A brand new hook")
        script.update(kw.get("kept_words") or {})
        return script, None

    monkeypatch.setattr(th, "_gpt_script_dict", gpt_script)
    # Keep card text: the re-script says the card's words; the card stays.
    _stills(senv, 840, refs={"intro_card_id": src, "intro_keep_text": True})
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL)]
    kept = _make_intro(senv, 840)
    asyncio.run(th.rescript_run(840, new_topic="Why bootcamps work"))
    md = senv.db.md(840)
    assert captured["kept_words"] == {"intro_headline": "Decode Your Tech Future"}
    assert md["script"]["intro_headline"] == "Decode Your Tech Future" and md["intro_image_id"] == kept
    assert not md.get("intro_video_stale")
    # Off: the new headline differs from the swapped card's: it is dropped.
    current["run"] = 841
    _stills(senv, 841, refs={"intro_card_id": src})
    senv.gem.reads = [_readback()]
    _make_intro(senv, 841)
    asyncio.run(th.rescript_run(841, new_topic="Why bootcamps work"))
    md = senv.db.md(841)
    assert captured["kept_words"] == {}
    assert md["intro_image_id"] is None and md["intro_video_stale"] is True


# ---------------------------------------------------------------------------
# What the run says about each card
# ---------------------------------------------------------------------------

def test_the_run_says_how_a_kept_cards_words_came_out(senv):
    src = _prev_run(senv, 750, picture=_card_picture(seed=9))
    _stills(senv, 850, refs={"intro_card_id": src})
    senv.gem.reads = [_read(LOGO, OLD_HEAD, URL), _readback("X"), _readback("Y")]
    asyncio.run(th.retry_preview(run_id=850, which="intro", feedback_text=""))
    senv.db.rows[850]["updated_at"] = 0   # the fake's clock is not a timestamp
    run = asyncio.run(REAL_FETCH_RUN(850, with_reel=False))
    intro = run["intro"]
    assert intro["as_is"] is True and intro["keep_text"] is False and intro["source_card_id"] == src
    assert intro["text_swap"]["status"] == "failed" and intro["text_swap"]["warning"]
    assert intro["reused_clip"] is False and run["refs"]["intro_card_id"] == src
    # The pick shown while no card is made from it.
    senv.db.md(850)["intro_image_id"] = None
    intro = asyncio.run(REAL_FETCH_RUN(850, with_reel=False))["intro"]
    assert intro["card_missing"] is True and intro["source"] == "card" and intro["card_id"] == src
    assert intro["url"] == f"/api/yas-producer/talking-head-v2/cards/{src}/file"


def test_no_em_dash_in_the_swap_prompts():
    dash = chr(0x2014)
    texts = [thp.CARD_TEXT_READ_SYSTEM, thp.CARD_TEXT_SWAP_SYSTEM, thp.CARD_TEXT_READ_PROMPT,
             thp.build_card_text_swap_directive([{"kind": "headline", "old": "a", "new": "b"}], strict=True)]
    assert not any(dash in t for t in texts)
    src = Path(th.__file__).read_text(encoding="utf-8")
    start = src.index("class _SwapPending")
    end = src.index("async def _kept_script_words")
    assert dash not in src[start:end]
