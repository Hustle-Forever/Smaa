"""TH v2 intro / outro (#63 part 1, Part A): plumbing, keep-as-is, animation.

- A picked or uploaded intro/outro image is kept AS IT IS by default (ref
  mode 'as_is'): the card row is the image's own pixels (padded on a blurred
  fill of itself when the video is another shape), with no image model call.
  'new_vibe' ("New design in this style") still draws a new card. A mode that
  is not one of as_is / new_vibe / text_only is refused everywhere.
- Every card records what it was made from (source_ref). A card made from
  another image than the one picked now is never animated: run 824's cards
  were drawn before its intro was picked, then animated instead of it.
- Edits follow the picks: a new pick or mode rebuilds a kept card at once (a
  free copy) or drops a drawn one; new words drop a drawn card but leave a
  kept card (its own words) alone; a re-script does the same.
- The bookend motion director: icons on the card make a tight Veo prompt that
  animates only them, camera locked off, with hard no-text / no-restyle rules
  and no CTA claim (run 827 had Veo paint "Apply Today" under the logo); no
  icons means no Veo at all, the in-house slow zoom (1.00 to 1.06, the reel's
  size and frame rate). A Veo bookend is read back when it lands: changed text
  or look and the clip becomes the slow zoom.
- An uploaded intro/outro VIDEO is used on a multi-scene reel too.
- The video editor's bookends (render_card_animation_style, insert_card_row,
  insert_animation_row, build_intro/outro_veo_prompt) keep today's defaults.

No database and no paid call: the run store is the in-memory FakePg of
test_th_v2_scene_stills (plus yas_animations here); Gemini, Veo and OpenAI are
fakes, and an unexpected model call fails the test. ffmpeg (imageio-ffmpeg)
is real where a clip is actually rendered.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from app.services import asset_storage
from app.services import card_animation as ca
from app.services import th_reel
from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp
from app.services.yas_poster_prompts import STYLE_EXTRACT_SYSTEM
from test_th_v2_scene_stills import _md, _norm, _png_bytes, _scene, env  # noqa: F401

REAL_FETCH_RUN = th.fetch_run
REAL_RENDER_ZOOM = th._render_zoom_clip

LOGO_ONLY = {"has_animatable_elements": False, "elements": [], "motion_plan": "nothing to move",
             "card_text": ["42", "ABU DHABI", "أبوظبي"]}
ICONS = {"has_animatable_elements": True,
         "elements": [{"what": "rocket icon", "where": "top left", "motion": "float"},
                      {"what": "headline text", "where": "centre", "motion": "pop"},
                      {"what": "star", "where": "under the headline", "motion": "pulse"}],
         "motion_plan": "the rocket floats, the star pulses",
         "card_text": ["Decode Your Tech Future", "42 Abu Dhabi"]}


def _pattern(size, seed=1) -> Image.Image:
    """A picture whose every pixel matters: a gradient with a block in it."""
    w, h = size
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7 + seed * 40) % 256, (y * 5 + seed * 20) % 256, ((x + y) * 3 + seed) % 256)
    im.paste((250, 250, 250), (w // 4, h // 4, w // 2, h // 2))
    return im


# ---------------------------------------------------------------------------
# Fixture: the run store with yas_animations, and fake models
# ---------------------------------------------------------------------------

class FakeGemini:
    """gemini_multimodal: the motion director, the read-back, the style
    extract of a 'new_vibe' card and a kept card's text read (Part B). Anything
    else fails the test."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.plan: object = LOGO_ONLY
        self.check: object = None
        # Kept-card text reads, answered in order; once empty, the director's
        # card_text read as logo lines (nothing a swap would touch).
        self.reads: list = []

    async def __call__(self, prompt, *, images=None, system=None, json_schema=None, **kw):
        self.calls.append({"prompt": prompt, "system": system,
                           "images": [Path(i) for i in images or []], "model": kw.get("model")})
        if system == thp.BOOKEND_MOTION_DIRECTOR_SYSTEM:
            if isinstance(self.plan, BaseException):
                raise self.plan
            return copy.deepcopy(self.plan)
        if system == thp.BOOKEND_TEXT_CHECK_SYSTEM:
            if isinstance(self.check, BaseException):
                raise self.check
            return copy.deepcopy(self.check)
        if system == thp.CARD_TEXT_READ_SYSTEM:
            if self.reads:
                read = self.reads.pop(0)
                if isinstance(read, BaseException):
                    raise read
                return copy.deepcopy(read)
            text = self.plan.get("card_text", []) if isinstance(self.plan, dict) else []
            return {"lines": [{"text": t, "kind": "logo", "box_2d": [40, 100, 90, 900]} for t in text]}
        if system == STYLE_EXTRACT_SYSTEM:
            return {}
        raise AssertionError(f"unexpected gemini_multimodal call: {str(system)[:60]}")

    def of(self, system: str) -> list[dict]:
        return [c for c in self.calls if c["system"] == system]


@pytest.fixture
def benv(env, monkeypatch):
    db = env.db
    base = db.run_sql
    anims: dict[int, dict] = {}
    counter = {"anim": 300}

    def run_sql(sql, p):  # noqa: C901 (one branch per statement)
        s = _norm(sql)
        if s.startswith("SELECT id, mode, status, local_path, storage_bucket, storage_path, prompt_used, "
                        "cost_usd, error, width, height, mime_type, metadata FROM yas_generations WHERE id = %s"):
            row = db.rows.get(p[0])
            if not row:
                return None
            out = {k: row.get(k) for k in ("id", "mode", "status", "local_path", "storage_bucket",
                                           "storage_path", "prompt_used", "cost_usd", "error", "width",
                                           "height", "mime_type")}
            out["metadata"] = copy.deepcopy(row.get("metadata"))
            return out
        if s.startswith("INSERT INTO yas_animations"):
            (gen_id, prompt, model, op, dur, audio, aspect, status, cost, meta) = p
            counter["anim"] += 1
            aid = counter["anim"]
            anims[aid] = {"id": aid, "yas_generation_id": gen_id, "prompt_used": prompt, "veo_model": model,
                          "veo_operation_name": op, "duration_sec": dur, "with_audio": audio,
                          "aspect_ratio": aspect, "status": status, "cost_usd": cost,
                          "metadata": copy.deepcopy(meta.obj), "local_path": None, "storage_bucket": None,
                          "storage_path": None, "error": None, "bytes": None}
            return {"id": aid}
        if s.startswith("SELECT id, status, veo_operation_name, local_path, storage_bucket, storage_path, "
                        "duration_sec, with_audio, cost_usd, error, metadata FROM yas_animations WHERE id = %s"):
            row = anims.get(int(p[0]))
            return copy.deepcopy(row) if row else None
        if s.startswith("UPDATE yas_animations SET status = %s, storage_bucket = %s"):
            status, bucket, spath, lpath, nbytes, aid = p
            anims[aid].update(status=status, storage_bucket=bucket, storage_path=spath, local_path=lpath,
                              bytes=nbytes, error=None)
            return None
        if s.startswith("UPDATE yas_animations SET status = %s, error = %s"):
            status, err, aid = p
            anims[aid].update(status=status, error=err)
            return None
        if s.startswith("UPDATE yas_animations SET metadata = metadata || %s::jsonb"):
            patch, aid = p
            anims[aid]["metadata"].update(copy.deepcopy(patch.obj))
            return None
        if s.startswith("UPDATE yas_animations SET status='generating', veo_operation_name=%s"):
            op, patch, aid = p
            anims[aid].update(status="generating", veo_operation_name=op, error=None)
            anims[aid]["metadata"].update(copy.deepcopy(patch.obj))
            return None
        if s.startswith("UPDATE yas_generations SET topic = %s, updated_at = NOW() WHERE id = %s"):
            topic, rid = p
            db.rows[rid]["topic"] = topic
            return None
        return base(sql, p)

    monkeypatch.setattr(db, "run_sql", run_sql)

    gem = FakeGemini()
    monkeypatch.setattr(th, "gemini_multimodal", gem)

    async def no_openai(*a, **k):
        raise RuntimeError("offline: no OpenAI in a test")

    monkeypatch.setattr(th, "openai_chat", no_openai)

    async def brand_req():
        return {}

    monkeypatch.setattr(th, "build_brand_requirements", brand_req)

    veo = {"cards": [], "scenes": []}

    def build_card_i2v_coro(prompt, **kw):
        veo["cards"].append({"prompt": prompt, **kw})

        async def _op():
            return {"name": f"op-card-{len(veo['cards'])}", "model": "veo-fast", "cost_usd": 0.4}
        return _op()

    async def vertex_veo(prompt, **kw):
        veo["scenes"].append({"prompt": prompt, **kw})
        return {"name": f"op-scene-{len(veo['scenes'])}", "model": "veo-fast", "cost_usd": 1.2}

    monkeypatch.setattr(ca, "build_card_i2v_coro", build_card_i2v_coro)
    monkeypatch.setattr(th, "_vertex_veo_generate", vertex_veo)

    zooms: list[dict] = []

    def fake_zoom(card_path, out, aspect, seconds=4.0):
        zooms.append({"card": Path(card_path), "out": Path(out), "aspect": aspect, "seconds": seconds})
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"ZOOM:" + Path(card_path).read_bytes()[:64])
        return Path(out)

    monkeypatch.setattr(th, "_render_zoom_clip", fake_zoom)

    async def resolve_asset_path(asset_id):
        return env_files[("asset", int(asset_id))]

    async def resolve_upload_path(upload_id):
        return env_files[("upload", int(upload_id))]

    env_files: dict[tuple[str, int], Path] = {}
    monkeypatch.setattr(th, "_resolve_asset_path", resolve_asset_path)
    monkeypatch.setattr(th, "_resolve_upload_path", resolve_upload_path)

    def pick(kind: str, ident: int, size=(720, 1280), seed=1) -> Path:
        p = env.ref_file(kind, ident, size=size)
        _pattern(size, seed).save(p)
        env_files[(kind, ident)] = p
        return p

    def main_still(run_id: int, size=(720, 1280)) -> str:
        p = env.tmp / "stills" / f"{run_id}-main.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (90, 90, 90)).save(p)
        return str(p)

    env.anims, env.gem, env.veo, env.zooms, env.pick, env.main_still = anims, gem, veo, zooms, pick, main_still
    return env


