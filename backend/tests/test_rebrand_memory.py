"""Rebrand memory (S11): what each finished reel shipped, kept scene by scene.

  distill       a shipped recipe's choices by a plain rule: 815's stored
                recipes (the fixture) match their golden
  snapshot      once a reel's render is ready, one row per segment — an
                untouched one too — from the frozen plan, the version rows
                and the scene folders on disk; every paid client, the
                director, the icon planner and Whisper refusing; one batched
                embed, none for texts embedded before; a NULL vector when
                it fails or is off; an unchanged segment not written again;
                a retry writes nothing twice; nothing for a plan of another
                seq (a failed render never snapshots)
  the poller    the ready branch starts it in the background, bounded
  nearest       rated, then rebranded, then closest; the brand filter; the
                run's own rows left out; a query never embeds
  feedback      icon rows gain the scene's kind, each idea's source and
                state, whether it was seen in Suggested, and `ignored`
  the store     the conftest guard gives every test the memory store

Nothing reaches the database: the reel's store and this one are in memory."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from app.services import omni_client as omni
from app.services import person_matte
from app.services import th_reel
from app.services import vertex_client
from app.services.rebrand_harness import director as D
from app.services.rebrand_harness import icon_feedback as FB
from app.services.rebrand_harness import rebrand_memory as M
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness import words as W
from app.workers import yas_th_v2_composition_poller as poller

FIX = Path(__file__).parent / "fixtures"
STORED = json.loads((FIX / "rebrand_recipes" / "stored_815.json").read_text(encoding="utf-8"))["versions"]
GOLDEN = FIX / "rebrand_memory_distill_815.json"
RUN = 815
BRAND = {"visual_identity": {"primary_colors": ["#7F2BF2"], "secondary_colors": ["#303030"],
                             "style_pillars": ["bold"], "fonts": ["Jost"]}, "voice": {"pillars": ["plain"]}}


@pytest.fixture(autouse=True)
def refusing(monkeypatch, tmp_path):
    """Every paid client refuses (the embed is counted, and answers only
    when a test says so); the director, the icon planner and Whisper
    refuse; the studio's folder is the test's own."""
    calls: dict = {"embed": [], "refused": []}

    def refuse(name):
        async def _a(*_x, **_k):
            calls["refused"].append(name)
            raise AssertionError(f"{name} in a memory test")
        return _a

    for mod, name in ((vertex_client, "gemini_text"), (vertex_client, "gemini_multimodal"),
                      (vertex_client, "gemini_image"), (omni, "edit_video"), (person_matte, "person_mask"),
                      (D, "direct"), (studio, "icon_plan"), (studio, "final_icon_plan"), (studio, "icon_ctx")):
        monkeypatch.setattr(mod, name, refuse(f"{mod.__name__}.{name}"))

    def no(*_a, **_k):
        calls["refused"].append("sync")
        raise AssertionError("a slow or paid step in a memory test")

    monkeypatch.setattr(studio, "_icon_jobs", no)
    monkeypatch.setattr(W, "scene_words", no)
    monkeypatch.setattr(W, "transcribe", no)

    async def embed(texts, **kw):
        calls["embed"].append((list(texts), kw))
        if calls.get("embed_fails"):
            raise RuntimeError("vertex is down")
        return [[float(len(t) % 7 + 1), 1.0, float(i)] for i, t in enumerate(texts)]

    monkeypatch.setattr(vertex_client, "gemini_embed", embed)
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "_studio")
    monkeypatch.setenv("REBRAND_MEMORY_EMBED", "on")
    yield calls
    assert calls["refused"] == [], calls["refused"]


def test_the_conftest_guard_gives_every_test_the_memory_store():
    assert isinstance(M.store(), M.MemoryStore)


# ---------------------------------------------------------------------------
# distill
# ---------------------------------------------------------------------------

def test_distill_on_815s_stored_recipes_matches_its_golden():
    got = {vid: M.distill(v["recipe"], trim={"auto": 235, "end": 235, "user": False})
           for vid, v in sorted(STORED.items()) if v.get("recipe")}
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))
    # 815's v25: two patterns, neither moved by hand; the icons as it froze them.
    assert [p["slug"] for p in got["25"]["patterns"]] == ["structure", "beads_row"]
    assert all(p["hand"] is None for p in got["25"]["patterns"])
    assert [(i["glyph"], i["source"]) for i in got["25"]["icons"]["items"]] == \
        [("rocket-launch", "added"), ("piggy-bank", "ai")]
    assert M.distill(None) == {} and M.distill({}) == {}


def test_distill_says_every_choice_and_keeps_under_its_cap():
    recipe = {"engine": "house", "colours": {"source": "brand"}, "elements": [
        {"id": "pattern", "kind": "pattern", "z": "front",
         "placement": {"anchor": "edge-right", "offset": [0.04, -0.06], "size": 1.3, "rotate": 45.0},
         "animation": {"entrance": "fade", "start": {"mode": "at_sec", "sec": 1.5},
                       "end": {"mode": "at_sec", "sec": 6.0}, "exit": "fade", "exit_sec": 0.5},
         "props": {"slug": "beads_row", "colour": "#7f2bf2", "follow": "screen",
                   "resolved": {"layout": {"placement": "right"}, "colour": "#7f2bf2",
                                "hand": {"dx": 0.04, "dy": -0.06, "scale": 1.3, "rotate": 45.0, "pivot": [0.5, 0.5]}}}},
        {"id": "icons", "kind": "icon", "animation": {"entrance": "pop"},
         "props": {"style": "circle", "ai": True, "dismissed": ["users-three"],
                   "added": [{"glyph": {"source": "library", "id": "code"}, "label": "Code"}],
                   "tweaks": {"code": {"off_word": True, "sec": 2.0}},
                   "resolved": [{"glyph": {"source": "library", "id": "code"}, "label": "Code", "from_sec": 1.7,
                                 "settle_sec": 2.0, "to_sec": 3.6, "box": {"x": 0.1, "y": 0.2, "w": 0.16, "h": 0.09},
                                 "fill": "#7f2bf2", "ink": "#ffffff", "style": "circle", "layer": "behind",
                                 "fx": {"entrance": "pop", "seed": "s"}},
                                {"glyph": {"source": "library", "id": "piggy-bank"}, "label": "Free",
                                 "from_sec": 4.0, "settle_sec": 4.3, "to_sec": 5.5,
                                 "box": {"x": 0.6, "y": 0.2, "w": 0.16, "h": 0.09}, "fill": "#7f2bf2",
                                 "ink": "#ffffff", "word": "tuition", "fx": {"entrance": "pop", "seed": "s"}}]}}]}
    d = M.distill(recipe, trim={"auto": 235, "end": 200, "user": True})
    p = d["patterns"][0]
    assert (p["slug"], p["z"], p["colour_asked"], p["colour_drawn"], p["follow"]) == \
        ("beads_row", "front", "#7f2bf2", "#7f2bf2", "screen")
    assert p["hand"] == {"dx": 0.04, "dy": -0.06, "scale": 1.3, "rotate": 45.0}
    assert p["span"] == {"from": 1.5, "to": 6.0} and (p["exit"], p["exit_sec"]) == ("fade", 0.5)
    icons = d["icons"]
    assert icons["ai"] is True and icons["dismissed"] == ["users-three"]
    by = {i["glyph"]: i for i in icons["items"]}
    assert by["code"]["source"] == "added" and by["code"]["off_word"] and by["code"]["said_at"] == 2.0
    assert by["piggy-bank"]["source"] == "ai" and by["piggy-bank"]["word"] == "tuition"
    assert d["colours"] == {"source": "brand"} and d["trim"] == {"auto": 235, "end": 200, "user": True}
    huge = copy.deepcopy(recipe)
    huge["elements"][1]["props"]["resolved"] = [dict(recipe["elements"][1]["props"]["resolved"][0],
                                                     label="x" * 900) for _ in range(80)]
    assert len(json.dumps(M.distill(huge)).encode("utf-8")) <= M.CHOICES_MAX_BYTES


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------

def _version(vid: int, target: str, *, rating=None) -> dict:
    stored = next(v for _vid, v in sorted(STORED.items()) if v.get("recipe"))
    recipe = {**copy.deepcopy(stored["recipe"]), "engine": "house", "target": target}
    return {"id": vid, "run_id": RUN, "status": "ready",
            "options": {"target": target, "engine": "house", "recipe": recipe, "scene_kind": "main",
                        "seen_ideas": ["book-open", "code"]},
            "arms": {"omni": {"status": "ready", "engine": "house", "recipe_hash": f"h{vid}",
                              "house": {"engine_v": 1}}},
            "rating": rating}


def _world(tmp_path, *, seq=3, pick=None):
    """Run 815 finalized: intro (anim 352), main (anim 351, with a version
    picked or not) and outro (card), its frozen plan, the versions, and the
    main scene's studio folder with its words and the director's answer."""
    clips = {}
    for name in ("anim-351.mp4", "anim-352.mp4", "outro.png"):
        p = tmp_path / "clips" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(name.encode() * 50)
        clips[name] = p
    segs = [{"target": "intro", "role": "intro", "src": "anim:352", "version_id": None, "file_rel": "i", "frames": 120},
            {"target": "scene_0", "role": "main", "src": "anim:351", "version_id": pick, "file_rel": "m",
             "frames": 235, "end": {"src_fps": 24.0, "src_frames": 192, "full": 240, "auto": 235, "user": None}},
            {"target": "outro", "role": "outro", "src": "card:817", "version_id": None, "file_rel": "o",
             "frames": 120}]
    md = {"scene_count": 1, "aspect_ratio": "9:16", "main_animation_id": 351, "intro_animation_id": 352,
          "outro_animation_id": 353, "outro_image_id": 817, "composition_seq": seq,
          "composition_plan": {"seq": seq, "segments": segs, "payload": {}, "total_frames": 451},
          "script": {"dialogue": "42 Abu Dhabi is where your future becomes uncoded.",
                     "intro_headline": "Unlock Your Coding Future", "outro_headline": "Start Your Uncoded Future",
                     "outro_subtitle": "42abudhabi.ae"}}
    run = {"id": RUN, "topic": "Why 42", "metadata": md}
    anims = {351: {"id": 351, "status": "ready", "local_path": str(clips["anim-351.mp4"])},
             352: {"id": 352, "status": "ready", "local_path": str(clips["anim-352.mp4"])},
             353: {"id": 353, "status": "ready", "local_path": str(tmp_path / "gone.mp4")}}
    exps = {9: _version(9, "scene_0", rating={"omni_stars": 4, "notes": "nice"})}
    th_reel.set_store(th_reel.MemoryStore({RUN: run}, anim_rows=anims, exps=exps))
    folder = studio.scene_base(RUN, "scene_0", clips["anim-351.mp4"])
    (folder / "icons").mkdir(parents=True, exist_ok=True)
    (folder / "icons" / "words.json").write_text(json.dumps({
        "digest": "wd1", "transcript": "42 Abu Dhabi is where your future becomes uncoded.",
        "words": [{"text": "42", "said": True}]}), encoding="utf-8")
    (folder / "icons" / "director.json").write_text(json.dumps({
        "key": "dk", "version": 3, "source": "ai", "inputs": {"model": "gemini-2.5-flash"},
        "ideas": [{"icon": "piggy-bank", "auto": True, "verdict": "keep"},
                  {"icon": "code", "auto": True, "verdict": "keep"},
                  {"icon": "book-open", "auto": False, "verdict": "suggest"},
                  {"icon": "rocket-launch", "auto": False, "verdict": "refuse"}]}), encoding="utf-8")
    (folder / "colours.json").write_text(json.dumps({"colour": "#7f2bf2", "ground": "#f0f0f0", "ink": "#ffffff",
                                                     "fell_back": False}), encoding="utf-8")
    mem = M.MemoryStore(brand=BRAND, scripts={RUN: [1.0, 1.0, 0.0]})
    M.set_store(mem)

    async def card(card_id):
        return clips["outro.png"] if int(card_id or 0) == 817 else None

    return run, md, mem, card


def test_a_ready_reel_is_kept_scene_by_scene_for_one_embed(tmp_path, monkeypatch, refusing):
    run, md, mem, card = _world(tmp_path, pick=9)
    monkeypatch.setattr(poller, "_fetch_card_path", card)
    n = asyncio.run(M.snapshot_ready(RUN, 3))
    assert n == 3 and len(mem.rows) == 3
    by = {r["target"]: r for r in mem.rows}
    main = by["scene_0"]
    assert (main["scene_kind"], main["scene_index"], main["scene_count"], main["aspect"]) == ("main", 0, 1, "9:16")
    assert (main["version_id"], main["rebranded"], main["engine"], main["engine_v"], main["recipe_hash"]) == \
        (9, True, "house", 1, "h9")
    assert (main["src_fps"], main["src_frames"], main["auto_end"], main["end_frame"], main["user_end"]) == \
        (24.0, 192, 235, 235, False)
    assert main["duration_sec"] == pytest.approx(235 / 30, abs=1e-3)
    assert main["topic"] == "Why 42" and main["line"].startswith("42 Abu Dhabi")
    assert main["transcript"].startswith("42 Abu Dhabi") and main["words_digest"] == "wd1"
    assert main["colours"]["colour"] == "#7f2bf2"
    assert main["brand"]["primary_colors"] == ["#7F2BF2"] and len(main["brand_sha"]) == 16
    assert main["choices"]["engine"] == "house" and main["choices"]["trim"]["end"] == 235
    assert main["director"]["key"] == "dk" and main["director"]["top"] == ["code", "piggy-bank"]
    assert main["director"]["refused"] == ["rocket-launch"] and main["director"]["seen"] == ["book-open", "code"]
    assert main["director"]["ignored"] == ["book-open"]
    assert (main["rating"], main["rating_notes"]) == (4, "nice")
    assert main["embed_text"] == "main | 9:16 | topic: Why 42 | said: " + main["line"]
    # Untouched segments are rows too (a skip is data).
    intro, outro = by["intro"], by["outro"]
    assert intro["rebranded"] is False and intro["choices"] == {} and intro["version_id"] is None
    assert intro["director"] is None and outro["line"] == "Start Your Uncoded Future — 42abudhabi.ae"
    assert all(r["embedding"] for r in mem.rows)
    # One batched embed, of the three texts, at 768 dimensions.
    assert len(refusing["embed"]) == 1 and len(refusing["embed"][0][0]) == 3
    assert refusing["embed"][0][1] == {"dim": 768}


def test_a_retry_or_an_unchanged_reel_writes_and_embeds_nothing_again(tmp_path, monkeypatch, refusing):
    run, md, mem, card = _world(tmp_path, pick=9)
    monkeypatch.setattr(poller, "_fetch_card_path", card)
    assert asyncio.run(M.snapshot_ready(RUN, 3)) == 3
    assert asyncio.run(M.snapshot_ready(RUN, 3)) == 0, "a retry: the same seq"
    # Finalized again with nothing changed: no new rows.
    md2 = copy.deepcopy(md)
    md2["composition_plan"]["seq"] = 4
    assert asyncio.run(M.snapshot_ready(RUN, 4, md2)) == 0
    # The main scene's end moved: only its row is new — and its text was
    # embedded before, so no call.
    md3 = copy.deepcopy(md2)
    md3["composition_plan"]["seq"] = 5
    md3["composition_plan"]["segments"][1].update(frames=200, end={**md3["composition_plan"]["segments"][1]["end"],
                                                                  "user": 200})
    assert asyncio.run(M.snapshot_ready(RUN, 5, md3)) == 1
    new = mem.rows[-1]
    assert (new["target"], new["end_frame"], new["user_end"], new["finalize_seq"]) == ("scene_0", 200, True, 5)
    assert new["embedding"] == next(r for r in mem.rows if r["target"] == "scene_0")["embedding"]
    assert len(refusing["embed"]) == 1


def test_no_row_for_a_plan_of_another_seq_and_a_null_vector_when_the_embed_fails(tmp_path, monkeypatch, refusing):
    run, md, mem, card = _world(tmp_path)
    monkeypatch.setattr(poller, "_fetch_card_path", card)
    assert asyncio.run(M.snapshot_ready(RUN, 2)) == 0 and mem.rows == [], "not the plan that rendered"
    refusing["embed_fails"] = True
    assert asyncio.run(M.snapshot_ready(RUN, 3)) == 3
    assert all(r["embedding"] is None for r in mem.rows)
    monkeypatch.setenv("REBRAND_MEMORY_EMBED", "off")
    md2 = copy.deepcopy(md)
    md2["composition_plan"]["seq"] = 4
    md2["composition_plan"]["segments"][0]["frames"] = 100
    assert asyncio.run(M.snapshot_ready(RUN, 4, md2)) == 1 and mem.rows[-1]["embedding"] is None
    assert len(refusing["embed"]) == 1, "off: no call"


def test_the_scene_folders_are_found_off_the_event_loop(tmp_path, monkeypatch, refusing):
    """Review finding 7: scene_base hashes (and on a cold cache decodes) the
    clip, 0.79 s a scene, and ran on the loop, where the snapshot's wait_for
    couldn't stop it. It runs in a thread now."""
    import threading

    run, md, mem, card = _world(tmp_path, pick=9)
    monkeypatch.setattr(poller, "_fetch_card_path", card)
    real = studio.scene_base
    threads: list[int] = []

    def scene_base(*a, **k):
        threads.append(threading.get_ident())
        return real(*a, **k)

    monkeypatch.setattr(studio, "scene_base", scene_base)

    async def go():
        loop = threading.get_ident()
        rows = await M.rows_for(run, 3)
        return loop, rows

    loop, rows = asyncio.run(go())
    assert len(rows) == 3 and threads and loop not in threads
    assert next(r for r in rows if r["target"] == "scene_0")["words_digest"] == "wd1"


