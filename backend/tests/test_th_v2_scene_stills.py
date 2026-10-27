"""TH v2 scene stills: the Phase 0 foundations.

- A prompt never names a reference image that is not attached. A lock with no
  person image used to select "FACE-AND-OUTFIT LOCK. The attached PERSON
  reference IS the speaker" with nothing attached (runs 799 / 808 / 827): the
  model invented a random face and the script subject was dropped.
- One per-scene resolver: a scene's own pick wins per slot, and the brief a
  prompt describes comes from the same pick as the image attached.
- Scene 0's still is stored as a NEW file per version (main-<stamp>.png); the
  replaced one stays on disk and in metadata.still_versions["0"]. Scenes >= 1
  get a row per version with lineage; scenes[i] is repointed atomically.
- metadata.scenes[i] writes are per scene (jsonb_set under a row lock), never a
  whole-array rewrite.
- edit_run makes no keyframe for a run that has no stills yet (the create-flow
  trap of run 827).
- card_animation's edit renderer takes an opt-in aspect_ratio.

No database and no model calls: yas_generations is an in-memory fake that
understands the statements these helpers issue (and enforces the table's
retry_kind CHECK); every image call is a fake that records its prompt.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import re
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image
from psycopg.types.json import Jsonb

from app.services import asset_storage
from app.services import card_animation as ca
from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp

PHANTOM = ("FACE-AND-OUTFIT LOCK", "attached PERSON reference")
SUBJECT = "an Emirati man in a white kandura and ghutra"


def _png_bytes(size=(768, 1344), color=(10, 20, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _norm(sql: str) -> str:
    return " ".join(sql.split())


# ---------------------------------------------------------------------------
# In-memory yas_generations
# ---------------------------------------------------------------------------

class FakePg:
    """The yas_generations rows, and the SQL the scene-still helpers send."""

    RETRY_KINDS = (None, "tweak", "pivot")  # yas_generations_retry_kind_check

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.next_id = 900
        self.log: list[str] = []
        self.clock = 0

    def add_run(self, run_id: int, md: dict, **cols) -> dict:
        row = {
            "id": run_id, "mode": th.MODE_PREVIEW, "topic": "Why peer learning works",
            "status": "ready", "image_model": "gemini_pro", "prompt_used": "script prompt",
            "local_path": None, "storage_bucket": None, "storage_path": None,
            "mime_type": None, "bytes": None, "width": None, "height": None,
            "feedback_text": None, "retry_kind": None, "parent_generation_id": None,
            "error": None, "metadata": md, "updated_at": 0,
        }
        row.update(cols)
        self.rows[run_id] = row
        return row

    def md(self, run_id: int) -> dict:
        return self.rows[run_id]["metadata"]

    def _touch(self, row: dict) -> None:
        self.clock += 1
        row["updated_at"] = self.clock

    def run_sql(self, sql: str, p: tuple):  # noqa: C901 (one branch per statement)
        s = _norm(sql)
        self.log.append(s)
        if s.startswith("SELECT metadata->'scenes'->(%s::int) AS scene FROM yas_generations"):
            assert "FOR UPDATE" in s
            i, rid, mode = p
            row = self.rows.get(rid)
            if not row or row["mode"] != mode:
                return None
            sc = row["metadata"].get("scenes")
            val = copy.deepcopy(sc[i]) if isinstance(sc, list) and 0 <= i < len(sc) else None
            return {"scene": val}
        if s.startswith("UPDATE yas_generations SET metadata = jsonb_set(metadata, %s::text[], %s::jsonb, false)"):
            path, val, rid = p
            assert path[0] == "scenes"
            row = self.rows[rid]
            row["metadata"]["scenes"][int(path[1])] = copy.deepcopy(val.obj)
            self._touch(row)
            return None
        if s.startswith("SELECT local_path, storage_bucket, storage_path, mime_type, width, height, "
                        "image_model, feedback_text, prompt_used, metadata FROM yas_generations"):
            assert "FOR UPDATE" in s
            rid, mode = p
            row = self.rows.get(rid)
            return copy.deepcopy(row) if row and row["mode"] == mode else None
        if "feedback_text = COALESCE(%s, feedback_text)" in s:
            (bucket, spath, lpath, mime, nbytes, w, h, prompt, status, model, fb, rk, *rest) = p
            rid, metas = rest[-1], rest[:-1]
            assert rk in self.RETRY_KINDS, f"retry_kind {rk!r} violates the CHECK"
            row = self.rows[rid]
            row.update(storage_bucket=bucket, storage_path=spath, local_path=lpath, mime_type=mime,
                       bytes=nbytes, width=w, height=h, prompt_used=prompt, status=status,
                       image_model=model)
            if fb is not None:
                row["feedback_text"] = fb
            if rk is not None:
                row["retry_kind"] = rk
            if "error = NULL" in s:
                row["error"] = None
            row["metadata"].update(copy.deepcopy(metas[0].obj))
            if "'{scenes,0}'" in s:
                row["metadata"]["scenes"][0] = copy.deepcopy(metas[1].obj)
            self._touch(row)
            return None
        if "feedback_text = %s, retry_kind = %s, status = %s, error = NULL" in s:  # undo, scene 0
            (bucket, spath, lpath, mime, nbytes, w, h, model, prompt, fb, rk, status, *rest) = p
            rid, metas = rest[-1], rest[:-1]
            assert rk in self.RETRY_KINDS, f"retry_kind {rk!r} violates the CHECK"
            row = self.rows[rid]
            row.update(storage_bucket=bucket, storage_path=spath, local_path=lpath, mime_type=mime,
                       bytes=nbytes, width=w, height=h, image_model=model, prompt_used=prompt,
                       feedback_text=fb, retry_kind=rk, status=status, error=None)
            row["metadata"].update(copy.deepcopy(metas[0].obj))
            if "'{scenes,0}'" in s:
                row["metadata"]["scenes"][0] = copy.deepcopy(metas[1].obj)
            self._touch(row)
            return None
        if s.startswith("INSERT INTO yas_generations"):
            cols = [c.strip() for c in re.search(r"INSERT INTO yas_generations \(([^)]*)\)", s).group(1).split(",")]
            vals = {c: (v.obj if isinstance(v, Jsonb) else v) for c, v in zip(cols, p)}
            assert vals.get("retry_kind") in self.RETRY_KINDS, "retry_kind CHECK"
            self.next_id += 1
            row = {"id": self.next_id, "local_path": None, "storage_bucket": None, "storage_path": None,
                   "feedback_text": None, "retry_kind": None, "parent_generation_id": None,
                   "updated_at": 0, **vals}
            self.rows[self.next_id] = row
            return {"id": self.next_id}
        if s.startswith("UPDATE yas_generations SET storage_bucket = %s, storage_path = %s, local_path = %s, "
                        "mime_type = %s, bytes = %s, width = %s, height = %s, status = %s, updated_at = NOW() "
                        "WHERE id = %s"):
            bucket, spath, lpath, mime, nbytes, w, h, status, rid = p
            row = self.rows[rid]
            row.update(storage_bucket=bucket, storage_path=spath, local_path=lpath, mime_type=mime,
                       bytes=nbytes, width=w, height=h, status=status)
            self._touch(row)
            return None
        if s.startswith("UPDATE yas_generations SET status = %s, error = %s"):
            status, err, rid = p
            self.rows[rid].update(status=status, error=err)
            return None
        if s.startswith("UPDATE yas_generations SET metadata = metadata || %s::jsonb WHERE id = %s "
                        "AND COALESCE(metadata->>'motion_warm', '') <> %s RETURNING id"):
            # A card's one background plan (_claim_card_plan): set only when
            # not set to that version yet, atomically.
            patch, rid, version = p
            row = self.rows.get(rid)
            cur = (row or {}).get("metadata", {}).get("motion_warm")
            if row is None or ("" if cur is None else str(cur)) == version:
                return None
            row["metadata"].update(copy.deepcopy(patch.obj))
            return {"id": rid}
        if s.startswith("SELECT * FROM yas_generations WHERE id = %s AND mode = %s"):
            rid, mode = p
            row = self.rows.get(rid)
            return copy.deepcopy(row) if row and row["mode"] == mode else None
        raise AssertionError(f"unexpected SQL: {s[:160]}")

    def get_conn(self):
        db = self

        class _Cur:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, sql, params=()):
                self._one = db.run_sql(sql, tuple(params))

            async def fetchone(self):
                return self._one

            async def fetchall(self):
                return self._one or []

        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def cursor(self):
                return _Cur()

        return lambda *a, **k: _Conn()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A fake run store, reference files, briefs and image model."""
    db = FakePg()
    monkeypatch.setattr(th, "get_conn", db.get_conn())
    monkeypatch.setattr(th, "TH_ROOT", tmp_path / "th")

    async def fetch_run_row(run_id):
        row = db.rows.get(run_id)
        if not row or row["mode"] != th.MODE_PREVIEW:
            raise HTTPException(404, "nope")
        return copy.deepcopy(row)

    async def patch_metadata(run_id, patch):
        db.rows[run_id]["metadata"].update(copy.deepcopy(patch))
        db._touch(db.rows[run_id])

    async def fetch_run(run_id, **_):
        return {"id": run_id}

    async def brand_req():
        return ""

    monkeypatch.setattr(th, "_fetch_run_row", fetch_run_row)
    monkeypatch.setattr(th, "_patch_metadata", patch_metadata)
    monkeypatch.setattr(th, "fetch_run", fetch_run)
    monkeypatch.setattr(th, "build_brand_requirements", brand_req)

    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    files: dict[tuple[str, int], Path] = {}
    missing: set[tuple[str, int]] = set()

    def ref_file(kind: str, ident: int, size=(1024, 1024)) -> Path:
        p = refs_dir / f"{kind}-{ident}.png"
        Image.new("RGB", size, (200, 100, 50)).save(p)
        files[(kind, ident)] = p
        return p

    async def slot_path(*, asset_id, upload_id):
        key = ("upload", upload_id) if upload_id else ("asset", asset_id) if asset_id else None
        if key is None:
            return None
        if key in missing:
            raise HTTPException(410, "file missing on disk")
        return files[key]

    async def slot_row_and_brief(*, asset_id, upload_id, kind):
        if not (asset_id or upload_id):
            return None, None
        return {"id": asset_id or upload_id}, f"BRIEF[{kind} asset={asset_id} upload={upload_id}]"

    monkeypatch.setattr(th, "_slot_path", slot_path)
    monkeypatch.setattr(th, "_slot_row_and_brief", slot_row_and_brief)

    gen = {"calls": [], "size": (768, 1344), "raise_on": None}

    async def run_image_gen(*, prompt, image_model, source_images=None, aspect="9:16", size=None,
                            conform="reframe"):
        gen["calls"].append({"prompt": prompt, "sources": source_images, "aspect": aspect,
                             "conform": conform})
        if gen["raise_on"] and gen["raise_on"](prompt, source_images):
            raise RuntimeError("model said no")
        return _png_bytes(gen["size"]), "image/png"

    monkeypatch.setattr(th, "_run_image_gen", run_image_gen)

    # Tripwire: an offline test must never reach a real image model (the
    # machine may have Google credentials). A test that exercises Refine
    # replaces card_animation.render_card_image with its own fake.
    async def no_real_model(*a, **k):
        raise AssertionError("a real image-model call in an offline test")

    monkeypatch.setattr(ca, "gemini_image_multipart", no_real_model)
    monkeypatch.setattr(th, "gemini_image", no_real_model)

    # Refine's image edit (card_animation._render_edit_escalating renders
    # through render_card_image): records each call, returns `size`.
    edit = {"calls": [], "size": (768, 1344)}

    async def render_card_image(parts, *, system_instruction, timeout, aspect_ratio=None):
        edit["calls"].append({"parts": list(parts), "system": system_instruction,
                              "timeout": timeout, "aspect": aspect_ratio})
        return _png_bytes(edit["size"])

    monkeypatch.setattr(ca, "render_card_image", render_card_image)

    uploads: list[str] = []

    async def upload(*, data, path, mime):
        uploads.append(path)
        return {"storage_bucket": "producer-media", "storage_path": path, "local_path": None}

    monkeypatch.setattr(asset_storage, "upload", upload)

    class Env:
        pass

    e = Env()
    e.db, e.gen, e.ref_file, e.missing, e.uploads, e.tmp = db, gen, ref_file, missing, uploads, tmp_path
    e.edit = edit
    return e