def _bookend_md(**kw) -> dict:
    """A single-scene run that wants both bookends (no picks yet)."""
    md = _md(intro_excluded=False, outro_excluded=False)
    md["refs"] = {}
    md["script"].update(intro_headline="Peer learning", outro_headline="Apply Today",
                        outro_subtitle="42abudhabi.ae")
    md.update(kw)
    return md


def _card_row(env, card_id: int) -> dict:
    return env.db.rows[card_id]


def _card_image(env, card_id: int) -> Image.Image:
    return Image.open(Path(_card_row(env, card_id)["local_path"])).convert("RGB")


# ---------------------------------------------------------------------------
# Ref modes: 'as_is' is the default, and only the three modes are accepted
# ---------------------------------------------------------------------------

def test_a_pick_is_kept_as_is_unless_asked_otherwise():
    assert th.TalkingHeadRefs().intro_ref_mode == "as_is"
    assert th.TalkingHeadRefs(intro_asset_id=243).to_dict()["intro_ref_mode"] == "as_is"
    assert th.TalkingHeadRefs(outro_ref_mode="new_vibe").outro_ref_mode == "new_vibe"
    assert th.TalkingHeadRefs(outro_ref_mode="text_only").outro_ref_mode == "text_only"
    assert th._ref_mode({}, "intro") == "as_is"
    assert th._ref_mode({"intro_ref_mode": "text_only"}, "intro") == "text_only"
    assert th._ref_mode({"intro_ref_mode": "junk"}, "intro") == "as_is"
    with pytest.raises(HTTPException) as e:
        th.TalkingHeadRefs(intro_ref_mode="keep")
    assert e.value.status_code == 400


def test_the_look_patch_refuses_an_unknown_mode_and_writes_nothing(benv):
    benv.db.add_run(801, _bookend_md())
    before = copy.deepcopy(benv.db.md(801))
    with pytest.raises(HTTPException) as e:
        asyncio.run(th._apply_look_patch(801, {"refs": {"intro_asset_id": 243, "intro_ref_mode": "copy"}}))
    assert e.value.status_code == 400 and benv.db.md(801) == before


def test_an_edit_refuses_an_unknown_mode_and_writes_nothing(benv):
    benv.db.add_run(802, _bookend_md())
    before = copy.deepcopy(benv.db.md(802))
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.edit_run(run_id=802, refs_patch={"outro_ref_mode": "photo"}))
    assert e.value.status_code == 400 and benv.db.md(802) == before


def test_the_router_only_takes_the_three_modes():
    from pydantic import ValidationError

    from app.routers.yas_producer import THV2EditRequest, THV2GeneratePreviewsRequest

    for model in (THV2EditRequest, THV2GeneratePreviewsRequest):
        for mode in ("as_is", "new_vibe", "text_only", None):
            assert model(intro_ref_mode=mode).intro_ref_mode == mode
        with pytest.raises(ValidationError):
            model(outro_ref_mode="keep_it")


# ---------------------------------------------------------------------------
# A kept card is the picked image, pixel for pixel, with no model call
# ---------------------------------------------------------------------------

def test_a_kept_card_is_the_picked_image_pixel_for_pixel(benv):
    src = benv.pick("asset", 243, size=(720, 1280))
    benv.db.add_run(810, _bookend_md(outro_excluded=True))
    asyncio.run(th.generate_previews(810, look={"refs": {"intro_asset_id": 243}}))
    md = benv.db.md(810)
    card_id = md["intro_image_id"]
    assert card_id and md["intro_error"] is None
    assert _card_image(benv, card_id).tobytes() == Image.open(src).convert("RGB").tobytes()
    cmd = _card_row(benv, card_id)["metadata"]
    assert cmd["source_ref"] == {"asset_id": 243} and cmd["as_is"] is True and cmd["ref_mode"] == "as_is"
    assert _card_row(benv, card_id)["prompt_used"] == th._AS_IS_CARD_PROMPT
    # No image model drew the card (the one image call is the main still).
    assert benv.edit["calls"] == []
    assert all("sources" in c for c in benv.gen["calls"]) and len(benv.gen["calls"]) == 1
    # The motion director looked at the card once and read its text.
    looks = benv.gem.of(thp.BOOKEND_MOTION_DIRECTOR_SYSTEM)
    assert len(looks) == 1 and looks[0]["images"] == [Path(_card_row(benv, card_id)["local_path"])]
    assert cmd["motion"]["mode"] == "zoom"
    assert cmd["card_text"] == "42\nABU DHABI\nأبوظبي"
    assert cmd["card_text_source"] == "detected"


def test_an_uploaded_image_is_kept_too_and_an_upload_wins(benv):
    benv.pick("asset", 242, size=(720, 1280), seed=2)
    up = benv.pick("upload", 77, size=(720, 1280), seed=3)
    benv.db.add_run(811, _bookend_md(intro_excluded=True))
    asyncio.run(th.generate_previews(811, look={"refs": {"outro_asset_id": 242, "outro_upload_id": 77}}))
    md = benv.db.md(811)
    assert _card_image(benv, md["outro_image_id"]).tobytes() == Image.open(up).convert("RGB").tobytes()
    assert _card_row(benv, md["outro_image_id"])["metadata"]["source_ref"] == {"upload_id": 77}


def test_a_kept_card_of_another_shape_is_padded_with_every_pixel_inside(benv):
    src = benv.pick("asset", 243, size=(720, 1280))
    benv.db.add_run(812, _bookend_md(aspect_ratio="16:9", outro_excluded=True))
    asyncio.run(th.generate_previews(812, look={"refs": {"intro_asset_id": 243}}))
    card = _card_image(benv, benv.db.md(812)["intro_image_id"])
    assert card.width * 9 == card.height * 16, "exactly 16:9"
    left, top = (card.width - 720) // 2, (card.height - 1280) // 2
    assert card.crop((left, top, left + 720, top + 1280)).tobytes() == Image.open(src).convert("RGB").tobytes()
    assert benv.edit["calls"] == [] and benv.gen["calls"][0]["prompt"] != "", "no model redrew the card"