def test_a_snapshot_never_raises_and_never_waits_forever(tmp_path, monkeypatch, refusing):
    class Down(M.MemoryStore):
        async def insert_many(self, rows):
            raise OSError("the database is down")

    run, md, mem, card = _world(tmp_path)
    monkeypatch.setattr(poller, "_fetch_card_path", card)
    M.set_store(Down(brand=BRAND))
    assert asyncio.run(M.snapshot_ready(RUN, 3)) == 0
    monkeypatch.setattr(M, "SNAPSHOT_TIMEOUT_SEC", 0.05)

    class Slow(M.MemoryStore):
        async def brand(self):
            await asyncio.sleep(5)

    M.set_store(Slow())
    assert asyncio.run(M.snapshot_ready(RUN, 3)) == 0


def test_the_pollers_ready_branch_starts_the_snapshot(tmp_path, monkeypatch, refusing):
    run, md, mem, card = _world(tmp_path)
    started: list = []
    monkeypatch.setattr(M, "start_snapshot", lambda run_id, seq, md=None: started.append((run_id, seq)))

    async def no_persist(**_k):
        return None, None

    from app.services import asset_storage

    monkeypatch.setattr(asset_storage, "persist_local_file", no_persist)
    out = tmp_path / "final-3.mp4"
    out.write_bytes(b"reel")
    asyncio.run(poller._plan_ready(RUN, {**md, "composition_inputs_sha": "s"}, out, 0.0))
    assert started == [(RUN, 3)]


