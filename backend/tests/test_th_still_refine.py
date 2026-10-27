"""TH v2 still Refine / Regenerate / Undo (#56).

- Refine is a TRUE image edit of the still the user is looking at: IMAGE 1 is
  the current still (the canvas), IMAGE 2 the scene's person reference only
  when a character lock applies and the still is not the character photo, no
  background reference, no keyframe prompt (its locks forbid the very changes
  people ask for), a TH refine system that keeps a closed, neutral mouth
  unless the change is about the expression, and the run's aspect forced.
  Refinements add up: the next refine edits this one.
- Every result is a new version (scene 0: a new file, the old one in
  metadata.still_versions; scenes >= 1: a new row with lineage), and Undo
  puts the previous one back.
- A failure never marks the run failed and leaves the still where it is;
  errors map to 429 / 422 / 502 / 503 / 504.
- Regenerate (no text) makes the still again the way first generation did;
  retry-preview without feedback and the batch "Regen images" use it.

No database and no model calls: the run store is the in-memory FakePg of
test_th_v2_scene_stills, and render_card_image / _run_image_gen are fakes (the
`env` fixture also fails any real model call).
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from app.services import asset_storage
from app.services import card_animation as ca
from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp
from app.services.poster_prompts import IMAGE_ONLY_RETRY_ENFORCER
from app.services.vertex_client import GeminiNoImageError, VertexBudgetExceededError
from test_th_v2_scene_stills import (  # noqa: F401
    PHANTOM, SUBJECT, _md, _png_bytes, _scene, _seed_old_main, _seed_scene_row, env,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = (
    "Camera:",
    "do not add, remove, or exaggerate a smile",
    "do not redesign or redress",
    "match that background faithfully",
)


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _texts(call: dict) -> list[str]:
    return [str(v) for k, v in call["parts"] if k == "text"]


def _images(call: dict) -> list[Path]:
    return [Path(v) for k, v in call["parts"] if k == "image"]


def _dims(path) -> tuple[int, int]:
    with Image.open(th._abs_from_rel(str(path))) as im:
        return im.size


@pytest.fixture
def renv(env, monkeypatch):
    """env with a fresh image-gen semaphore (bound to the test's loop) and a
    record of every _mark_row_failed."""
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    failed: list[tuple] = []

    async def mark_failed(row_id, error):
        failed.append((row_id, error))

    monkeypatch.setattr(th, "_mark_row_failed", mark_failed)
    env.failed = failed
    return env


def _scripted_render(monkeypatch, outcomes: list):
    """render_card_image answering with `outcomes` in turn (bytes, or an
    exception to raise; the last one repeats). Returns the list of calls."""
    calls: list[dict] = []

    async def render(parts, *, system_instruction, timeout, aspect_ratio=None):
        calls.append({"parts": list(parts), "system": system_instruction, "aspect": aspect_ratio})
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(ca, "render_card_image", render)
    return calls


def _single(env, run_id: int, **kw) -> Path:
    """A single-scene run with a still (1248x832 main.png)."""
    env.db.add_run(run_id, _md(**kw))
    return _seed_old_main(env, run_id)


# ---------------------------------------------------------------------------
# The prompt: ordered parts, one change, a closed mouth by default
# ---------------------------------------------------------------------------

def test_parts_are_canvas_then_identity_then_the_directive(tmp_path):
    still, ref = tmp_path / "still.png", tmp_path / "ref.png"
    parts = thp.build_still_refine_parts("warmer light", still=still, identity_ref=ref, aspect="9:16")
    assert [k for k, _ in parts] == ["text", "image", "text", "image", "text"]
    assert parts[0][1].startswith("IMAGE 1 is the CURRENT STILL") and parts[1][1] == still
    assert parts[2][1].startswith("IMAGE 2 is an IDENTITY REFERENCE") and parts[3][1] == ref
    assert parts[4][1].startswith("RENDER the still again")
    alone = thp.build_still_refine_parts("warmer light", still=still, aspect="9:16")
    assert [k for k, _ in alone] == ["text", "image", "text"]
    assert "IMAGE 2" not in alone[-1][1], "no identity reference: the directive does not name one"


def test_the_directive_quotes_the_change_and_none_of_the_keyframe_locks():
    d = thp.build_still_refine_directive("zoom out so she is visible above the hip", aspect="9:16")
    assert '"zoom out so she is visible above the hip"' in d
    assert "the change wins" in d and "Exactly one person" in d
    assert "No new text, captions, logos or watermarks" in d
    assert "9:16 vertical" in d and "edge-to-edge" in d
    assert "16:9 horizontal" in thp.build_still_refine_directive("x", aspect="16:9")
    for bad in FORBIDDEN + PHANTOM:
        assert bad not in d
        assert bad not in thp.TH_STILL_REFINE_SYSTEM


def test_the_change_is_capped_and_rai_scrubbed():
    long = "a" * 600
    d = thp.build_still_refine_directive(long)
    assert "a" * 500 in d and "a" * 501 not in d
    assert thp.clean_still_change("  " + long + "  ") == "a" * 500
    d = thp.build_still_refine_directive("make the face swap smoother")
    assert "face swap" not in d and "appearance change" in d


def test_a_closed_mouth_unless_the_change_is_about_the_expression():
    for change in ("warmer, golden-hour light", "zoom out so she is visible above the hip",
                   "a plain grey wall behind him", "sadly lit room"):
        assert not thp.still_refine_changes_expression(change), change
        assert "mouth closed" in thp.build_still_refine_directive(change)
    for change in ("a slight natural smile", "look more serious", "happier", "Smiling softly",
                   "close his mouth", "a friendlier expression"):
        assert thp.still_refine_changes_expression(change), change
        d = thp.build_still_refine_directive(change)
        assert "about the expression" in d and "mouth closed" not in d
    system = thp.TH_STILL_REFINE_SYSTEM
    assert "ALWAYS respond with an image" in system and "NEVER with text" in system
    assert "mouth closed" in system and "explicitly about the facial expression" in system


def test_the_new_prompt_text_has_no_em_dash():
    em = chr(0x2014)
    assert em not in thp.TH_STILL_REFINE_SYSTEM
    for part in thp.build_still_refine_parts("x", still="s", identity_ref="r"):
        assert em not in str(part[1])


# ---------------------------------------------------------------------------
# refine_still: what is sent
# ---------------------------------------------------------------------------

def test_scene0_refine_edits_the_current_still_with_the_identity_reference(renv):
    char = renv.ref_file("asset", 7)
    bg = renv.ref_file("asset", 4, size=(1500, 1000))
    old = _single(renv, 40, lock=True, refs={"character_asset_id": 7, "bg_asset_id": 4})
    asyncio.run(th.refine_still(40, "main", "warmer light"))
    call, = renv.edit["calls"]
    assert _images(call) == [old, char], "IMAGE 1 = the current still, IMAGE 2 = the person"
    assert bg not in _images(call), "the background reference is never attached"
    assert call["system"] == thp.TH_STILL_REFINE_SYSTEM != th._GEMINI_FACE_SYSTEM
    # One edit gets the refine engine's own limit, not the 300 s of a
    # generation (run 830: one stalled call held the whole refine).
    assert call["aspect"] == "9:16" and call["timeout"] == th.REFINE_ATTEMPT_TIMEOUT_SEC
    final = _texts(call)[-1]
    assert '"warmer light"' in final and "mouth closed" in final
    for bad in FORBIDDEN + PHANTOM:
        assert bad not in "\n".join(_texts(call))
    assert renv.gen["calls"] == [], "no compose generation"


def test_scene1_refine_canvas_is_the_scene_row_still(renv):
    md = _md(scenes=2, lock=False)
    md["scenes"][1].update(keyframe_id=555, character_asset_id=9)
    renv.db.add_run(41, md)
    renv.ref_file("asset", 9)
    canvas = _seed_scene_row(renv, 41, 1, 555)
    asyncio.run(th.refine_still(41, "scene_1", "brighter"))
    call, = renv.edit["calls"]
    assert _images(call) == [canvas], "lock off and a pick of its own: no identity reference"


@pytest.mark.parametrize("aspect,raw,stored", [
    ("9:16", (768, 1344), (756, 1344)),
    ("16:9", (1344, 768), (1344, 756)),
])
def test_the_aspect_is_forced_and_the_result_is_exactly_the_run_shape(renv, aspect, raw, stored):
    renv.edit["size"] = raw
    _single(renv, 42, aspect=aspect)
    out = asyncio.run(th.refine_still(42, "main", "warmer light"))
    assert renv.edit["calls"][0]["aspect"] == aspect
    assert (out["width"], out["height"]) == stored
    run = renv.db.rows[42]
    assert (run["width"], run["height"]) == stored and _dims(run["local_path"]) == stored


def test_a_wrong_shaped_edit_is_reframed_once_and_kept_padded_if_that_fails(renv):
    renv.edit["size"] = (1024, 1024)
    _single(renv, 43)
    asyncio.run(th.refine_still(43, "main", "warmer light"))
    reframe, = renv.gen["calls"]
    assert "RESIZE ONLY" in reframe["prompt"] and reframe["conform"] is None
    assert (renv.db.rows[43]["width"], renv.db.rows[43]["height"]) == (756, 1344)
    # The reframe call fails: the edit is kept, padded to the shape.
    renv.gen["raise_on"] = lambda prompt, sources: True
    asyncio.run(th.refine_still(43, "main", "cooler light"))
    w, h = renv.db.rows[43]["width"], renv.db.rows[43]["height"]
    assert th._is_exact_aspect(w, h, "9:16")
    assert renv.db.rows[43]["feedback_text"] == "cooler light"


def test_identity_reference_rules(renv):
    char7 = renv.ref_file("asset", 7)
    char9 = renv.ref_file("asset", 9)
    md = _md(scenes=4, lock=True, refs={"character_asset_id": 7})
    md["scenes"][1].update(keyframe_id=501, character_asset_id=9)
    md["scenes"][2].update(keyframe_id=502, as_is=True, character_asset_id=9)
    md["scenes"][3].update(keyframe_id=503)
    renv.db.add_run(44, md)
    for i, rid in ((1, 501), (2, 502), (3, 503)):
        _seed_scene_row(renv, 44, i, rid)
    asyncio.run(th.refine_still(44, 1, "brighter"))
    asyncio.run(th.refine_still(44, 2, "brighter"))
    asyncio.run(th.refine_still(44, 3, "brighter"))
    s1, s2, s3 = renv.edit["calls"]
    assert _images(s1)[1:] == [char9], "the scene's own pick"
    assert _images(s2)[1:] == [], "an as-is still is the person already"
    assert _images(s3)[1:] == [char7], "no scene pick: the run's pick"


def test_an_anchored_scene_gets_scene0s_still_whatever_the_lock(renv):
    md = _md(scenes=2, lock=False)
    md["scenes"][1].update(keyframe_id=510)
    renv.db.add_run(45, md)
    main = _seed_old_main(renv, 45)
    canvas = _seed_scene_row(renv, 45, 1, 510)
    asyncio.run(th.refine_still(45, "scene_1", "closer"))
    call, = renv.edit["calls"]
    assert _images(call) == [canvas, main]
    # Scene 0 itself has no person reference without a pick.
    asyncio.run(th.refine_still(45, "main", "closer"))
    assert _images(renv.edit["calls"][1]) == [main]


def test_the_edit_runs_under_the_image_gen_semaphore(renv, monkeypatch):
    seen: list[int] = []

    async def render(parts, *, system_instruction, timeout, aspect_ratio=None):
        seen.append(th._image_gen_sem()._value)
        return _png_bytes()

    monkeypatch.setattr(ca, "render_card_image", render)
    _single(renv, 46)
    asyncio.run(th.refine_still(46, "main", "x"))
    assert seen == [th.MAX_CONCURRENT_IMAGE_GENS - 1]


def test_a_bucket_only_still_is_fetched_as_the_canvas(renv, monkeypatch):
    old = _single(renv, 47)
    old.unlink()
    cached = renv.tmp / "cached.png"
    cached.write_bytes(_png_bytes())
    asked: list[int] = []

    async def resolve_card_path(row_id):
        asked.append(row_id)
        return cached

    monkeypatch.setattr(th, "_resolve_card_path", resolve_card_path)
    asyncio.run(th.refine_still(47, "main", "x"))
    assert asked == [47] and _images(renv.edit["calls"][0]) == [cached]


# ---------------------------------------------------------------------------
# refine_still: versions, lineage, flags
# ---------------------------------------------------------------------------

def test_scene0_refine_is_a_new_version(renv):
    old = _single(renv, 50)
    renv.db.rows[50].update(error="main_keyframe_failed: x", status="failed")
    renv.db.md(50).update(main_error="x", main_as_is=True)
    asyncio.run(th.refine_still(50, "main", "warmer light"))
    run, md = renv.db.rows[50], renv.db.md(50)
    assert old.exists() and run["local_path"] != str(old) and Path(run["local_path"]).exists()
    assert run["storage_path"].startswith("yas/talking-head-v2/50/main-")
    prev, = md["still_versions"]["0"]
    assert prev["local_path"] == str(old) and prev["feedback_text"] == "far away shot"
    assert prev["as_is"] is True
    assert run["feedback_text"] == "warmer light" and run["retry_kind"] is None
    assert md["main_retry_kind"] == "refine"
    assert run["status"] == "ready" and run["error"] is None and md["main_error"] is None
    assert md["main_as_is"] is False and md["main_video_stale"] is True
    assert "IMAGE 1 is the CURRENT STILL" in run["prompt_used"] and "warmer light" in run["prompt_used"]


def test_scene0_refine_on_a_multi_scene_run_marks_scene0_stale(renv):
    md = _md(scenes=2)
    renv.db.add_run(51, md)
    _seed_old_main(renv, 51)
    asyncio.run(th.refine_still(51, "scene_0", "x"))
    md = renv.db.md(51)
    assert md["scenes"][0]["video_stale"] is True and md["scenes"][1]["video_stale"] is False
    assert "main_video_stale" not in md


def test_scene1_refine_records_lineage(renv):
    md = _md(scenes=3)
    md["scenes"][1].update(keyframe_id=555, animation_id=77, error="old failure", as_is=True)
    renv.db.add_run(52, md)
    _seed_scene_row(renv, 52, 1, 555)
    out = asyncio.run(th.refine_still(52, "scene_1", "smile less"))
    sc = renv.db.md(52)["scenes"]
    assert sc[1]["keyframe_id"] == out["id"] != 555 and sc[1]["previous_keyframe_ids"] == [555]
    assert sc[1]["video_stale"] is True and sc[1]["error"] is None and sc[1]["as_is"] is False
    assert sc[1]["animation_id"] == 77 and sc[0] == _scene(0) and sc[2] == _scene(2)
    row = renv.db.rows[out["id"]]
    assert row["feedback_text"] == "smile less" and row["parent_generation_id"] == 555
    assert row["metadata"]["retry_kind"] == "refine" and row["retry_kind"] is None


def test_refinements_add_up(renv):
    _single(renv, 53)
    asyncio.run(th.refine_still(53, "main", "warmer light"))
    first = Path(renv.db.rows[53]["local_path"])
    asyncio.run(th.refine_still(53, "main", "zoom out"))
    assert _images(renv.edit["calls"][1])[0] == first, "refine 2 edits refine 1's output"
    md = _md(scenes=2)
    md["scenes"][1].update(keyframe_id=560)
    renv.db.add_run(54, md)
    _seed_scene_row(renv, 54, 1, 560)
    asyncio.run(th.refine_still(54, 1, "warmer light"))
    first = Path(renv.db.rows[renv.db.md(54)["scenes"][1]["keyframe_id"]]["local_path"])
    asyncio.run(th.refine_still(54, 1, "zoom out"))
    assert _images(renv.edit["calls"][3])[0] == first


def test_refine_is_an_engine_other_steps_can_drive(renv):
    """The framing reshoot (E3) calls refine_still with its own instruction,
    retry_kind and what it records."""
    md = _md(scenes=2)
    md["scenes"][1].update(keyframe_id=570)
    renv.db.add_run(55, md)
    _seed_scene_row(renv, 55, 1, 570)
    out = asyncio.run(th.refine_still(55, 1, "Re-frame to a waist-up medium shot.",
                                      retry_kind="reframe", extra_md={"framing_used": "above_hip"},
                                      scene_md={"framing": "above_hip"}))
    row = renv.db.rows[out["id"]]
    assert row["metadata"]["retry_kind"] == "reframe" and row["metadata"]["framing_used"] == "above_hip"
    assert renv.db.md(55)["scenes"][1]["framing"] == "above_hip"
    _seed_old_main(renv, 55)
    asyncio.run(th.refine_still(55, 0, "Re-frame.", retry_kind="reframe",
                                extra_md={"framing_used": "close"}))
    assert renv.db.md(55)["framing_used"] == "close" and renv.db.md(55)["main_retry_kind"] == "reframe"
    # Run from an edit's background regen, which marks the scene generating.
    renv.db.md(55)["scenes"][1]["keyframe_generating"] = True
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.refine_still(55, 1, "Re-frame."))
    assert e.value.status_code == 409
    asyncio.run(th.refine_still(55, 1, "Re-frame.", busy_ok=True))
    assert renv.db.md(55)["scenes"][1]["previous_keyframe_ids"] == [570, out["id"]]


# ---------------------------------------------------------------------------
# refine_still: refusals and failures
# ---------------------------------------------------------------------------

def test_refine_needs_a_change_a_still_and_a_free_scene(renv):
    renv.db.add_run(60, _md())
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.refine_still(60, "main", "   "))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.refine_still(60, "main", "warmer"))
    assert e.value.status_code == 409 and "Regenerate" in e.value.detail
    _seed_old_main(renv, 60)
    renv.db.md(60)["main_keyframe_generating"] = True
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.refine_still(60, "main", "warmer"))
    assert e.value.status_code == 409 and e.value.detail == th.STILL_BUSY_MESSAGE
    assert renv.edit["calls"] == []


# A timeout or an answer with no image is asked once more (without the
# identity reference): a text reply is 2 calls a try (the image-only
# escalation), so 4; a timeout 2; an empty image 2. Anything else fails at once.
@pytest.mark.parametrize("outcomes,status,calls", [
    ([RuntimeError("429 RESOURCE_EXHAUSTED: quota")], 429, 1),
    ([GeminiNoImageError("no image", blocked=True, finish_reason="IMAGE_SAFETY")], 422, 1),
    ([GeminiNoImageError("text", blocked=False)], 502, 4),
    ([asyncio.TimeoutError()], 504, 2),
    ([VertexBudgetExceededError("over budget")], 503, 1),
    ([b""], 422, 2),
    ([RuntimeError("boom")], 502, 1),
])
def test_a_failed_refine_maps_cleanly_and_leaves_the_run_alone(renv, monkeypatch, outcomes, status, calls):
    old = _single(renv, 61)
    before = dict(renv.db.rows[61])
    md_before = dict(renv.db.md(61))
    sent = _scripted_render(monkeypatch, outcomes)
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.refine_still(61, "main", "warmer light"))
    assert e.value.status_code == status
    assert len(sent) == calls
    run = renv.db.rows[61]
    assert run["status"] == before["status"] == "ready" and run["error"] is None
    assert run["local_path"] == str(old) and renv.db.md(61) == md_before
    assert renv.failed == [], "a refine error never marks the run failed"


def test_a_text_reply_is_retried_once_with_the_image_only_contract(renv, monkeypatch):
    _single(renv, 62)
    sent = _scripted_render(monkeypatch, [GeminiNoImageError("text", blocked=False), _png_bytes()])
    asyncio.run(th.refine_still(62, "main", "warmer light"))
    assert len(sent) == 2
    assert _texts(sent[0])[-1] + IMAGE_ONLY_RETRY_ENFORCER == _texts(sent[1])[-1]
    assert sent[1]["aspect"] == "9:16"
    assert renv.db.rows[62]["feedback_text"] == "warmer light"


def test_retry_preview_never_fails_the_run_on_a_still_error(renv, monkeypatch):
    _single(renv, 63)
    _scripted_render(monkeypatch, [RuntimeError("boom")])
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.retry_preview(run_id=63, which="main", feedback_text="warmer"))
    assert e.value.status_code == 502 and renv.failed == []
    assert renv.db.rows[63]["status"] == "ready"


# ---------------------------------------------------------------------------
# Regenerate: no text, the first-generation mode
# ---------------------------------------------------------------------------

def test_retry_preview_without_feedback_regenerates(renv):
    _single(renv, 70, lock=True)
    asyncio.run(th.retry_preview(run_id=70, which="main", feedback_text=""))
    asyncio.run(th.retry_preview(run_id=70, which="main", feedback_text=None))
    assert len(renv.gen["calls"]) == 2 and renv.edit["calls"] == []
    md = renv.db.md(70)
    assert md["main_retry_kind"] == "regenerate" and len(md["still_versions"]["0"]) == 2
    assert renv.db.rows[70]["feedback_text"] == "", "this version had no change text"


def test_regenerate_without_a_character_never_claims_a_person(renv):
    md = _md(scenes=2, lock=True)
    md["scenes"][1].update(keyframe_id=700)
    renv.db.add_run(71, md)
    _seed_scene_row(renv, 71, 1, 700)
    asyncio.run(th.regenerate_still(71, "main"))
    call, = renv.gen["calls"]
    assert call["sources"] is None
    for bad in PHANTOM:
        assert bad not in call["prompt"]
    assert SUBJECT in call["prompt"]
    # Scene 1 (no pick) is anchored on scene 0's new still: really attached.
    asyncio.run(th.regenerate_still(71, "scene_1"))
    anchor = th._abs_from_rel(renv.db.rows[71]["local_path"])
    assert renv.gen["calls"][1]["sources"] == [anchor, anchor]
    sc = renv.db.md(71)["scenes"][1]
    assert sc["previous_keyframe_ids"] == [700] and sc["video_stale"] is True
    row = renv.db.rows[sc["keyframe_id"]]
    assert row["metadata"]["retry_kind"] == "regenerate" and row["parent_generation_id"] == 700
    assert row["feedback_text"] is None


def test_regenerate_keeps_the_first_generation_mode(renv):
    renv.ref_file("upload", 31, size=(720, 1280))  # exactly 9:16: no reframe
    # The still is the character photo itself (New bg OFF): the photo again.
    _single(renv, 72, lock=True, refs={"character_upload_id": 31}, main_as_is=True)
    asyncio.run(th.regenerate_still(72, "main"))
    assert renv.gen["calls"] == [], "the as-is photo needs no image call"
    assert renv.db.rows[72]["prompt_used"] == th._USE_AS_IS_PROMPT
    assert renv.db.md(72)["main_as_is"] is True
    # A generated still from a lone character pick (New bg ON, lock on):
    # keep the person, new background.
    _single(renv, 73, lock=True, refs={"character_upload_id": 31}, main_as_is=False)
    asyncio.run(th.regenerate_still(73, "main"))
    call, = renv.gen["calls"]
    assert call["prompt"].startswith("EDIT THE ATTACHED IMAGE")
    assert [p.name for p in call["sources"]] == ["upload-31.png"]
    assert renv.db.md(73)["main_as_is"] is False
    # No still yet (a failed tile): the Look step's default, the photo as is.
    renv.db.add_run(74, _md(lock=True, refs={"character_upload_id": 31}))
    asyncio.run(th.regenerate_still(74, "main"))
    assert renv.db.rows[74]["prompt_used"] == th._USE_AS_IS_PROMPT and len(renv.gen["calls"]) == 1


def test_a_failed_regenerate_keeps_the_error_on_a_failed_tile_only(renv, monkeypatch):
    renv.db.add_run(75, _md(scenes=2))
    renv.db.rows[75]["status"] = "failed"
    renv.gen["raise_on"] = lambda prompt, sources: True
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.regenerate_still(75, "main"))
    assert e.value.status_code == 502
    assert "model said no" in renv.db.md(75)["main_error"]
    with pytest.raises(HTTPException):
        asyncio.run(th.regenerate_still(75, "scene_1"))
    assert "model said no" in renv.db.md(75)["scenes"][1]["error"]
    assert renv.failed == []
    # A tile with a still keeps it, and no error is written on it.
    old = _single(renv, 76)
    with pytest.raises(HTTPException):
        asyncio.run(th.regenerate_still(76, "main"))
    assert renv.db.rows[76]["local_path"] == str(old) and "main_error" not in renv.db.md(76)


def test_a_quota_error_on_regenerate_is_a_429(renv, monkeypatch):
    async def quota(**kw):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(th, "_run_image_gen", quota)
    _single(renv, 77)
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.retry_preview(run_id=77, which="main", feedback_text=""))
    assert e.value.status_code == 429


def test_a_regenerated_tile_clears_its_failure(renv):
    renv.db.add_run(78, _md())
    renv.db.rows[78].update(status="failed", error="main_keyframe_failed: x")
    renv.db.md(78)["main_error"] = "x"
    asyncio.run(th.regenerate_still(78, "main"))
    run = renv.db.rows[78]
    assert run["status"] == "ready" and run["error"] is None and renv.db.md(78)["main_error"] is None


def test_batch_regen_images_regenerates_and_refines_only_with_text(renv):
    md = _md(scenes=3)
    md["scenes"][1].update(keyframe_id=801)
    md["scenes"][2].update(keyframe_id=802)
    renv.db.add_run(80, md)
    _seed_scene_row(renv, 80, 1, 801)
    _seed_scene_row(renv, 80, 2, 802)
    asyncio.run(th.batch_regen(run_id=80, items=[{"which": "scene_1"}, {"which": "scene_2"}],
                               target="images"))
    assert len(renv.gen["calls"]) == 2 and renv.edit["calls"] == []
    for c in renv.gen["calls"]:
        assert "Regenerate this image" not in c["prompt"] and "REFINEMENT" not in c["prompt"]
    asyncio.run(th.batch_regen(run_id=80, items=[{"which": "scene_1", "feedback_text": "warmer"}],
                               target="images"))
    assert len(renv.edit["calls"]) == 1 and len(renv.gen["calls"]) == 2
    assert "Regenerate this image." not in inspect.getsource(th.batch_regen)


def test_an_edit_regen_is_never_marked_as_is(renv):
    renv.ref_file("upload", 31, size=(720, 1280))
    md = _md(scenes=2, lock=True, refs={"character_upload_id": 31}, main_as_is=True)
    md["scenes"][1].update(keyframe_id=810, as_is=True)
    renv.db.add_run(81, md)
    _seed_old_main(renv, 81)
    _seed_scene_row(renv, 81, 1, 810)
    asyncio.run(th._regen_scene_keyframe(81, scene_index=0, keep_subject=True,
                                         has_char_ref=True, has_bg_ref=False))
    asyncio.run(th._regen_scene_keyframe(81, scene_index=1, keep_subject=True,
                                         has_char_ref=True, has_bg_ref=False))
    md = renv.db.md(81)
    assert md["main_as_is"] is False and md["scenes"][1]["as_is"] is False


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def test_undo_scene0_walks_back_through_the_versions(renv):
    old = _single(renv, 90)
    asyncio.run(th.refine_still(90, "main", "warmer light"))
    first = renv.db.rows[90]["local_path"]
    asyncio.run(th.refine_still(90, "main", "zoom out"))
    second = renv.db.rows[90]["local_path"]
    renv.db.md(90)["main_video_stale"] = False
    asyncio.run(th.undo_still(90, "main"))
    run, md = renv.db.rows[90], renv.db.md(90)
    assert run["local_path"] == first and run["feedback_text"] == "warmer light"
    assert (run["width"], run["height"]) == (756, 1344) and run["status"] == "ready"
    assert [v["local_path"] for v in md["still_versions"]["0"]] == [str(old)]
    assert [v["local_path"] for v in md["still_undone"]["0"]] == [second]
    assert Path(second).exists(), "the still taken off is kept"
    assert md["main_retry_kind"] == "refine" and md["main_video_stale"] is True
    asyncio.run(th.undo_still(90, "main"))
    run = renv.db.rows[90]
    assert run["local_path"] == str(old) and run["feedback_text"] == "far away shot"
    assert (run["width"], run["height"]) == (1248, 832) and run["prompt_used"] == "old prompt"
    assert renv.db.md(90)["still_versions"]["0"] == []
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.undo_still(90, "main"))
    assert e.value.status_code == 409


def test_undo_scene0_restores_as_is_and_skips_a_lost_version(renv):
    renv.db.add_run(91, _md(scenes=2))
    good = renv.tmp / "good.png"
    good.write_bytes(_png_bytes((720, 1280)))
    renv.db.md(91)["still_versions"] = {"0": [
        {"local_path": str(good), "storage_bucket": None, "storage_path": None, "mime_type": "image/png",
         "width": 720, "height": 1280, "image_model": "gemini_pro", "feedback_text": None,
         "retry_kind": None, "as_is": True, "prompt_used": th._USE_AS_IS_PROMPT},
        {"local_path": str(renv.tmp / "gone.png"), "storage_bucket": None, "storage_path": None,
         "width": 1, "height": 1, "as_is": False, "prompt_used": "lost"},
    ]}
    _seed_old_main(renv, 91)
    renv.db.rows[91]["error"] = "stale"
    asyncio.run(th.undo_still(91, "scene_0"))
    run, md = renv.db.rows[91], renv.db.md(91)
    assert run["local_path"] == str(good) and run["bytes"] == good.stat().st_size
    assert run["error"] is None and md["main_as_is"] is True and md["main_error"] is None
    assert md["still_versions"]["0"] == [] and md["scenes"][0]["video_stale"] is True
    assert "main_video_stale" not in md, "a multi-scene run: scene 0's flag"


def test_undo_scene1_repoints_to_the_previous_row(renv):
    md = _md(scenes=3)
    md["scenes"][1].update(keyframe_id=900, error="x")
    renv.db.add_run(92, md)
    _seed_scene_row(renv, 92, 1, 900, prompt=th._USE_AS_IS_PROMPT)
    out = asyncio.run(th.refine_still(92, "scene_1", "warmer light"))
    renv.db.md(92)["scenes"][1].update(video_stale=False, error="later")
    asyncio.run(th.undo_still(92, "scene_1"))
    sc = renv.db.md(92)["scenes"]
    assert sc[1]["keyframe_id"] == 900 and sc[1]["previous_keyframe_ids"] == []
    assert sc[1]["as_is"] is True and sc[1]["video_stale"] is True and sc[1]["error"] is None
    assert sc[0] == _scene(0) and sc[2] == _scene(2)
    assert out["id"] in renv.db.rows, "the row taken off stays (removed with the run)"
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.undo_still(92, "scene_1"))
    assert e.value.status_code == 409


def test_undo_scene1_skips_a_lost_row(renv):
    md = _md(scenes=2)
    md["scenes"][1].update(keyframe_id=912, previous_keyframe_ids=[910, 911])
    renv.db.add_run(93, md)
    _seed_scene_row(renv, 93, 1, 910)
    _seed_scene_row(renv, 93, 1, 912)  # 911 is gone
    asyncio.run(th.undo_still(93, 1))
    sc = renv.db.md(93)["scenes"][1]
    assert sc["keyframe_id"] == 910 and sc["previous_keyframe_ids"] == []


def test_undo_scene1_refuses_when_the_still_changed_meanwhile(renv, monkeypatch):
    md = _md(scenes=2)
    md["scenes"][1].update(keyframe_id=921, previous_keyframe_ids=[920])
    renv.db.add_run(94, md)
    _seed_scene_row(renv, 94, 1, 920)
    real = th._fetch_scene_row

    async def fetch_and_race(scene_id):
        renv.db.md(94)["scenes"][1]["keyframe_id"] = 999  # a concurrent new still
        return await real(scene_id)

    monkeypatch.setattr(th, "_fetch_scene_row", fetch_and_race)
    with pytest.raises(HTTPException) as e:
        asyncio.run(th.undo_still(94, "scene_1"))
    assert e.value.status_code == 409
    assert renv.db.md(94)["scenes"][1]["keyframe_id"] == 999


def test_undo_refuses_while_the_still_is_being_made(renv):
    md = _md(scenes=2)
    md["scenes"][1].update(keyframe_id=931, previous_keyframe_ids=[930], keyframe_generating=True)
    renv.db.add_run(95, md)
    for fn in (th.undo_still, th.regenerate_still):
        with pytest.raises(HTTPException) as e:
            asyncio.run(fn(95, "scene_1"))
        assert e.value.status_code == 409


def test_can_undo_follows_the_versions():
    assert th._can_undo({}, 0) is False
    assert th._can_undo({"still_versions": {"0": [{"local_path": "x"}]}}, 0) is True
    assert th._can_undo({"still_versions": {"0": []}}, 0) is False
    md = {"scenes": [_scene(0), _scene(1, previous_keyframe_ids=[3]), _scene(2)]}
    assert th._can_undo(md, 1) is True and th._can_undo(md, 2) is False
    src = inspect.getsource(th.fetch_run)
    assert '"can_undo": _can_undo(md, i)' in src and '"can_undo": _can_undo(md, 0)' in src


def test_delete_run_removes_the_stills_undo_took_off(monkeypatch, tmp_path):
    monkeypatch.setattr(th, "TH_ROOT", tmp_path / "th")

    async def fetch_run_row(run_id):
        return {"id": run_id, "storage_bucket": "b", "storage_path": "k/main-1.png",
                "local_path": "l/main-1.png",
                "metadata": {"still_versions": {"0": [{"storage_bucket": "b", "storage_path": "k/main.png"}]},
                             "still_undone": {"0": [{"storage_bucket": "b", "storage_path": "k/main-2.png"}]}}}

    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, sql, params=()):
            pass

        async def fetchall(self):
            return []

        async def fetchone(self):
            return {"id": 4}

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    deleted: list[str] = []

    async def delete(*, storage_bucket, storage_path, local_path):
        deleted.append(storage_path)

    monkeypatch.setattr(th, "_fetch_run_row", fetch_run_row)
    monkeypatch.setattr(th, "get_conn", lambda *a, **k: _Conn())
    monkeypatch.setattr(asset_storage, "delete", delete)
    asyncio.run(th.delete_run(4))
    assert deleted == ["k/main-1.png", "k/main.png", "k/main-2.png"]


# ---------------------------------------------------------------------------
# which, and the routes
# ---------------------------------------------------------------------------

def test_still_index():
    md = _md(scenes=3)
    assert th._still_index("main", md) == 0 and th._still_index("scene_0", md) == 0
    assert th._still_index("scene_2", md) == 2 and th._still_index(1, md) == 1
    for bad in ("scene_3", "intro", "scene_x", "scene_-1", 5):
        with pytest.raises(HTTPException) as e:
            th._still_index(bad, md)
        assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        th._still_index("scene_1", _md())  # a single-scene run has scene 0 only


def test_still_routes_are_registered_and_call_the_engines(monkeypatch):
    from app.routers import yas_producer as yp

    want = {f"/api/yas-producer/talking-head-v2/{{run_id}}/still/{{which}}/{a}"
            for a in ("refine", "regenerate", "undo")}
    methods: dict[str, set] = {}
    for r in yp.router.routes:
        if getattr(r, "path", None) in want:
            methods.setdefault(r.path, set()).update(r.methods or set())
    assert set(methods) == want and all("POST" in m for m in methods.values())

    seen: list[tuple] = []

    async def refine(run_id, which, change):
        seen.append(("refine", run_id, which, change))

    async def regenerate(run_id, which):
        seen.append(("regenerate", run_id, which))

    async def undo(run_id, which):
        seen.append(("undo", run_id, which))

    async def fetch_run(run_id, **_):
        return {"id": run_id}

    monkeypatch.setattr(th, "refine_still", refine)
    monkeypatch.setattr(th, "regenerate_still", regenerate)
    monkeypatch.setattr(th, "undo_still", undo)
    monkeypatch.setattr(th, "fetch_run", fetch_run)
    body = yp.THV2RefineStillRequest(change="warmer light")
    assert asyncio.run(yp.th_v2_refine_still(5, "scene_1", body)) == {"id": 5}
    assert asyncio.run(yp.th_v2_regenerate_still(5, "main")) == {"id": 5}
    assert asyncio.run(yp.th_v2_undo_still(5, "main")) == {"id": 5}
    assert seen == [("refine", 5, "scene_1", "warmer light"), ("regenerate", 5, "main"),
                    ("undo", 5, "main")]
    with pytest.raises(Exception):
        yp.THV2RefineStillRequest(change="x" * 2001)


# ---------------------------------------------------------------------------
# Frontend wiring (source guards; the button logic is in check-th-still.mjs)
# ---------------------------------------------------------------------------

def test_api_client_has_the_still_methods():
    src = _read("frontend/src/lib/api.ts")
    for name in ("refineStill: (", "regenerateStill: (", "undoStill: ("):
        assert name in src
    for action in ("refine", "regenerate", "undo"):
        assert f"`/api/yas-producer/talking-head-v2/${{runId}}/still/${{which}}/{action}`" in src


def test_the_stills_tiles_offer_refine_regenerate_and_undo():
    src = _read("frontend/src/components/yas/TalkingHeadV2Creator.tsx")
    assert "async function stillTileAction(" in src
    assert "talkingHeadV2.refineStill(" in src
    assert "talkingHeadV2.regenerateStill(" in src
    assert "talkingHeadV2.undoStill(" in src
    assert src.count("onRegenerate={") >= 2 and src.count("onUndo={") >= 2
    assert "can_undo" in src and "STILL_REFINE_PLACEHOLDER" in src
    lib = _read("frontend/src/lib/thStill.ts")
    assert "One change, e.g. warmer light, zoom out to above the hip" in lib
    types = _read("frontend/src/lib/types.ts")
    assert "can_undo?: boolean;" in types