def test_a_near_shape_kept_card_is_padded_never_trimmed(benv):
    """768x1344 is 1.6% off 9:16: a still would be trimmed for free, but a
    kept card keeps every pixel (a URL at its edge stays)."""
    src = benv.pick("asset", 245, size=(768, 1344))
    benv.db.add_run(817, _bookend_md(outro_excluded=True))
    asyncio.run(th.generate_previews(817, look={"refs": {"intro_asset_id": 245}}))
    card = _card_image(benv, benv.db.md(817)["intro_image_id"])
    assert card.width * 16 == card.height * 9 and card.width >= 768 and card.height >= 1344
    left, top = (card.width - 768) // 2, (card.height - 1344) // 2
    assert card.crop((left, top, left + 768, top + 1344)).tobytes() == Image.open(src).convert("RGB").tobytes()


def test_a_missing_kept_image_fails_the_card_and_never_draws_one(benv):
    benv.pick("asset", 243)
    benv.missing.add(("asset", 243))
    benv.db.add_run(813, _bookend_md(outro_excluded=True))
    asyncio.run(th.generate_previews(813, look={"refs": {"intro_asset_id": 243}}))
    md = benv.db.md(813)
    assert md["intro_image_id"] is None and md["intro_error"]
    assert benv.edit["calls"] == [], "no AI card instead of the picked one"


def test_new_design_in_this_style_still_draws_a_card(benv):
    benv.pick("asset", 243)
    benv.db.add_run(814, _bookend_md(outro_excluded=True))
    asyncio.run(th.generate_previews(814, look={"refs": {"intro_asset_id": 243, "intro_ref_mode": "new_vibe"}}))
    md = benv.db.md(814)
    cmd = _card_row(benv, md["intro_image_id"])["metadata"]
    assert len(benv.edit["calls"]) == 1, "the card is drawn"
    assert cmd["as_is"] is False and cmd["ref_mode"] == "new_vibe" and cmd["source_ref"] == {"asset_id": 243}
    assert cmd["card_text"] == "Peer learning" and cmd["card_text_source"] == "planned"


def test_no_pick_draws_the_voxel_card_and_records_no_source(benv):
    benv.db.add_run(815, _bookend_md(intro_excluded=True))
    asyncio.run(th.generate_previews(815))
    cmd = _card_row(benv, benv.db.md(815)["outro_image_id"])["metadata"]
    assert cmd["source_ref"] is None and cmd["ref_mode"] is None and cmd["as_is"] is False
    assert cmd["card_text"] == "Apply Today\n42abudhabi.ae"


def test_an_uploaded_video_gets_no_card(benv):
    benv.db.add_run(816, _bookend_md(outro_excluded=True, refs={"intro_video_upload_id": 55}))
    asyncio.run(th.generate_previews(816))
    assert benv.db.md(816)["intro_image_id"] is None and benv.edit["calls"] == []


# ---------------------------------------------------------------------------
# A card made from another image than the one picked now is never animated
# ---------------------------------------------------------------------------

def _seed_card(env, run_id: int, role: str, *, source_ref, as_is=False, prompt="[voxel-poster] x",
               card_text=None, legacy=False, color=(10, 200, 10)) -> int:
    buf = BytesIO()
    Image.new("RGB", (720, 1280), color).save(buf, "PNG")
    extra = None if legacy else {"source_ref": source_ref, "as_is": as_is,
                                 "ref_mode": "as_is" if as_is else None, "card_text": card_text}
    return asyncio.run(th._insert_card_row(
        parent_run_id=run_id, topic="t", image_model="gemini_pro",
        card_mode=th.MODE_INTRO if role == "intro" else th.MODE_OUTRO, image_bytes=buf.getvalue(),
        mime="image/png", used_prompt=prompt, card_type=role, extra_md=extra,
    ))


def test_a_card_from_another_image_is_not_used(benv):
    src = benv.pick("asset", 243)
    benv.db.add_run(820, _bookend_md(refs={"intro_asset_id": 243}))
    card = _seed_card(benv, 820, "intro", source_ref=None)
    md = {**benv.db.md(820), "intro_image_id": card}
    bk = asyncio.run(th._resolve_bookend(run_id=820, md=md, refs=md["refs"], role="intro"))
    assert bk["path"] == src and bk["card_id"] is None and bk["kept"] is True
    # The card made from the picked image is used.
    same = _seed_card(benv, 820, "intro", source_ref={"asset_id": 243}, as_is=True)
    md["intro_image_id"] = same
    bk = asyncio.run(th._resolve_bookend(run_id=820, md=md, refs=md["refs"], role="intro"))
    assert bk["card_id"] == same and bk["kept"] is True


def test_an_older_card_is_judged_by_how_it_was_made(benv):
    src = benv.pick("asset", 243)
    benv.db.add_run(821, _bookend_md(refs={"intro_asset_id": 243}))
    # Run 824: a voxel card (drawn from no image) while an image is picked now.
    voxel = _seed_card(benv, 821, "intro", source_ref=None, legacy=True, prompt="[voxel-poster] A card")
    md = {**benv.db.md(821), "intro_image_id": voxel}
    assert asyncio.run(th._resolve_intro_card_path(run_id=821, md=md, refs=md["refs"])) == src
    # A card drawn from a pick before cards recorded their source: used as before.
    drawn = _seed_card(benv, 821, "intro", source_ref=None, legacy=True, prompt="[new_vibe]\nDNA")
    md["intro_image_id"] = drawn
    got = asyncio.run(th._resolve_intro_card_path(run_id=821, md=md, refs=md["refs"]))
    assert got == Path(benv.db.rows[drawn]["local_path"])
    # And a voxel card with nothing picked is the card.
    md2 = {**md, "refs": {}, "intro_image_id": voxel}
    assert asyncio.run(th._resolve_intro_card_path(run_id=821, md=md2, refs={})) == \
        Path(benv.db.rows[voxel]["local_path"])


def test_run_824_a_pick_after_the_stills_is_the_bookend(benv):
    """Stills made with no intro/outro picked (voxel cards), then the IG
    frames 243/242 picked with Apply look, then finalize: the bookends are
    the picked frames, never the old voxel cards, and no prompt names the
    script's CTA."""
    a243 = benv.pick("asset", 243, seed=4)
    a242 = benv.pick("asset", 242, seed=5)
    benv.db.add_run(824, _bookend_md(), local_path=benv.main_still(824))
    asyncio.run(th.generate_previews(824))
    old_intro, old_outro = benv.db.md(824)["intro_image_id"], benv.db.md(824)["outro_image_id"]
    assert old_intro and old_outro
    benv.db.rows[824]["local_path"] = benv.main_still(824)
    asyncio.run(th.edit_run(run_id=824, refs_patch={"intro_asset_id": 243, "outro_asset_id": 242,
                                                    "intro_ref_mode": "as_is", "outro_ref_mode": "as_is"}))
    md = benv.db.md(824)
    assert md["intro_image_id"] not in (None, old_intro) and md["outro_image_id"] not in (None, old_outro)
    assert _card_image(benv, md["intro_image_id"]).tobytes() == Image.open(a243).convert("RGB").tobytes()
    assert _card_image(benv, md["outro_image_id"]).tobytes() == Image.open(a242).convert("RGB").tobytes()
    assert md["intro_video_stale"] and md["outro_video_stale"]
    # Finalize: the logo-only frames have nothing to animate, so no Veo at all.
    benv.gem.plan = LOGO_ONLY
    asyncio.run(th.start_videos(824))
    md = benv.db.md(824)
    assert benv.veo["cards"] == []
    made = {z["card"] for z in benv.zooms}
    assert made == {Path(benv.db.rows[md["intro_image_id"]]["local_path"]),
                    Path(benv.db.rows[md["outro_image_id"]]["local_path"])}
    assert md["intro_animation_style"] == md["outro_animation_style"] == "slow_zoom"
    for role in ("intro", "outro"):
        anim = benv.anims[md[f"{role}_animation_id"]]
        assert anim["status"] == "ready" and anim["veo_model"] == th.BOOKEND_ZOOM_MODEL
        assert anim["cost_usd"] == 0.0 and anim["veo_operation_name"] is None
        assert anim["metadata"]["slow_zoom"] is True and anim["metadata"]["role"] == role
    assert not any("Apply Today" in c["prompt"] for c in benv.veo["scenes"] + benv.veo["cards"])