# ---------------------------------------------------------------------------
# nearest
# ---------------------------------------------------------------------------

def _row(i, run, kind, vec, *, rebranded=False, rating=None, brand="b1", target="scene_0"):
    return {"run_id": run, "finalize_seq": 1, "target": target, "scene_kind": kind, "rebranded": rebranded,
            "rating": rating, "brand_sha": brand, "embedding": vec, "embed_sha": f"e{i}", "created_at": i}


def test_nearest_ranks_rated_then_rebranded_then_closest(refusing):
    mem = M.MemoryStore(scripts={1: [1.0, 0.0]})
    M.set_store(mem)
    asyncio.run(mem.insert_many([
        _row(1, 2, "main", [1.0, 0.0]), _row(2, 3, "main", [0.0, 1.0], rebranded=True),
        _row(3, 4, "main", [0.9, 0.1]), _row(4, 5, "main", [0.0, 1.0], rating=5),
        _row(5, 6, "main", [1.0, 0.0], brand="b2"), _row(6, 7, "intro", [1.0, 0.0]),
        _row(7, 1, "main", [1.0, 0.0]),                           # the query run's own
    ]))
    got = asyncio.run(M.nearest("main", run_id=1))
    # Rated, rebranded, then closest (a tie: the newest first).
    assert [r["run_id"] for r in got] == [5, 3, 6, 2, 4]
    got = asyncio.run(M.nearest("main", run_id=1, brand_sha="b1"))
    assert [r["run_id"] for r in got] == [5, 3, 2, 4]
    assert "embedding" not in got[0]
    got = asyncio.run(M.nearest("main", vec=[0.0, 1.0], exclude_run=5, k=2))
    assert [r["run_id"] for r in got] == [3, 4]
    with pytest.raises(ValueError):
        asyncio.run(M.nearest("reel"))
    assert refusing["embed"] == [], "a query never pays"