def _scene(i: int, **kw) -> dict:
    base = {"index": i, "dialogue": f"line {i}", "hook": "", "user_authored": False,
            "keyframe_id": None, "animation_id": None,
            "character_asset_id": None, "character_upload_id": None,
            "bg_asset_id": None, "bg_upload_id": None, "excluded": False, "video_stale": False}
    base.update(kw)
    return base


def _md(*, scenes: int = 1, lock: bool = True, refs: dict | None = None, aspect="9:16", **kw) -> dict:
    md = {
        "category": "talking_head",
        "script": {"subject": SUBJECT, "action": "speaking", "camera": "Medium close-up",
                   "lighting": "soft daylight", "intro_headline": "Hi", "outro_headline": "Bye"},
        "refs": {"intro_ref_mode": "new_vibe", "outro_ref_mode": "new_vibe", **(refs or {})},
        "lock_character": lock, "image_models": {}, "aspect_ratio": aspect,
        "intro_image_id": None, "outro_image_id": None,
        "intro_excluded": True, "outro_excluded": True,
    }
    if scenes > 1:
        md["scene_count"] = scenes
        md["scenes"] = [_scene(i) for i in range(scenes)]
    md.update(kw)
    return md


def _no_phantom(prompt: str) -> None:
    for bad in PHANTOM:
        assert bad not in prompt, f"prompt names a person reference that is not attached: {bad!r}"