def test_run_824_with_text_only_drops_the_old_card_and_keeps_the_frame(benv):
    """824 as it really was ('Replace text'): the edit drops the old voxel
    card (no paid redraw on an edit), and finalize animates the picked frame
    itself, kept, never the old card."""
    a243 = benv.pick("asset", 243, seed=4)
    benv.db.add_run(825, _bookend_md(outro_excluded=True), local_path=benv.main_still(825))
    asyncio.run(th.generate_previews(825))
    benv.db.rows[825]["local_path"] = benv.main_still(825)
    drawn = len(benv.edit["calls"])
    asyncio.run(th.edit_run(run_id=825, refs_patch={"intro_asset_id": 243, "intro_ref_mode": "text_only"}))
    md = benv.db.md(825)
    assert md["intro_image_id"] is None and md["intro_video_stale"] is True
    assert len(benv.edit["calls"]) == drawn, "an edit never pays for a redraw"
    benv.gem.plan = ICONS
    asyncio.run(th.start_videos(825))
    sent, = benv.veo["cards"]
    assert Path(sent["start_image"]) == a243
    assert "Apply Today" not in sent["prompt"] and "Peer learning" not in sent["prompt"]


def test_run_827_cards_exist_before_finalize(benv):
    """Run 827: script, scene count changed to 2 (no still made: P0), the
    intro/outro picked on the Look step. Generate stills (the Look step's
    primary while no still exists) makes the kept cards with the stills, so
    finalize has them; their logo-only frames get the slow zoom, and no
    prompt ever claims the script's CTA ('Apply Today' under the logo)."""
    a243 = benv.pick("asset", 243, seed=4)
    a242 = benv.pick("asset", 242, seed=5)
    md = _bookend_md()
    md["scene_count"] = 2
    md["scenes"] = [_scene(0), _scene(1)]
    benv.db.add_run(827, md)
    asyncio.run(th.edit_run(run_id=827, refs_patch={"intro_asset_id": 243, "outro_asset_id": 242}))
    md = benv.db.md(827)
    assert md["intro_image_id"] is None, "before stills an edit only saves the picks"
    assert th._aspect_locked(benv.db.rows[827], md) is False
    asyncio.run(th.generate_previews(827, look={"refs": {"intro_asset_id": 243, "outro_asset_id": 242}}))
    md = benv.db.md(827)
    assert _card_image(benv, md["intro_image_id"]).tobytes() == Image.open(a243).convert("RGB").tobytes()
    assert _card_image(benv, md["outro_image_id"]).tobytes() == Image.open(a242).convert("RGB").tobytes()
    asyncio.run(th.start_videos_multi(827))
    md = benv.db.md(827)
    assert benv.veo["cards"] == [] and len(benv.veo["scenes"]) == 2
    assert md["intro_animation_style"] == md["outro_animation_style"] == "slow_zoom"
    assert not any("Apply Today" in c["prompt"] for c in benv.veo["scenes"])


def test_an_older_run_with_a_kept_pick_and_no_card_gets_its_card_at_finalize(benv):
    """An older run picked after its stills (no card from the pick): finalize
    makes the kept card first (free), then animates that."""
    a242 = benv.pick("asset", 242, seed=5)
    benv.db.add_run(828, _bookend_md(intro_excluded=True, refs={"outro_asset_id": 242,
                                                                "outro_ref_mode": "as_is"}),
                    local_path=benv.main_still(828))
    asyncio.run(th.start_videos(828))
    md = benv.db.md(828)
    assert md["outro_image_id"] and _card_image(benv, md["outro_image_id"]).tobytes() == \
        Image.open(a242).convert("RGB").tobytes()
    assert benv.zooms[-1]["card"] == Path(benv.db.rows[md["outro_image_id"]]["local_path"])


# ---------------------------------------------------------------------------
# Edits rebuild a kept card or drop a drawn one
# ---------------------------------------------------------------------------

def _stills_run(env, run_id: int, **kw) -> None:
    env.db.add_run(run_id, _bookend_md(**kw), local_path=env.main_still(run_id))


def test_a_new_pick_rebuilds_a_kept_card_at_once(benv):
    benv.pick("asset", 243, seed=4)
    a244 = benv.pick("asset", 244, seed=6)
    _stills_run(benv, 830, outro_excluded=True, refs={"intro_asset_id": 243, "intro_ref_mode": "as_is"})
    first = _seed_card(benv, 830, "intro", source_ref={"asset_id": 243}, as_is=True)
    benv.db.md(830)["intro_image_id"] = first
    asyncio.run(th.edit_run(run_id=830, refs_patch={"intro_asset_id": 244}))
    md = benv.db.md(830)
    assert md["intro_image_id"] not in (None, first)
    assert _card_image(benv, md["intro_image_id"]).tobytes() == Image.open(a244).convert("RGB").tobytes()
    assert md["intro_video_stale"] is True and benv.edit["calls"] == []


def test_a_switch_to_a_new_design_drops_the_card_without_drawing(benv):
    benv.pick("asset", 243)
    _stills_run(benv, 831, outro_excluded=True, refs={"intro_asset_id": 243, "intro_ref_mode": "as_is"})
    kept = _seed_card(benv, 831, "intro", source_ref={"asset_id": 243}, as_is=True)
    benv.db.md(831)["intro_image_id"] = kept
    asyncio.run(th.edit_run(run_id=831, refs_patch={"intro_ref_mode": "new_vibe"}))
    md = benv.db.md(831)
    assert md["intro_image_id"] is None and md["intro_video_stale"] is True
    assert benv.edit["calls"] == [] and benv.db.md(831)["refs"]["intro_ref_mode"] == "new_vibe"
    # Retry with no text makes it in the new mode (a mode change needs no feedback).
    asyncio.run(th.retry_preview(run_id=831, which="intro", feedback_text=""))
    md = benv.db.md(831)
    assert md["intro_image_id"] and len(benv.edit["calls"]) == 1
    assert _card_row(benv, md["intro_image_id"])["metadata"]["ref_mode"] == "new_vibe"


def test_a_switch_back_to_keep_as_is_copies_the_pick_again(benv):
    a243 = benv.pick("asset", 243, seed=4)
    _stills_run(benv, 832, outro_excluded=True, refs={"intro_asset_id": 243, "intro_ref_mode": "new_vibe"})
    drawn = _seed_card(benv, 832, "intro", source_ref={"asset_id": 243}, prompt="[new_vibe]\nx",
                       card_text="Peer learning")
    benv.db.md(832)["intro_image_id"] = drawn
    asyncio.run(th.edit_run(run_id=832, refs_patch={"intro_ref_mode": "as_is"}))
    md = benv.db.md(832)
    assert md["intro_image_id"] not in (None, drawn)
    assert _card_image(benv, md["intro_image_id"]).tobytes() == Image.open(a243).convert("RGB").tobytes()