# ---------------------------------------------------------------------------
# The icon feedback log
# ---------------------------------------------------------------------------

def test_icon_feedback_says_the_ideas_source_state_and_what_was_seen_and_left():
    director = {"key": "k", "version": 3, "source": "ai", "inputs": {"model": "m"},
                "ideas": [{"icon": "piggy-bank", "auto": True, "verdict": "keep", "w": 1, "priority": 5},
                          {"icon": "book-open", "auto": False, "verdict": "suggest", "w": 2, "priority": 2},
                          {"icon": "rocket-launch", "auto": False, "verdict": "refuse", "w": 3}]}
    props = rc.IconProps.model_validate({"added": [{"glyph": {"source": "library", "id": "code"}, "label": "Code"}]})
    shown = [{"glyph": {"source": "library", "id": g}} for g in ("piggy-bank", "code")]
    ideas = [{"glyph": {"source": "library", "id": "piggy-bank"}, "state": "on", "source": "ai", "auto": True},
             {"glyph": {"source": "library", "id": "book-open"}, "state": "available", "source": "ai"},
             {"glyph": {"source": "library", "id": "rocket-launch"}, "state": "refused", "source": "ai"},
             {"glyph": {"source": "library", "id": "code"}, "state": "on", "source": "user", "added": True}]
    rows = FB.feedback_of(director, props, shown, ideas=ideas, seen=["book-open", "rocket-launch", "code"])
    by = {r["glyph"]: r for r in rows}
    assert {g: r["outcome"] for g, r in by.items()} == {
        "piggy-bank": "shown_ai", "code": "added_library", "book-open": "ignored"}
    assert (by["book-open"]["idea_source"], by["book-open"]["idea_state"], by["book-open"]["seen"]) == \
        ("ai", "available", True)
    assert by["piggy-bank"]["seen"] is False and by["code"]["seen"] is True
    assert "ignored" in FB.OUTCOMES
    # Without what was seen: exactly the rows it always wrote.
    assert {r["glyph"] for r in FB.feedback_of(director, props, shown)} == {"piggy-bank", "code"}