# ---------------------------------------------------------------------------
# 0a: build_main_keyframe_prompt names only what is attached
# ---------------------------------------------------------------------------

SCRIPT = {"subject": SUBJECT, "action": "speaking", "camera": "Medium close-up",
          "lighting": "soft daylight"}


def test_a_lock_with_no_person_image_is_not_a_face_lock():
    p = thp.build_main_keyframe_prompt(script=SCRIPT, character_brief=None, bg_brief=None,
                                       lock_character=True)
    _no_phantom(p)
    assert SUBJECT in p, "the free-gen prompt carries the script subject"


def test_a_character_brief_without_its_image_is_inspiration_only():
    p = thp.build_main_keyframe_prompt(script=SCRIPT, character_brief="BRIEF: a student",
                                       bg_brief=None, lock_character=True)
    _no_phantom(p)
    assert "BRIEF: a student" in p and "inspiration" in p


def test_an_attached_person_image_still_locks_the_face():
    p = thp.build_main_keyframe_prompt(script=SCRIPT, character_brief=None, bg_brief=None,
                                       lock_character=True, has_character_ref=True)
    assert p.startswith("FACE-AND-OUTFIT LOCK")


def test_a_background_only_still_keeps_the_background_and_casts_the_subject():
    p = thp.build_main_keyframe_prompt(script=SCRIPT, character_brief=None, bg_brief="Library",
                                       lock_character=True, has_bg_ref=True)
    _no_phantom(p)
    assert p.startswith("KEEP THE ATTACHED BACKGROUND") and SUBJECT in p


def test_the_default_is_not_inferred_from_the_lock():
    sig = inspect.signature(thp.build_main_keyframe_prompt)
    assert sig.parameters["has_character_ref"].default is False
    assert "has_character_ref = bool(lock_character)" not in inspect.getsource(thp.build_main_keyframe_prompt)


# ---------------------------------------------------------------------------
# 0b: the per-scene resolver
# ---------------------------------------------------------------------------

def test_scene_refs_scene_pick_wins_per_slot():
    md = _md(scenes=3, refs={"character_asset_id": 7, "bg_upload_id": 70})
    md["scenes"][1].update(character_asset_id=9)
    md["scenes"][2].update(bg_asset_id=11)
    assert th._scene_ref_ids(md, 0) == {"character_asset_id": 7, "character_upload_id": None,
                                        "bg_asset_id": None, "bg_upload_id": 70}
    # Scene 1 overrides the character only: its asset wins as a PAIR (the
    # global upload id must not leak in and win at _slot_path, which prefers
    # uploads), and it still inherits the global background.
    md["refs"]["character_upload_id"] = 8
    assert th._scene_ref_ids(md, 1) == {"character_asset_id": 9, "character_upload_id": None,
                                        "bg_asset_id": None, "bg_upload_id": 70}
    assert th._scene_ref_ids(md, 2)["bg_asset_id"] == 11
    assert th._scene_ref_ids(md, 2)["bg_upload_id"] is None
    # Single-scene runs have no scenes list: the global refs.
    single = _md(refs={"character_upload_id": 5})
    assert th._scene_ref_ids(single, 0)["character_upload_id"] == 5
    assert th._scene_ref_ids(single, 3)["character_upload_id"] == 5


def test_scene_refs_briefs_come_from_the_scene_pick(env):
    md = _md(scenes=2, refs={"character_asset_id": 7})
    md["scenes"][1].update(character_asset_id=9)
    sr = asyncio.run(th._scene_refs(md, 1))
    assert sr["character_brief"] == "BRIEF[character asset=9 upload=None]"
    assert sr["bg_brief"] is None
    assert asyncio.run(th._scene_refs(md, 0))["character_brief"] == "BRIEF[character asset=7 upload=None]"


def test_person_ref_for_scene_is_the_pick_then_the_global_pick(env):
    char7 = env.ref_file("asset", 7)
    char9 = env.ref_file("asset", 9)
    md = _md(scenes=3, refs={"character_asset_id": 7})
    md["scenes"][1].update(character_asset_id=9)
    assert asyncio.run(th._person_ref_for_scene(md, 1)) == char9
    assert asyncio.run(th._person_ref_for_scene(md, 2)) == char7
    assert asyncio.run(th._person_ref_for_scene(_md(scenes=2), 1)) is None


def test_build_scene_prompt_reads_the_run_and_the_attached_flags():
    md = _md(aspect="16:9", lock=True)
    p = th._build_scene_prompt(md, 0, character_brief=None, bg_brief=None,
                               has_character_ref=False, has_bg_ref=False)
    _no_phantom(p)
    assert "16:9" in p and SUBJECT in p
    b = th._build_scene_prompt(md, 0, character_brief=None, bg_brief=None,
                               has_character_ref=True, has_bg_ref=False)
    assert b.startswith("FACE-AND-OUTFIT LOCK")