def test_new_words_drop_a_drawn_card_but_never_touch_a_kept_one(benv):
    benv.pick("asset", 242)
    _stills_run(benv, 833, refs={"outro_asset_id": 242, "outro_ref_mode": "as_is"})
    drawn = _seed_card(benv, 833, "intro", source_ref=None, card_text="Peer learning")
    kept = _seed_card(benv, 833, "outro", source_ref={"asset_id": 242}, as_is=True)
    benv.db.md(833).update(intro_image_id=drawn, outro_image_id=kept)
    asyncio.run(th.edit_run(run_id=833, intro_headline="Learn from peers", outro_headline="Join us"))
    md = benv.db.md(833)
    assert md["intro_image_id"] is None and md["intro_video_stale"] is True
    assert md["outro_image_id"] == kept and not md.get("outro_video_stale"), \
        "a kept card shows its own words: neither it nor its clip changes"
    assert md["script"]["intro_headline"] == "Learn from peers"


def test_an_apply_look_that_changes_nothing_keeps_every_card(benv):
    """An older run stored 'new_vibe' for empty slots and nothing for some
    picks; the Look step now sends 'as_is'. With no pick a mode means
    nothing, and a missing mode IS 'as_is': nothing is dropped or stale."""
    benv.pick("asset", 242)
    _stills_run(benv, 838, refs={"intro_ref_mode": "new_vibe", "outro_asset_id": 242})
    voxel = _seed_card(benv, 838, "intro", source_ref=None, legacy=True, prompt="[voxel-poster] x")
    kept = _seed_card(benv, 838, "outro", source_ref={"asset_id": 242}, as_is=True)
    benv.db.md(838).update(intro_image_id=voxel, outro_image_id=kept)
    asyncio.run(th.edit_run(run_id=838, refs_patch={
        "intro_asset_id": None, "intro_upload_id": None, "intro_ref_mode": "as_is",
        "outro_asset_id": 242, "outro_upload_id": None, "outro_ref_mode": "as_is"}))
    md = benv.db.md(838)
    assert md["intro_image_id"] == voxel and md["outro_image_id"] == kept
    assert not md.get("intro_video_stale") and not md.get("outro_video_stale")
    assert md["refs"]["intro_ref_mode"] == "as_is", "the mode is still saved"
    assert benv.gem.calls == [] and benv.edit["calls"] == []


def test_an_edit_before_any_still_only_saves_the_pick(benv):
    benv.pick("asset", 243)
    benv.db.add_run(834, _bookend_md())
    asyncio.run(th.edit_run(run_id=834, refs_patch={"intro_asset_id": 243}))
    md = benv.db.md(834)
    assert md["intro_image_id"] is None and md["refs"]["intro_asset_id"] == 243
    assert benv.gem.calls == [] and not th._aspect_locked(benv.db.rows[834], md)


def test_an_uploaded_video_drops_the_card(benv):
    benv.pick("asset", 243)
    _stills_run(benv, 835, outro_excluded=True, refs={"intro_asset_id": 243, "intro_ref_mode": "as_is"})
    kept = _seed_card(benv, 835, "intro", source_ref={"asset_id": 243}, as_is=True)
    benv.db.md(835)["intro_image_id"] = kept
    asyncio.run(th.edit_run(run_id=835, refs_patch={"intro_asset_id": None, "intro_video_upload_id": 66}))
    md = benv.db.md(835)
    # The upload is used as it is: no clip to make, so nothing is out of date.
    assert md["intro_image_id"] is None and md["intro_video_stale"] is False
    assert th_reel.stale_offenders(md) == []


def _clips_run(env, run_id: int, **kw) -> None:
    """A single-scene run with stills, a kept intro card and fresh clips."""
    env.pick("asset", 243)
    _stills_run(env, run_id, outro_excluded=True, refs={"intro_asset_id": 243, "intro_ref_mode": "as_is"}, **kw)
    kept = _seed_card(env, run_id, "intro", source_ref={"asset_id": 243}, as_is=True)
    env.db.md(run_id)["intro_image_id"] = kept
    asyncio.run(th.start_videos(run_id))
    md = env.db.md(run_id)
    for key in ("intro_animation_id", "main_animation_id"):
        env.anims[md[key]]["status"] = "ready"
    assert not md.get("intro_video_stale")


@pytest.mark.parametrize("edit", [
    {"refs_patch": {"intro_asset_id": None, "intro_video_upload_id": 66}},
    {"video_quality": "quality"},
    {"lock_character": False},
])
def test_an_uploaded_video_never_blocks_finalize(benv, edit):
    """An uploaded intro video set by an edit (or any edit on a run that has
    one) leaves nothing out of date: the reel plays the upload as it is, and
    no clip could ever clear the mark."""
    _clips_run(benv, 846)
    if "refs_patch" not in edit:
        asyncio.run(th.edit_run(run_id=846, refs_patch={"intro_asset_id": None, "intro_video_upload_id": 66}))
    asyncio.run(th.edit_run(run_id=846, **edit))
    md = benv.db.md(846)
    assert not md.get("intro_video_stale") and th_reel.stale_offenders(md) in ([], ["Main"])
    asyncio.run(th.start_videos(846))
    md = benv.db.md(846)
    assert th_reel.stale_offenders(md) == [] and "Intro" not in th_reel.stale_offenders(md)


def test_a_rescript_leaves_an_uploaded_video_fresh(benv, monkeypatch):
    _stills_run(benv, 847, outro_excluded=True, refs={"intro_video_upload_id": 66})

    async def gpt_script(**kw):
        return dict(benv.db.md(847)["script"], intro_headline="A new hook"), None

    monkeypatch.setattr(th, "_gpt_script_dict", gpt_script)
    asyncio.run(th.rescript_run(847, new_topic="Why coding bootcamps work"))
    md = benv.db.md(847)
    assert not md.get("intro_video_stale") and md["main_video_stale"] is True
    assert th_reel.stale_offenders(md) == ["Main"]


def test_a_run_already_stuck_on_an_uploaded_video_can_finalize(benv, monkeypatch):
    """A run saved before the fix with an uploaded intro marked stale: the
    reel and the run read it as fresh."""
    _stills_run(benv, 848, outro_excluded=True, refs={"intro_video_upload_id": 66}, intro_video_stale=True)
    md = benv.db.md(848)
    assert th_reel.stale_offenders(md) == [] and not th_reel.bookend_stale(md, "intro")
    assert th_reel.bookend_stale({"intro_video_stale": True, "refs": {"intro_asset_id": 1}}, "intro")
    monkeypatch.setattr(th, "fetch_run", REAL_FETCH_RUN)
    got = asyncio.run(th.fetch_run(848, with_reel=False))
    assert got["intro_video_stale"] is False


def test_a_rescript_drops_a_drawn_card_and_keeps_a_kept_one(benv, monkeypatch):
    benv.pick("asset", 242)
    _stills_run(benv, 836, refs={"outro_asset_id": 242, "outro_ref_mode": "as_is"})
    drawn = _seed_card(benv, 836, "intro", source_ref=None, card_text="Peer learning")
    kept = _seed_card(benv, 836, "outro", source_ref={"asset_id": 242}, as_is=True)
    benv.db.md(836).update(intro_image_id=drawn, outro_image_id=kept)

    async def gpt_script(**kw):
        script = dict(benv.db.md(836)["script"], intro_headline="A new hook", outro_headline="Apply now")
        return script, None

    monkeypatch.setattr(th, "_gpt_script_dict", gpt_script)
    asyncio.run(th.rescript_run(836, new_topic="Why coding bootcamps work"))
    md = benv.db.md(836)
    assert md["intro_image_id"] is None and md["intro_video_stale"] is True
    assert md["outro_image_id"] == kept and not md.get("outro_video_stale")


def test_a_rescript_keeps_a_drawn_card_whose_words_did_not_change(benv, monkeypatch):
    _stills_run(benv, 837, outro_excluded=True)
    drawn = _seed_card(benv, 837, "intro", source_ref=None, card_text="Peer learning")
    benv.db.md(837)["intro_image_id"] = drawn

    async def gpt_script(**kw):
        return dict(benv.db.md(837)["script"]), None

    monkeypatch.setattr(th, "_gpt_script_dict", gpt_script)
    asyncio.run(th.rescript_run(837, new_topic="Peer learning, again"))
    assert benv.db.md(837)["intro_image_id"] == drawn


# ---------------------------------------------------------------------------
# The bookend motion director
# ---------------------------------------------------------------------------