def test_icon_feedback_rows_carry_the_scene_kind_and_the_new_columns():
    assert {"scene_kind", "idea_source", "idea_state", "seen"} <= set(FB._COLUMNS)
    ddl = " ".join(FB._DDL_COLUMNS)
    for col in ("scene_kind", "idea_source", "idea_state", "seen"):
        assert f"ADD COLUMN IF NOT EXISTS {col} " in ddl


def test_the_new_modules_call_no_model_but_the_one_embed():
    here = Path(M.__file__).parent
    src = (here / "rebrand_memory.py").read_text(encoding="utf-8")
    for call in ("gemini_image(", "gemini_multimodal(", "gemini_text(", "person_mask(", "edit_video(",
                 "direct(", "_icon_jobs(", "icon_plan(", "icon_ctx(", "scene_words("):
        assert call not in src, call
    assert src.count("gemini_embed(") == 1


# ---------------------------------------------------------------------------
# The report and the backfill scripts
# ---------------------------------------------------------------------------

def _script(name: str):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_report_sums_the_rows_read_only(tmp_path, monkeypatch, refusing):
    run, md, mem, card = _world(tmp_path, pick=9)
    monkeypatch.setattr(poller, "_fetch_card_path", card)
    asyncio.run(M.snapshot_ready(RUN, 3))
    report = _script("rebrand_choices_report")
    s = report.summary(mem.rows)
    assert (s["rows"], s["runs"], s["with_vector"], s["brands"]) == (3, 1, 3, 1)
    main = s["kinds"]["main"]
    assert (main["segments"], main["rebranded"], main["trimmed"], main["rated"]) == (1, 1, 0, 1)
    assert s["kinds"]["intro"]["rebranded"] == 0 and main["ignored_most"] == [("book-open", 1)]
    assert "3 segments from 1 reels" in report.render(s)
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps(mem.rows, default=str), encoding="utf-8")
    assert report.main(["--json", str(rows_file)]) == 0