# ---------------------------------------------------------------------------
# generate_previews: every still names only what is attached
# ---------------------------------------------------------------------------

def test_generate_previews_without_a_character_never_claims_one(env):
    env.db.add_run(1, _md(scenes=3, lock=True))
    asyncio.run(th.generate_previews(1, look={"refs": {}, "lock_character": True}))
    calls = env.gen["calls"]
    assert len(calls) == 3
    # Scene 0 is made from text with the subject. Scenes 1-2 (E5) attach scene
    # 0's new still as the identity anchor, which really is attached.
    first, = [c for c in calls if c["sources"] is None]
    _no_phantom(first["prompt"])
    assert SUBJECT in first["prompt"]
    anchor = th._abs_from_rel(env.db.rows[1]["local_path"])
    for c in calls:
        if c is not first:
            assert c["sources"] == [anchor, anchor] and "IDENTITY ANCHOR" in c["prompt"]
    run = env.db.rows[1]
    assert re.fullmatch(r".*main-\d{8}T\d{12}-[0-9a-f]{4}\.png", run["local_path"])
    assert run["status"] == "ready" and run["width"] == 768 and run["height"] == 1344
    scenes = env.db.md(1)["scenes"]
    for i in (1, 2):
        row = env.db.rows[scenes[i]["keyframe_id"]]
        assert row["mode"] == th.MODE_SCENE and row["metadata"]["scene_index"] == i
        assert scenes[i]["as_is"] is False and scenes[i]["error"] is None
    assert env.db.md(1)["main_as_is"] is False and env.db.md(1)["main_error"] is None


def test_generate_previews_builds_each_scene_prompt_from_its_own_pick(env):
    char = env.ref_file("asset", 9)
    bg = env.ref_file("asset", 4, size=(1500, 1000))
    md = _md(scenes=3, lock=False)
    # A background too, so scene 1 composes (a character alone with New bg
    # OFF would be the as-is photo, no image call).
    md["scenes"][1].update(character_asset_id=9, bg_asset_id=4)
    env.db.add_run(2, md)
    asyncio.run(th.generate_previews(2, look={"refs": {}}))
    assert len(env.gen["calls"]) == 3
    by_brief = [c for c in env.gen["calls"] if "asset=9" in c["prompt"]]
    assert len(by_brief) == 1, "only scene 1 describes scene 1's character"
    assert by_brief[0]["sources"] == [bg], "lock off: no person image, the background is"
    anchor = th._abs_from_rel(env.db.rows[2]["local_path"])
    for c in env.gen["calls"]:
        assert char not in (c["sources"] or [])
        if c["sources"] == [anchor, anchor]:
            continue  # scene 2 (no pick): scene 0's still, attached as the identity anchor (E5)
        _no_phantom(c["prompt"])


def test_generate_previews_locked_scene_pick_is_attached_and_locked(env):
    char = env.ref_file("asset", 9)
    md = _md(scenes=2, lock=True)
    md["scenes"][1].update(character_asset_id=9, bg_asset_id=4)
    bg = env.ref_file("asset", 4, size=(1500, 1000))
    env.db.add_run(3, md)
    asyncio.run(th.generate_previews(3, look={"refs": {}}))
    s0, s1 = sorted(env.gen["calls"], key=lambda c: bool(c["sources"]))
    _no_phantom(s0["prompt"])
    assert s1["sources"] == [char, char, bg]
    assert s1["prompt"].startswith("FACE-AND-OUTFIT LOCK")
    assert "attached BACKGROUND reference" in s1["prompt"]


def test_a_missing_reference_file_is_not_named_in_the_prompt(env):
    env.ref_file("asset", 9)
    env.missing.add(("asset", 9))
    env.db.add_run(4, _md(refs={"character_asset_id": 9}, lock=True))
    asyncio.run(th.generate_previews(4, look={"refs": {"character_asset_id": 9}, "bg_mode": "new"}))
    # New bg ON, but the photo is gone: no keep-character canvas, no lock.
    call, = env.gen["calls"]
    _no_phantom(call["prompt"])
    assert call["sources"] is None


def test_generate_previews_as_is_and_keep_character_modes(env):
    env.ref_file("upload", 31, size=(720, 1280))
    md = _md(scenes=2, lock=True, refs={"character_upload_id": 31})
    env.db.add_run(5, md)
    look = {"refs": {"character_upload_id": 31},
            "scene_overrides": [None, {"bg_mode": "new"}]}
    asyncio.run(th.generate_previews(5, look=look))
    # Scene 0: the photo itself, no image call. Scene 1: keep_subject canvas.
    call, = env.gen["calls"]
    assert call["prompt"].startswith("EDIT THE ATTACHED IMAGE")
    assert [p.name for p in call["sources"]] == ["upload-31.png"]
    run = env.db.rows[5]
    assert run["prompt_used"] == th._USE_AS_IS_PROMPT
    assert env.db.md(5)["main_as_is"] is True
    assert env.db.md(5)["scenes"][1]["as_is"] is False


def test_an_extra_scene_falls_back_to_compose_but_scene_0_reports(env):
    env.ref_file("upload", 31, size=(1000, 1000))  # wrong shape: needs a reframe
    env.db.add_run(6, _md(scenes=2, lock=True, refs={"character_upload_id": 31}))
    env.gen["raise_on"] = lambda prompt, sources: "RESIZE ONLY" in prompt
    asyncio.run(th.generate_previews(6, look={"refs": {"character_upload_id": 31}}))
    md = env.db.md(6)
    assert md["main_error"], "scene 0's reframe failure is reported"
    assert env.db.rows[6]["status"] == "failed"
    assert md["scenes"][1]["keyframe_id"], "scene 1 fell back to a compose still"
    compose = [c for c in env.gen["calls"] if "RESIZE ONLY" not in c["prompt"]]
    assert len(compose) == 1 and compose[0]["prompt"].startswith("FACE-AND-OUTFIT LOCK")