def test_the_director_keeps_only_graphic_elements():
    plan = th._bookend_plan_from(ICONS)
    assert plan["mode"] == "veo"
    assert [e["what"] for e in plan["elements"]] == ["rocket icon", "star"], "never text"
    plan = th._bookend_plan_from({"has_animatable_elements": True, "elements": [
        {"what": "42 logo", "where": "centre", "motion": "pop"},
        {"what": "the wordmark", "where": "top", "motion": "float"},
        {"what": "background gradient", "where": "all", "motion": "pulse"}], "card_text": []})
    assert plan["mode"] == "zoom" and plan["elements"] == []
    plan = th._bookend_plan_from({"has_animatable_elements": True, "card_text": ["Hi"], "elements": [
        {"what": "arrow", "where": "right", "motion": "spin around wildly"}] * 6})
    assert plan["mode"] == "veo" and len(plan["elements"]) == 4 and plan["elements"][0]["motion"] == "float"
    assert th._bookend_plan_from("nope") is None


def test_icons_make_a_tight_veo_prompt_with_the_hard_rules(benv):
    card = benv.pick("asset", 300)
    job = asyncio.run(th._bookend_job(
        role="outro", bookend={"path": card, "card_id": None, "card": None, "kept": True},
        aspect="9:16", quality="normal", plan=th._bookend_plan_from(ICONS)))
    p = job["prompt"]
    assert job["style"] == "animate_icons" and job["coro"] is not None
    asyncio.run(job["coro"])
    sent, = benv.veo["cards"]
    assert "The rocket icon (top left) floats gently" in p and "The star (under the headline) pulses" in p
    for rule in ("ANIMATE ONLY THESE GRAPHIC ELEMENTS", "CAMERA: locked off",
                 "Keep every letter, word, number, logo and colour exactly as on the card",
                 "Add no new text", "Add no particles", "The background stays exactly as it is"):
        assert rule in p, rule
    # No ambient motion, no CTA claim, no card words for Veo to paint.
    assert thp._BOOKEND_AMBIENT_MOTION.strip()[:40] not in p
    assert "CTA text on the card" not in p and "Decode Your Tech Future" not in p
    assert "headline text" not in p
    assert sent["negative_prompt"] == thp.BOOKEND_MOTION_NEGATIVE_PROMPT and len(sent["negative_prompt"]) < 1000
    assert sent["duration"] == th.VEO_DURATION_BOOKEND and sent["aspect"] == "9:16"
    md = job["extra_md"]
    assert md["bookend_check"] is True and md["bookend_motion"]["mode"] == "veo"
    assert md["bookend_card"]["kept"] is True and md["bookend_card"]["aspect"] == "9:16"


def test_a_kept_card_of_the_wrong_shape_is_padded_for_veo_never_redrawn(benv):
    card = benv.pick("asset", 301, size=(1080, 1350))
    job = asyncio.run(th._bookend_job(
        role="intro", bookend={"path": card, "card_id": None, "card": None, "kept": True},
        aspect="9:16", quality=None, plan=th._bookend_plan_from(ICONS)))
    asyncio.run(job["coro"])
    sent, = benv.veo["cards"]
    assert Path(sent["start_image"]).name == "asset-301.9x16.pad.png"
    assert benv.gen["calls"] == [], "no model redraws a kept card"


def test_nothing_to_animate_is_the_slow_zoom_and_no_veo(benv):
    benv.pick("asset", 242)
    benv.gem.plan = LOGO_ONLY
    _stills_run(benv, 840, intro_excluded=True, refs={"outro_asset_id": 242, "outro_ref_mode": "as_is"})
    kept = _seed_card(benv, 840, "outro", source_ref={"asset_id": 242}, as_is=True)
    benv.db.md(840)["outro_image_id"] = kept
    asyncio.run(th.start_videos(840))
    md = benv.db.md(840)
    assert benv.veo["cards"] == [] and len(benv.veo["scenes"]) == 1, "only the main scene went to Veo"
    anim = benv.anims[md["outro_animation_id"]]
    assert anim["status"] == "ready" and anim["local_path"] and Path(anim["local_path"]).exists()
    assert anim["duration_sec"] == th.VEO_DURATION_BOOKEND and anim["with_audio"] is False
    assert md["outro_animation_style"] == "slow_zoom" and md["outro_video_stale"] is False
    assert benv.zooms[-1]["card"] == Path(benv.db.rows[kept]["local_path"])
    assert md["composition_status"] == th.COMP_QUEUED


def test_icons_on_the_card_go_to_veo_flagged_for_the_read_back(benv):
    benv.gem.plan = ICONS
    _stills_run(benv, 841, intro_excluded=True)
    drawn = _seed_card(benv, 841, "outro", source_ref=None, card_text="Apply Today\n42abudhabi.ae")
    benv.db.md(841)["outro_image_id"] = drawn
    asyncio.run(th.start_videos(841))
    md = benv.db.md(841)
    sent, = benv.veo["cards"]
    assert Path(sent["start_image"]) == Path(benv.db.rows[drawn]["local_path"])
    anim = benv.anims[md["outro_animation_id"]]
    assert anim["status"] == "generating" and anim["veo_operation_name"] == "op-card-1"
    assert anim["metadata"]["bookend_check"] is True and anim["metadata"]["bookend_card"]["card_id"] == drawn
    assert md["outro_animation_style"] == "animate_icons"
    assert "Apply Today" not in sent["prompt"], "the prompt never names the card's words"
    # The plan was cached on the card: the next clip asks no one.
    looks = len(benv.gem.of(thp.BOOKEND_MOTION_DIRECTOR_SYSTEM))
    anim["status"] = "ready"
    benv.anims[md["outro_animation_id"]]["status"] = "ready"
    asyncio.run(th.retry_video(run_id=841, which="outro", feedback_text=""))
    assert len(benv.gem.of(thp.BOOKEND_MOTION_DIRECTOR_SYSTEM)) == looks
    assert benv.db.rows[drawn]["metadata"]["motion"]["mode"] == "veo"


def test_a_director_that_fails_is_the_slow_zoom_and_is_asked_again(benv):
    benv.gem.plan = RuntimeError("vision down")
    card = benv.pick("asset", 302)
    bk = {"path": card, "card_id": None, "card": None, "kept": True}
    job = asyncio.run(th._bookend_job(role="intro", bookend=bk, aspect="9:16", quality=None))
    assert job["coro"] is None and job["style"] == "slow_zoom"
    benv.gem.plan = ICONS
    job = asyncio.run(th._bookend_job(role="intro", bookend=bk, aspect="9:16", quality=None))
    assert job["style"] == "animate_icons"
    job["coro"].close()


def test_a_video_retry_adds_the_feedback_to_a_veo_bookend_only(benv):
    benv.gem.plan = ICONS
    _stills_run(benv, 842, intro_excluded=True)
    drawn = _seed_card(benv, 842, "outro", source_ref=None)
    benv.db.md(842)["outro_image_id"] = drawn
    asyncio.run(th.retry_video(run_id=842, which="outro", feedback_text="slower"))
    sent, = benv.veo["cards"]
    assert sent["prompt"].endswith("slower")
    md = benv.db.md(842)
    assert benv.anims[md["outro_animation_id"]]["metadata"]["bookend_feedback"] == "slower"
    # A slow-zoom bookend is made again as one (no Veo, nothing to change).
    benv.anims[md["outro_animation_id"]]["status"] = "ready"
    benv.db.rows[drawn]["metadata"]["motion"] = th._bookend_plan_from(LOGO_ONLY)
    asyncio.run(th.retry_video(run_id=842, which="outro", feedback_text="more energy"))
    assert len(benv.veo["cards"]) == 1 and benv.db.md(842)["outro_animation_style"] == "slow_zoom"


def test_a_video_retry_of_an_uploaded_video_is_refused(benv):
    _stills_run(benv, 843, intro_excluded=True, refs={"outro_video_upload_id": 9})
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.retry_video(run_id=843, which="outro", feedback_text=""))
    assert e.value.status_code == 409