def test_the_backfill_is_a_dry_run_unless_asked(tmp_path, monkeypatch, refusing):
    run, md, mem, card = _world(tmp_path, pick=9)
    monkeypatch.setattr(poller, "_fetch_card_path", card)
    backfill = _script("rebrand_choices_backfill")

    async def runs(limit):
        return [{"id": RUN, "topic": run["topic"], "metadata": md, "seq": 3}]

    async def none(limit):
        return []

    monkeypatch.setattr(backfill, "_runs", runs)
    monkeypatch.setattr(backfill, "_null_vectors", none)
    monkeypatch.setattr(backfill, "_ReadOnlyReel", lambda: th_reel.store())   # the test's memory store
    dry = asyncio.run(backfill.rows(False, None))
    assert dry["new_rows"] == 3 and dry["written"] == 0 and mem.rows == [] and refusing["embed"] == []
    done = asyncio.run(backfill.rows(True, None))
    assert done["written"] == 3 and len(mem.rows) == 3
    again = asyncio.run(backfill.rows(True, None))
    assert again["new_rows"] == 0 and again["written"] == 0, "a seq already kept writes nothing"


# ---------------------------------------------------------------------------
# seen_ideas: from Generate to the version's options
# ---------------------------------------------------------------------------

def test_generate_stores_the_ideas_seen_in_suggested(monkeypatch, refusing):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt
    from app.services import rebrand_pipeline as pipeline
    from app.services import rebrand_store as store
    from app.services import yas_talking_head_v2 as th

    seen: dict = {}

    async def start(run_id, raw, recipe=None, base=None, **kw):
        seen["kw"] = kw
        return {"id": 5, "run_id": run_id, "status": "queued", "options": {"target": recipe.target}}

    app = FastAPI()
    app.include_router(rt.router)
    c = TestClient(app)
    recipe = {"engine": "house", "elements": []}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "start", start)
        r = c.post("/api/rebrand/harness/versions", json={"run_id": RUN, "target": "scene_0", "recipe": recipe,
                                                          "seen_ideas": ["book-open", "code"]})
        assert r.status_code == 200, r.text
        assert seen["kw"] == {"seen_ideas": ["book-open", "code"]}
        c.post("/api/rebrand/harness/versions", json={"run_id": RUN, "target": "scene_0", "recipe": recipe})
        assert seen["kw"] == {}, "a client that sends none: the very call as before"

    # pipeline.start keeps them beside the recipe (JSON only).
    created: list = []

    async def run_row(run_id):
        return {"id": run_id, "topic": "t", "metadata": {"scene_count": 1, "aspect_ratio": "9:16"}}

    async def none(*_a, **_k):
        return []

    async def create(run_id, options):
        created.append(options)
        return {"id": 6, "run_id": run_id, "options": options, "status": "queued"}

    async def no_run(*_a, **_k):
        return None

    monkeypatch.setattr(th, "_fetch_run_row", run_row)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", no_run)
    house = rc.normalize({"engine": "house", "target": "scene_0", "elements": []})
    asyncio.run(pipeline.start(RUN, None, recipe=house, seen_ideas=["book-open"], variant="full", end_frame=200))
    opts = created[-1]
    assert opts["seen_ideas"] == ["book-open"] and opts["variant"] == "full" and opts["end_frame"] == 200
    asyncio.run(pipeline.start(RUN, None, recipe=house))
    assert "seen_ideas" not in created[-1] and "variant" not in created[-1]