# ---------------------------------------------------------------------------
# 0c: versioned scene-0 stills, lineage, atomic scene writes
# ---------------------------------------------------------------------------

def _seed_old_main(env, run_id: int) -> Path:
    old = env.tmp / "th" / str(run_id) / "main.png"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(_png_bytes((1248, 832)))
    row = env.db.rows[run_id]
    row.update(local_path=str(old), storage_bucket="producer-media",
               storage_path=f"yas/talking-head-v2/{run_id}/main.png", mime_type="image/png",
               width=1248, height=832, prompt_used="old prompt", feedback_text="far away shot")
    return old


def test_retry_main_keeps_the_old_still_and_never_claims_a_person(env):
    env.db.add_run(10, _md(lock=True))
    old = _seed_old_main(env, 10)
    # No feedback: Regenerate, a compose still from the script (E4).
    asyncio.run(th.retry_preview(run_id=10, which="main", feedback_text=""))
    call, = env.gen["calls"]
    _no_phantom(call["prompt"])
    assert SUBJECT in call["prompt"]
    run = env.db.rows[10]
    assert old.exists(), "the replaced still survives"
    assert run["local_path"] != str(old) and Path(run["local_path"]).exists()
    assert run["storage_path"].startswith("yas/talking-head-v2/10/main-")
    md = env.db.md(10)
    assert md["main_video_stale"] is True
    prev, = md["still_versions"]["0"]
    assert prev["local_path"] == str(old) and prev["storage_path"] == "yas/talking-head-v2/10/main.png"
    assert prev["feedback_text"] == "far away shot" and (prev["width"], prev["height"]) == (1248, 832)
    assert prev["prompt_used"] == "old prompt" and prev["as_is"] is False
    # Feedback: a Refine, an edit of the current still (E4); also a version.
    asyncio.run(th.retry_preview(run_id=10, which="main", feedback_text="warmer light"))
    assert len(env.gen["calls"]) == 1 and len(env.edit["calls"]) == 1
    assert env.db.rows[10]["feedback_text"] == "warmer light"
    assert len(env.db.md(10)["still_versions"]["0"]) == 2


def test_each_scene0_store_is_a_new_version(env):
    env.db.add_run(11, _md())
    paths = []
    for n in range(3):
        out = asyncio.run(th._store_scene_still(11, 0, _png_bytes(), "image/png", f"p{n}", "gemini_pro"))
        paths.append(out["local_path"])
        assert (out["previous"] is None) == (n == 0)
    assert len(set(paths)) == 3 and all(Path(p).exists() for p in paths)
    versions = env.db.md(11)["still_versions"]["0"]
    assert [v["local_path"] for v in versions] == paths[:2]
    assert env.db.rows[11]["local_path"] == paths[2]
    assert env.db.rows[11]["feedback_text"] is None, "no feedback leaves the column alone"


def test_scene0_versions_are_capped(env, monkeypatch):
    monkeypatch.setattr(th, "_STILL_VERSIONS_MAX", 2)
    env.db.add_run(12, _md())
    for n in range(4):
        asyncio.run(th._store_scene_still(12, 0, _png_bytes(), "image/png", f"p{n}", "gemini_pro"))
    assert [v["prompt_used"] for v in env.db.md(12)["still_versions"]["0"]] == ["p1", "p2"]


def test_scene0_versions_past_the_cap_have_their_files_deleted(env, monkeypatch):
    """A scene-0 version that drops off still_versions or still_undone is
    deleted then (bucket object and local file): delete_run only finds what
    the lists hold, so it would stay in the bucket for good."""
    monkeypatch.setattr(th, "_STILL_VERSIONS_MAX", 2)
    deleted: list[tuple] = []

    async def delete(*, storage_bucket, storage_path, local_path):
        deleted.append((storage_bucket, storage_path, local_path))

    monkeypatch.setattr(asset_storage, "delete", delete)
    env.db.add_run(15, _md())

    def store(n):
        return asyncio.run(th._store_scene_still(15, 0, _png_bytes(), "image/png", f"s{n}", "gemini_pro"))

    outs = [store(n) for n in range(4)]
    assert [d[1] for d in deleted] == [outs[0]["storage_path"]], "s0 fell off the list"
    assert deleted[0][0] == "producer-media" and deleted[0][2] == outs[0]["local_path"]
    # Undo pushes the current still to still_undone, capped the same way.
    for n in (4, 5):
        asyncio.run(th.undo_still(15, "main"))
        outs.append(store(n))
    asyncio.run(th.undo_still(15, "main"))
    md = env.db.md(15)
    assert [v["prompt_used"] for v in md["still_undone"]["0"]] == ["s4", "s5"]
    assert [d[1] for d in deleted] == [outs[0]["storage_path"], outs[3]["storage_path"]]
    # Every key ever uploaded is either deleted already or still listed for delete_run.
    listed = {v["storage_path"] for k in ("still_versions", "still_undone") for v in md[k]["0"]}
    listed.add(env.db.rows[15]["storage_path"])
    assert listed | {d[1] for d in deleted} == set(env.uploads)
    assert not listed & {d[1] for d in deleted}


def test_a_dropped_version_still_in_use_is_kept(monkeypatch):
    deleted: list = []

    async def delete(**kw):
        deleted.append(kw["storage_path"])

    monkeypatch.setattr(asset_storage, "delete", delete)
    a = {"storage_bucket": "b", "storage_path": "k/a.png", "local_path": "l/a.png"}
    b = {"storage_bucket": "b", "storage_path": "k/b.png", "local_path": "l/b.png"}
    asyncio.run(th._delete_dropped_stills(1, [a, b, None, {}], [dict(b)]))
    assert deleted == ["k/a.png"]