def test_a_transient_resubmit_repeats_the_directed_clip(benv):
    benv.gem.plan = ICONS
    _stills_run(benv, 844, intro_excluded=True)
    drawn = _seed_card(benv, 844, "outro", source_ref=None)
    benv.db.md(844)["outro_image_id"] = drawn
    asyncio.run(th.start_videos(844))
    aid = benv.db.md(844)["outro_animation_id"]
    first = benv.veo["cards"][0]["prompt"]
    looks = len(benv.gem.calls)
    assert asyncio.run(th.resubmit_animation_if_transient(aid, "Service is experiencing high load")) is True
    assert benv.veo["cards"][1]["prompt"] == first and len(benv.gem.calls) == looks
    assert benv.anims[aid]["veo_operation_name"] == "op-card-2"
    assert benv.anims[aid]["metadata"]["bookend_check"] is True


def test_a_resubmit_of_a_card_now_planned_as_a_zoom_finishes_that_row(benv):
    _stills_run(benv, 845, intro_excluded=True)
    drawn = _seed_card(benv, 845, "outro", source_ref=None)
    benv.db.md(845)["outro_image_id"] = drawn
    benv.anims[500] = {"id": 500, "status": "generating", "veo_operation_name": "op-old", "local_path": None,
                       "storage_bucket": None, "storage_path": None, "duration_sec": 4, "with_audio": False,
                       "cost_usd": 0.4, "error": None,
                       "metadata": {"parent_talking_head_id": 845, "role": "outro"}}
    benv.gem.plan = LOGO_ONLY
    assert asyncio.run(th.resubmit_animation_if_transient(500, "please try again")) is True
    assert benv.anims[500]["status"] == "ready" and benv.veo["cards"] == []


# ---------------------------------------------------------------------------
# The read-back of a Veo bookend
# ---------------------------------------------------------------------------

def _ffmpeg(*args) -> None:
    import imageio_ffmpeg
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *args], check=True,
                   capture_output=True, timeout=120)


def _veo_like_clip(path: Path, color="red") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=72x128:d=4:r=24", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(path))
    return path