def test_retry_kind_only_reaches_the_column_when_the_check_allows_it(env):
    env.db.add_run(13, _md(scenes=2))
    asyncio.run(th._store_scene_still(13, 0, _png_bytes(), "image/png", "p", "gemini_pro",
                                      retry_kind="refine"))
    assert env.db.rows[13]["retry_kind"] is None
    assert env.db.md(13)["main_retry_kind"] == "refine"
    asyncio.run(th._store_scene_still(13, 0, _png_bytes(), "image/png", "p", "gemini_pro",
                                      retry_kind="tweak"))
    assert env.db.rows[13]["retry_kind"] == "tweak"
    assert env.db.md(13)["still_versions"]["0"][-1]["retry_kind"] == "refine"
    out = asyncio.run(th._store_scene_still(13, 1, _png_bytes(), "image/png", "p", "gemini_pro",
                                            retry_kind="refine", feedback="zoom out",
                                            extra_md={"framing_used": "close"}))
    row = env.db.rows[out["id"]]
    assert row["retry_kind"] is None and row["metadata"]["retry_kind"] == "refine"
    assert row["feedback_text"] == "zoom out" and row["metadata"]["framing_used"] == "close"
    assert row["metadata"]["category"] == "talking_head_v2_scene"


def test_scene0_store_patches_scene0_when_the_run_has_scenes(env):
    env.db.add_run(14, _md(scenes=2))
    asyncio.run(th._store_scene_still(14, 0, _png_bytes(), "image/png", "p", "gemini_pro",
                                      scene_md={"video_stale": True}, extra_md={"main_error": None}))
    md = env.db.md(14)
    assert md["scenes"][0]["video_stale"] is True and md["scenes"][0]["dialogue"] == "line 0"
    assert md["scenes"][1]["video_stale"] is False
    assert md["main_error"] is None


def _seed_scene_row(env, run_id: int, i: int, row_id: int, *, prompt="scene prompt") -> Path:
    """A MODE_SCENE row `row_id` with a real still on disk, as scene i's."""
    p = env.tmp / "th" / str(run_id) / f"scene-{i}-{row_id}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_png_bytes((756, 1344), (90, 60, 30)))
    env.db.rows[row_id] = {
        "id": row_id, "mode": th.MODE_SCENE, "topic": "t", "status": "ready",
        "image_model": "gemini_pro", "prompt_used": prompt, "local_path": str(p),
        "storage_bucket": "producer-media", "storage_path": f"yas/talking-head-v2/{run_id}/{p.name}",
        "mime_type": "image/png", "width": 756, "height": 1344, "feedback_text": None,
        "retry_kind": None, "parent_generation_id": None, "error": None, "updated_at": 0,
        "metadata": {"category": "talking_head_v2_scene", "parent_talking_head_id": run_id,
                     "scene_index": i},
    }
    return p


def test_retry_scene_records_lineage_and_repoints_atomically(env):
    md = _md(scenes=3, lock=True)
    md["scenes"][1].update(keyframe_id=555, animation_id=77, error="old failure")
    env.db.add_run(20, md)
    _seed_scene_row(env, 20, 1, 555)
    # Feedback: a Refine of scene 1's still (E4), an edit with no prompt that
    # could claim a person.
    asyncio.run(th.retry_preview(run_id=20, which="scene_1", feedback_text="smile less"))
    edit, = env.edit["calls"]
    _no_phantom("\n".join(str(v) for k, v in edit["parts"] if k == "text"))
    assert env.gen["calls"] == []
    sc = env.db.md(20)["scenes"]
    new_id = sc[1]["keyframe_id"]
    assert new_id != 555 and sc[1]["previous_keyframe_ids"] == [555]
    assert sc[1]["video_stale"] is True and sc[1]["error"] is None
    assert sc[1]["animation_id"] == 77 and sc[1]["dialogue"] == "line 1"
    row = env.db.rows[new_id]
    assert row["feedback_text"] == "smile less" and row["parent_generation_id"] == 555
    assert sc[0] == _scene(0) and sc[2] == _scene(2), "other scenes untouched"
    # The scene write is one jsonb_set on that element, never the whole array.
    writes = [s for s in env.db.log if s.startswith("UPDATE yas_generations SET metadata")]
    assert writes and all("jsonb_set(metadata, %s::text[]" in s for s in writes)


def test_retry_scene_brief_and_image_come_from_the_scene_pick(env):
    env.ref_file("asset", 7)
    char9 = env.ref_file("asset", 9)
    bg = env.ref_file("asset", 4, size=(1500, 1000))
    md = _md(scenes=2, lock=True, refs={"character_asset_id": 7})
    # A background too, so the scene composes (a lone character with New bg
    # OFF is the as-is photo).
    md["scenes"][1].update(character_asset_id=9, bg_asset_id=4)
    env.db.add_run(21, md)
    # No feedback: Regenerate composes from the scene's own picks (E4).
    asyncio.run(th.retry_preview(run_id=21, which="scene_1", feedback_text=""))
    call, = env.gen["calls"]
    assert call["sources"] == [char9, char9, bg]
    # Lock off: the brief text is in the prompt, and it is scene 1's.
    env.gen["calls"].clear()
    env.db.rows[21]["metadata"]["lock_character"] = False
    asyncio.run(th.retry_preview(run_id=21, which="scene_1", feedback_text=""))
    call, = env.gen["calls"]
    assert "asset=9" in call["prompt"] and "asset=7" not in call["prompt"]


def test_retry_scene0_keeps_the_scene0_marker(env):
    env.db.add_run(22, _md(scenes=2))
    _seed_old_main(env, 22)
    asyncio.run(th.retry_preview(run_id=22, which="scene_0", feedback_text="brighter"))
    assert len(env.edit["calls"]) == 1, "feedback is a Refine (an image edit)"
    md = env.db.md(22)
    assert md["scenes"][0]["video_stale"] is True
    assert len(md["still_versions"]["0"]) == 1
    assert env.db.rows[22]["feedback_text"] == "brighter"


def test_patch_scene_merges_one_scene(env):
    env.db.add_run(30, _md(scenes=3))
    out = asyncio.run(th._patch_scene(30, 2, {"error": "x", "keyframe_generating": False}))
    assert out["error"] == "x" and out["dialogue"] == "line 2"
    sc = env.db.md(30)["scenes"]
    assert sc[2]["error"] == "x" and sc[0] == _scene(0) and sc[1] == _scene(1)
    seen = {}

    def fn(scene):
        seen.update(scene)
        return {"n": (scene.get("n") or 0) + 1}

    asyncio.run(th._patch_scene(30, 1, fn))
    asyncio.run(th._patch_scene(30, 1, fn))
    assert env.db.md(30)["scenes"][1]["n"] == 2 and seen["dialogue"] == "line 1"


def test_patch_scene_out_of_range_writes_nothing(env):
    env.db.add_run(31, _md(scenes=2))
    env.db.add_run(32, _md())
    before = len(env.db.log)
    assert asyncio.run(th._patch_scene(31, 5, {"error": "x"})) is None
    assert asyncio.run(th._patch_scene(32, 0, {"error": "x"})) is None  # single-scene: no list
    assert asyncio.run(th._patch_scene(31, -1, {"error": "x"})) is None
    assert not any(s.startswith("UPDATE") for s in env.db.log[before:])


def test_patch_scene_concurrent_writers_on_different_scenes_both_land(env):
    env.db.add_run(33, _md(scenes=3))

    async def both():
        await asyncio.gather(th._patch_scene(33, 1, {"a": 1}), th._patch_scene(33, 2, {"b": 2}))

    asyncio.run(both())
    sc = env.db.md(33)["scenes"]
    assert sc[1]["a"] == 1 and sc[2]["b"] == 2


def test_patch_scene_sql_locks_and_sets_one_element():
    src = inspect.getsource(th._patch_scene)
    assert "FOR UPDATE" in src
    assert "jsonb_set(metadata, %s::text[], %s::jsonb, false)" in src
    assert '["scenes", str(i)]' in src


# ---------------------------------------------------------------------------
# generate_previews_for_scenes and the edit regen
# ---------------------------------------------------------------------------

def test_generate_previews_for_scenes_never_claims_a_person(env):
    md = _md(scenes=3, lock=True)
    md["scenes"][2].update(animation_id=88)
    env.db.add_run(40, md, local_path="x/main.png")
    asyncio.run(th.generate_previews_for_scenes(40, [1, 2]))
    assert len(env.gen["calls"]) == 2
    for c in env.gen["calls"]:
        _no_phantom(c["prompt"])
    sc = env.db.md(40)["scenes"]
    assert sc[1]["keyframe_id"] and sc[2]["keyframe_id"]
    assert sc[1]["video_stale"] is False, "no clip yet: nothing to mark stale"
    assert sc[2]["video_stale"] is True, "scene 2 had a clip"


def test_generate_previews_for_scenes_records_a_failure_on_that_scene(env):
    env.db.add_run(41, _md(scenes=3))
    env.gen["raise_on"] = lambda prompt, sources: True
    asyncio.run(th.generate_previews_for_scenes(41, [2]))
    sc = env.db.md(41)["scenes"]
    assert sc[2]["error"] == "model said no" and sc[2]["keyframe_id"] is None
    assert sc[1] == _scene(1)


def test_regen_with_a_missing_character_file_is_not_a_face_lock(env):
    env.ref_file("asset", 9)
    env.missing.add(("asset", 9))
    md = _md(scenes=2, lock=True, refs={"character_asset_id": 9})
    md["scenes"][1].update(keyframe_id=600)
    env.db.add_run(50, md)
    asyncio.run(th._regen_scene_keyframe(50, scene_index=1, keep_subject=False,
                                         has_char_ref=True, has_bg_ref=False))
    call, = env.gen["calls"]
    _no_phantom(call["prompt"])
    assert call["sources"] is None
    sc = env.db.md(50)["scenes"][1]
    assert sc["previous_keyframe_ids"] == [600] and sc["error"] is None
    assert sc["video_stale"] is False, "no clip yet"


def test_regen_keep_subject_with_a_missing_bg_file_names_no_background(env):
    env.db.add_run(51, _md(lock=True, refs={"bg_asset_id": 4}))
    env.missing.add(("asset", 4))
    old = _seed_old_main(env, 51)
    asyncio.run(th._regen_scene_keyframe(51, scene_index=0, keep_subject=True,
                                         has_char_ref=False, has_bg_ref=True))
    call, = env.gen["calls"]
    assert call["sources"] == [old]
    assert call["prompt"].startswith("EDIT THE ATTACHED IMAGE")
    assert "attached BACKGROUND reference" not in call["prompt"]
    md = env.db.md(51)
    assert md["main_video_stale"] is True and md["main_error"] is None
    assert md["still_versions"]["0"][0]["local_path"] == str(old) and old.exists()


def test_regen_compose_with_both_refs_attached(env):
    char = env.ref_file("asset", 9)
    bg = env.ref_file("asset", 4, size=(1500, 1000))
    env.db.add_run(52, _md(lock=True, refs={"character_asset_id": 9, "bg_asset_id": 4}))
    asyncio.run(th._regen_scene_keyframe(52, scene_index=0, keep_subject=False,
                                         has_char_ref=True, has_bg_ref=True))
    call, = env.gen["calls"]
    assert call["sources"] == [char, char, bg]
    assert call["prompt"].startswith("FACE-AND-OUTFIT LOCK")


def test_keyframe_regen_error_and_markers_are_per_scene(env):
    md = _md(scenes=3, main_keyframe_generating=True)
    for s in md["scenes"]:
        s["keyframe_generating"] = True
    env.db.add_run(53, md)
    env.gen["raise_on"] = lambda prompt, sources: True
    asyncio.run(th._run_keyframe_regen(53, [
        {"scene_index": 2, "keep_subject": False, "has_char_ref": False, "has_bg_ref": False},
        {"scene_index": 0, "keep_subject": False, "has_char_ref": False, "has_bg_ref": False},
    ]))
    md = env.db.md(53)
    assert md["scenes"][2]["error"] == "model said no"
    assert md["main_error"] == "model said no"
    assert md["scenes"][0]["keyframe_generating"] is False
    assert md["scenes"][2]["keyframe_generating"] is False
    assert md["scenes"][1]["keyframe_generating"] is True, "not in this regen"
    assert md["main_keyframe_generating"] is False


# ---------------------------------------------------------------------------
# 0d: no keyframes before the run has stills
# ---------------------------------------------------------------------------