def _clip_of_card(path: Path, card: Path, seconds: int = 4) -> Path:
    """A clip that shows the card as it is (what a faithful Veo bookend
    looks like to the pixel check), at Veo's 720x1280, 24 fps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-loop", "1", "-i", str(card), "-t", str(seconds), "-r", "24", "-vf", "scale=720:1280",
            "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(path))
    return path


def _frame(clip: Path, at: float) -> Image.Image:
    out = clip.with_name(f"{clip.stem}-{at}.png")
    _ffmpeg("-ss", str(at), "-i", str(clip), "-frames:v", "1", str(out))
    return Image.open(out).convert("RGB")


def _flagged_anim(env, run_id: int, card_path: Path, aid: int = 600) -> int:
    env.anims[aid] = {"id": aid, "status": "generating", "veo_operation_name": "op", "local_path": None,
                      "storage_bucket": None, "storage_path": None, "duration_sec": 4, "with_audio": False,
                      "cost_usd": 0.4, "error": None,
                      "metadata": {"parent_talking_head_id": run_id, "role": "outro", "bookend_check": True,
                                   "bookend_card": {"card_id": None, "path": str(card_path), "kept": True,
                                                    "aspect": "9:16"}}}
    return aid


def test_the_verdict_on_a_read_back():
    ok = {"card_text": ["42", "ABU DHABI"], "middle_text": ["42"], "last_text": ["ABU  dhabi.", "42"],
          "text_changed": False, "restyled": False}
    assert th._bookend_text_verdict(ok) == (True, "the text and the look match the card")
    added = dict(ok, last_text=["42", "ABU DHABI", "Apply Today"])
    good, why = th._bookend_text_verdict(added)
    assert not good and "apply" in why and "today" in why
    garbled = dict(ok, last_text=["42", "ABU DHABJ"])
    assert th._bookend_text_verdict(garbled)[0] is False
    missing = dict(ok, last_text=["42"])
    assert "missing" in th._bookend_text_verdict(missing)[1]
    assert th._bookend_text_verdict(dict(ok, text_changed=True))[0] is False
    assert "look changed" in th._bookend_text_verdict(dict(ok, restyled=True))[1]
    assert th._bookend_text_verdict(None)[0] is False
    # Arabic words are compared too, in any Unicode form.
    ar = "أبوظبي"
    assert thp.card_text_words([ar]) == thp.card_text_words([" " + ar + "!"]) and thp.card_text_words([ar])


def test_a_bookend_whose_text_held_is_kept(benv, tmp_path):
    card = benv.pick("asset", 310)
    clip = _clip_of_card(tmp_path / "anim-600.mp4", card)
    before = clip.read_bytes()
    benv.db.add_run(850, _bookend_md())
    aid = _flagged_anim(benv, 850, card)
    benv.gem.check = {"card_text": ["Join us"], "middle_text": ["Join us"], "last_text": ["Join us"],
                      "text_changed": False, "restyled": False}
    rec = asyncio.run(th.check_bookend_clip(aid, clip))
    assert rec["ok"] is True and rec["reason"] == "the text and the look match the card"
    assert rec["pixels"]["ok"] is True and "fallback" not in rec, "its pixels held too"
    assert clip.read_bytes() == before
    call, = benv.gem.of(thp.BOOKEND_TEXT_CHECK_SYSTEM)
    assert call["images"][0] == card and len(call["images"]) == 3
    assert call["model"] == th.config.vertex_gemini_fast_model
    assert benv.anims[aid]["metadata"]["bookend_text_check"]["ok"] is True


def test_a_bookend_that_gained_text_becomes_the_slow_zoom(benv, tmp_path, monkeypatch):
    monkeypatch.setattr(th, "_render_zoom_clip", REAL_RENDER_ZOOM)
    card = tmp_path / "card.png"
    Image.new("RGB", (720, 1280), (20, 40, 230)).save(card)
    # Its pixels hold (the card as it is): the text read decides.
    clip = _clip_of_card(tmp_path / "anim-601.mp4", card)
    benv.db.add_run(851, _bookend_md())
    aid = _flagged_anim(benv, 851, card, aid=601)
    benv.gem.check = {"card_text": ["42"], "middle_text": ["42"], "last_text": ["42", "Apply Today"],
                      "text_changed": True, "restyled": True}
    rec = asyncio.run(th.check_bookend_clip(aid, clip))
    assert rec["ok"] is False and rec["fallback"] == "slow_zoom" and "apply" in rec["reason"]
    mid = _frame(clip, 2.0)
    assert mid.size == (1080, 1920), "the clip is now the slow zoom, at the reel's size"
    r, g, b = mid.resize((1, 1)).getpixel((0, 0))
    assert b > 180 and r < 60, "the clip is the card"
    assert rec["pixels"]["ok"] is True
    assert benv.db.md(851)["outro_animation_style"] == "slow_zoom"
    assert benv.anims[aid]["metadata"]["bookend_text_check"]["fallback"] == "slow_zoom"


def test_a_read_back_that_cannot_be_made_is_not_trusted(benv, tmp_path):
    card = benv.pick("asset", 311)
    clip = _veo_like_clip(tmp_path / "anim-602.mp4")
    benv.db.add_run(852, _bookend_md())
    aid = _flagged_anim(benv, 852, card, aid=602)
    benv.gem.check = RuntimeError("quota")
    rec = asyncio.run(th.check_bookend_clip(aid, clip))
    assert rec["ok"] is False and rec["fallback"] == "slow_zoom"
    assert benv.zooms[-1]["out"] == clip and benv.zooms[-1]["card"] == card


def test_other_clips_are_never_read_back(benv, tmp_path):
    benv.anims[603] = {"id": 603, "status": "generating", "metadata": {"role": "scene_0"}}
    benv.anims[604] = {"id": 604, "status": "generating", "metadata": {"role": "intro"}}  # video editor
    assert asyncio.run(th.check_bookend_clip(603, tmp_path / "x.mp4")) is None
    assert asyncio.run(th.check_bookend_clip(604, tmp_path / "x.mp4")) is None
    assert benv.gem.calls == []


def test_the_poller_reads_a_bookend_back_before_storing_it(monkeypatch, tmp_path):
    from app.workers import yas_animations_poller as poller

    order: list[str] = []

    async def download(uri, dest):
        Path(dest).write_bytes(b"VEO")
        order.append("download")

    async def check(anim_id, path):
        order.append("check")
        Path(path).write_bytes(b"ZOOM")

    stored: dict = {}

    async def upload(*, data, path, mime):
        order.append("upload")
        stored["data"] = data
        return {"storage_bucket": "b", "storage_path": path, "local_path": None}

    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=()):
            order.append("update")

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(poller, "download_gcs", download)
    monkeypatch.setattr(poller, "ANIMATIONS_DIR", tmp_path)
    monkeypatch.setattr(asset_storage, "upload", upload)
    monkeypatch.setattr(poller, "get_conn", lambda *a, **k: _Conn())
    monkeypatch.setattr(th, "check_bookend_clip", check)
    asyncio.run(poller._mark_ready(7, "gs://x/7.mp4"))
    assert order == ["download", "check", "upload", "update"] and stored["data"] == b"ZOOM"

    async def broken(anim_id, path):
        raise RuntimeError("boom")

    monkeypatch.setattr(th, "check_bookend_clip", broken)
    asyncio.run(poller._mark_ready(8, "gs://x/8.mp4"))
    assert stored["data"] == b"VEO", "a broken check keeps the clip as Veo made it"


# ---------------------------------------------------------------------------
# The in-house slow zoom
# ---------------------------------------------------------------------------

def test_the_zoom_eases_from_1_to_1_06():
    assert th._zoom_scale(0) == 1.0 and abs(th._zoom_scale(1) - 1.06) < 1e-12
    vals = [th._zoom_scale(k / 119) for k in range(120)]
    assert all(b >= a for a, b in zip(vals, vals[1:])), "never zooms back out"
    assert vals[1] - vals[0] < vals[60] - vals[59], "starts gently"
    assert vals[-1] - vals[-2] < vals[60] - vals[59], "ends gently"


def test_the_zoom_clip_is_the_card_at_the_reels_size_and_rate(tmp_path):
    card = tmp_path / "card.png"
    im = _pattern((756, 1344), seed=7)
    im.save(card)
    out = tmp_path / "anim-1.mp4"
    REAL_RENDER_ZOOM(card, out, "9:16", 4.0)
    import imageio_ffmpeg
    probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(out)],
                           capture_output=True, text=True).stderr
    assert "1080x1920" in probe and "30 fps" in probe and "Audio" not in probe
    frames, _ = imageio_ffmpeg.count_frames_and_secs(str(out))
    assert frames == 120
    first = _frame(out, 0.0)
    want_first = im.resize((1080, 1920), Image.BICUBIC)
    assert _mean_diff(first, want_first) < 6, "frame 0 is the whole card"
    last = _frame(out, 3.95)
    bw, bh = 756 / 1.06, 1344 / 1.06
    box = ((756 - bw) / 2, (1344 - bh) / 2, (756 + bw) / 2, (1344 + bh) / 2)
    want_last = im.resize((1080, 1920), Image.BICUBIC, box=box)
    assert _mean_diff(last, want_last) < 6, "the last frame is the centre 1/1.06 of the card"


def test_a_landscape_zoom_and_a_card_of_another_shape(tmp_path):
    card = tmp_path / "card.png"
    Image.new("RGB", (720, 1280), (200, 30, 30)).save(card)
    out = REAL_RENDER_ZOOM(card, tmp_path / "anim-2.mp4", "16:9", 4.0)
    mid = _frame(out, 2.0)
    assert mid.size == (1920, 1080)
    assert mid.getpixel((960, 540))[0] > 150, "the card sits in the middle, padded, not stretched"


def _mean_diff(a: Image.Image, b: Image.Image) -> float:
    a = a.resize((108, 192)).convert("L")
    b = b.resize((108, 192)).convert("L")
    pa, pb = list(a.getdata()), list(b.getdata())
    return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


# ---------------------------------------------------------------------------
# The card on the Stills tile, and the video editor's bookends untouched
# ---------------------------------------------------------------------------

def test_the_run_says_what_each_card_is(benv, monkeypatch):
    benv.pick("asset", 242)
    _stills_run(benv, 860, refs={"intro_asset_id": 243, "outro_asset_id": 242, "outro_ref_mode": "as_is"})
    stale = _seed_card(benv, 860, "intro", source_ref=None, card_text="Peer learning")
    kept = _seed_card(benv, 860, "outro", source_ref={"asset_id": 242}, as_is=True, card_text="42")
    benv.db.rows[kept]["metadata"]["card_text_source"] = "detected"
    benv.db.rows[kept]["metadata"]["motion"] = th._bookend_plan_from(LOGO_ONLY)
    benv.db.md(860).update(intro_image_id=stale, outro_image_id=kept)
    run = asyncio.run(REAL_FETCH_RUN(860, with_reel=False))
    assert run["outro"]["as_is"] is True and run["outro"]["card_text"] == "42"
    assert run["outro"]["card_text_source"] == "detected" and run["outro"]["motion"] == "zoom"
    assert run["outro"]["matches_refs"] is True
    assert run["intro"]["matches_refs"] is False and run["intro"]["as_is"] is False
    # A pick with no card is the pick, flagged as no card.
    benv.db.md(860)["outro_image_id"] = None
    run = asyncio.run(REAL_FETCH_RUN(860, with_reel=False))
    assert run["outro"]["card_missing"] is True and run["outro"]["id"] is None


def test_the_video_editor_bookends_keep_todays_defaults(benv):
    assert th.render_card_animation_style is th._render_card_animation_style
    assert th.insert_card_row is th._insert_card_row
    sig = inspect.signature(th._render_card_animation_style)
    assert sig.parameters["conform"].default is None
    assert inspect.signature(th._insert_card_row).parameters["extra_md"].default is None
    assert inspect.signature(th._insert_animation_row).parameters["extra_md"].default is None
    cid = asyncio.run(th._insert_card_row(
        parent_run_id=99, topic="t", image_model="gemini_pro", card_mode=th.MODE_OUTRO,
        image_bytes=_png_bytes((720, 1280)), mime="image/png", used_prompt="[voxel-poster] x",
        card_type="outro", category="video_edit_bookend_card"))
    assert benv.db.rows[cid]["metadata"] == {"category": "video_edit_bookend_card",
                                            "parent_talking_head_id": 99, "card_type": "outro"}
    aid = asyncio.run(th._insert_animation_row(
        parent_run_id=99, prompt_used="p", veo_op_name="op", veo_model="m", duration_sec=4,
        with_audio=False, cost_usd=0.4, role="outro"))
    assert benv.anims[aid]["metadata"] == {"parent_talking_head_id": 99, "role": "outro"}
    # The editor's Veo prompts keep their ambient motion and CTA line.
    p = thp.build_outro_veo_prompt(topic="t", script={"outro_headline": "Apply Today"})
    assert 'The CTA text on the card is "Apply Today"' in p and "ambient LIFE" in p
    assert "ambient LIFE" in thp.build_intro_veo_prompt(topic="t", script={})
    from app.services import yas_video_edit_ai_bookends as ve
    src = inspect.getsource(ve.kickoff_bookend)
    assert 'ref_mode="new_vibe"' in src and "pick_bookend_animation" in src and "_bookend_job" not in src


def test_no_em_dash_in_the_new_prompts():
    dash = chr(0x2014)
    texts = [thp.BOOKEND_MOTION_DIRECTOR_SYSTEM, thp.BOOKEND_TEXT_CHECK_SYSTEM,
             thp.BOOKEND_MOTION_NEGATIVE_PROMPT,
             thp.build_bookend_motion_veo_prompt(role="intro", elements=ICONS["elements"])]
    assert not any(dash in t for t in texts)