@pytest.fixture
def scene_count_env(monkeypatch):
    rows: dict[int, dict] = {}
    made: list[tuple[int, list[int]]] = []

    async def fetch_run_row(run_id):
        return copy.deepcopy(rows[run_id])

    async def patch_metadata(run_id, patch):
        rows[run_id]["metadata"].update(patch)

    async def apply_scene_count(run_id, n, *, restore):
        return list(range(int(rows[run_id]["metadata"].get("scene_count") or 1), n))

    async def gen_for_scenes(run_id, indices):
        made.append((run_id, list(indices)))
        return {}

    async def fetch_run(run_id, **_):
        return {"id": run_id}

    monkeypatch.setattr(th, "_fetch_run_row", fetch_run_row)
    monkeypatch.setattr(th, "_patch_metadata", patch_metadata)
    monkeypatch.setattr(th, "_apply_scene_count", apply_scene_count)
    monkeypatch.setattr(th, "generate_previews_for_scenes", gen_for_scenes)
    monkeypatch.setattr(th, "fetch_run", fetch_run)
    return rows, made


def test_edit_run_scene_count_before_stills_makes_no_keyframe(scene_count_env):
    rows, made = scene_count_env
    rows[827] = {"id": 827, "topic": "t", "local_path": None, "storage_path": None,
                 "metadata": {"scene_count": 1}}
    asyncio.run(th.edit_run(run_id=827, scene_count=2))
    assert made == [], "the create-flow trap: no scene still before Generate stills"


def test_edit_run_scene_count_after_stills_makes_the_new_keyframes(scene_count_env):
    rows, made = scene_count_env
    rows[828] = {"id": 828, "topic": "t", "local_path": "x/main.png", "storage_path": None,
                 "metadata": {"scene_count": 1}}
    asyncio.run(th.edit_run(run_id=828, scene_count=3))
    assert made == [(828, [1, 2])]


def test_edit_run_counts_a_scene_row_as_a_still(scene_count_env):
    rows, made = scene_count_env
    rows[829] = {"id": 829, "topic": "t", "local_path": None, "storage_path": None,
                 "metadata": {"scene_count": 2, "scenes": [_scene(0), _scene(1, keyframe_id=5)]}}
    asyncio.run(th.edit_run(run_id=829, scene_count=3))
    assert made == [(829, [2])]


def test_restore_scene_before_stills_makes_no_keyframe(scene_count_env):
    rows, made = scene_count_env
    md = {"scene_count": 1, "archived_scenes": [_scene(1, archived_index=1)]}
    rows[830] = {"id": 830, "topic": "t", "local_path": None, "storage_path": None, "metadata": md}
    asyncio.run(th.restore_scene(830, archived_index=1))
    assert made == []
    rows[831] = {"id": 831, "topic": "t", "local_path": "x/main.png", "storage_path": None,
                 "metadata": copy.deepcopy(md)}
    asyncio.run(th.restore_scene(831, archived_index=1))
    assert made == [(831, [1])]


# ---------------------------------------------------------------------------
# delete_run removes every scene-0 version
# ---------------------------------------------------------------------------

def test_delete_run_removes_the_earlier_scene0_versions(monkeypatch, tmp_path):
    monkeypatch.setattr(th, "TH_ROOT", tmp_path / "th")

    async def fetch_run_row(run_id):
        return {"id": run_id, "storage_bucket": "b", "storage_path": "k/main-2.png",
                "local_path": "l/main-2.png",
                "metadata": {"still_versions": {"0": [
                    {"storage_bucket": "b", "storage_path": "k/main.png", "local_path": "l/main.png"},
                    {"storage_bucket": "b", "storage_path": "k/main-1.png", "local_path": "l/main-1.png"},
                ]}}}

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
    assert asyncio.run(th.delete_run(4)) == {"deleted": 1, "id": 4}
    assert deleted == ["k/main-2.png", "k/main.png", "k/main-1.png"]


# ---------------------------------------------------------------------------
# 0e: card_animation's edit renderer takes an opt-in aspect
# ---------------------------------------------------------------------------

def test_edit_render_without_an_aspect_sends_the_same_call(monkeypatch):
    seen: list[dict] = []

    async def render(parts, **kwargs):
        seen.append(kwargs)
        return b"IMG"

    monkeypatch.setattr(ca, "render_card_image", render)
    out = asyncio.run(ca._render_edit_escalating([("text", "go")], system_instruction="S",
                                                 timeout=1.0, what="refine"))
    assert out == b"IMG" and seen == [{"system_instruction": "S", "timeout": 1.0}]


def test_edit_render_forces_the_aspect_on_both_attempts(monkeypatch):
    from app.services.vertex_client import GeminiNoImageError

    seen: list[dict] = []

    async def render(parts, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            raise GeminiNoImageError("text reply", blocked=False, finish_reason="STOP")
        return b"IMG"

    monkeypatch.setattr(ca, "render_card_image", render)
    out = asyncio.run(ca._render_edit_escalating([("text", "go")], system_instruction="S",
                                                 timeout=1.0, what="refine", aspect_ratio="9:16"))
    assert out == b"IMG" and [k.get("aspect_ratio") for k in seen] == ["9:16", "9:16"]


def test_refine_image_passes_the_aspect_through(monkeypatch, tmp_path):
    seen: list[dict] = []

    async def escalate(parts, **kwargs):
        seen.append(kwargs)
        return b"IMG"

    monkeypatch.setattr(ca, "_render_edit_escalating", escalate)
    src = tmp_path / "still.png"
    asyncio.run(ca.render_refine_image(src, change="warmer", timeout=1.0, aspect_ratio="16:9"))
    asyncio.run(ca.render_refine_image(src, change="warmer", timeout=1.0))
    assert [k["aspect_ratio"] for k in seen] == ["16:9", None]


# ---------------------------------------------------------------------------
# 0f: the Gemini script fallback logs the model it calls
# ---------------------------------------------------------------------------

def test_gemini_script_fallback_logs_the_fast_model():
    # The script call (OpenAI, else Gemini) is shared by generate_script and
    # the re-script path (E5).
    src = inspect.getsource(th._script_llm)
    m = re.search(r'script generated via Gemini fallback \(%s\)", (config\.\w+)', src)
    assert m and m.group(1) == "config.vertex_gemini_fast_model"
    call = src[src.index("gemini_multimodal("): src.index("timeout=SCRIPT_LLM_TIMEOUT_SEC", src.index("gemini_multimodal("))]
    assert "model=config.vertex_gemini_fast_model" in call
