"""The icon director, checked for free: for every scene the studio prepared on
this machine, what was said, what the AI kept or refused and why, and what
showed — from the files the studio already has. No model is ever called.

  python -m app.services.rebrand_harness.director_dryrun
      [--run 815] [--target scene_0]     one run, or one scene (default: every local scene)
      [--line "…"] [--topic "…"] [--other "…" …]
                                         the scene's words as the AI got them (default: read
                                         from the database, read-only; --offline never reads it)
      [--no-listen]                      never run local Whisper / MODNet for a scene without them
      [--want FILE]                      the icons you wanted, {"815/scene_0": ["piggy-bank", …]},
                                         for precision and recall
      [--out DIR] [--html FILE]          default REBRAND_ROOT/_dryrun (report.html + report.json)

GUARDED. While it works (blocked()), every paid call of vertex_client — Gemini
text, search, multimodal, embeddings and images, and Veo — is replaced by one
that raises PaidCallBlocked and is noted in ATTEMPTS; main() exits EXIT_PAID
when one was attempted, even if the code that tried swallowed the error. It
never writes into the studio's folders: the words or speaker track a scene
does not have yet are made (local Whisper and MODNet, free) in its own cache
under --out.

Per scene (scene_report):
  1. said       the words (icons/words.json), director.explain() — every
                keyword matched, and why each one dropped was — and the lists
                said together;
  2. asked      the line, the topic and the other scenes' lines; the keyword
                candidates; the prompt the AI gets (its size and an estimated
                cost) and its key: whether the stored answer still answers it
                (no call), or the studio would ask again (one paid call);
  3. answered   the stored answer's ideas: kept, refused by the AI, or kept by
                the AI and refused by a rule here (director.refused_by), and
                why; for a library miss, the library's best match today;
  4. replayed   the same answer judged by today's rules (director.replay):
                from its `raw`, or — for an answer kept before `raw` was —
                from an answer rebuilt from its stored ideas (approximate,
                and said so);
  5. shown      the icon plan the studio's starting draft makes from it
                (elements.icon.plan: the scene's default recipe + automatic
                icons, placed without the pattern's side), and the plans the
                studio itself made for the scene (icons/plans, newest first);
  6. without    the keyword suggestions the studio falls back on
     the AI     (director.fallback).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import html
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .. import vertex_client
from . import director as dmod
from . import recipe as rc
from . import speaker_track
from . import words as wordsmod
from .elements import icon as icon_el

log = logging.getLogger("archivist.rebrand.harness.director_dryrun")

# vertex_client's calls that cost money: all refused while the dry run works.
PAID_CALLS = ("gemini_text", "gemini_search", "gemini_multimodal", "gemini_embed", "gemini_image",
              "gemini_image_multipart", "veo_generate")
ROOM_TOKENS = 516            # the 720 x 1280 room image: two 768 px tiles of 258 tokens
ANSWER_TOKENS = 800          # an answer's length when none was kept (815's ideas are ~2.4k characters)
PLANS_SHOWN = 5              # the studio's own plans listed per scene, newest first
THUMB_W = 300                # the pictures in the report, px wide
EXIT_PAID = 3                # main()'s exit code when a paid call was attempted
DB_TIMEOUT_SEC = 20

# Every paid call attempted while blocked (each one refused), in order.
ATTEMPTS: list[str] = []


class PaidCallBlocked(RuntimeError):
    """A model call the dry run refused."""


def _refuser(name: str):
    async def refuse(*_a: Any, **_k: Any) -> Any:
        ATTEMPTS.append(name)
        raise PaidCallBlocked(f"{name}: the director dry run makes no model call")
    return refuse


@contextlib.contextmanager
def blocked() -> Iterator[None]:
    """Every paid call of vertex_client refused while inside (PaidCallBlocked,
    noted in ATTEMPTS); what was there before is put back after."""
    was = {n: getattr(vertex_client, n) for n in PAID_CALLS if hasattr(vertex_client, n)}
    for n in was:
        setattr(vertex_client, n, _refuser(n))
    try:
        yield
    finally:
        for n, fn in was.items():
            setattr(vertex_client, n, fn)


# ---------------------------------------------------------------------------
# The scenes on this machine, and their words
# ---------------------------------------------------------------------------

@dataclass
class LocalScene:
    """One scene the studio prepared here: <root>/<run>/<target>_<source hash>
    [_t<cut>]. Read only."""
    run_id: int
    target: str
    work: Path

    @property
    def name(self) -> str:
        return f"{self.run_id}/{self.target}"

    @property
    def icons(self) -> Path:
        return self.work / "icons"

    @property
    def clip(self) -> Optional[Path]:
        p = self.work / "base" / f"scene{self.target[6:]}.mp4"
        return p if p.is_file() else None


_FOLDER = re.compile(r"^(scene_\d+)_[0-9a-z]+(?:_t\d+)?$")


def studio_root() -> Path:
    from . import studio

    return studio.STUDIO_ROOT


def local_scenes(root: Optional[Path] = None, *, run_id: Optional[int] = None,
                 target: Optional[str] = None) -> list[LocalScene]:
    """Every scene prepared under `root` (the studio's folder), one per run
    and target: the folder with the director's answer, else with the words,
    else the trimmed one (studio.scene_base "_t<frame>"), else the newest."""
    root = Path(root) if root is not None else studio_root()
    best: dict[tuple[int, str], tuple[tuple, LocalScene]] = {}
    if not root.is_dir():
        return []
    for run_dir in sorted(root.iterdir()):
        if not (run_dir.is_dir() and run_dir.name.isdigit()):
            continue
        rid = int(run_dir.name)
        if run_id is not None and rid != run_id:
            continue
        for work in sorted(run_dir.iterdir()):
            m = _FOLDER.match(work.name)
            if not (work.is_dir() and m) or (target is not None and m.group(1) != target):
                continue
            icons = work / "icons"
            rank = ((icons / "director.json").is_file(), (icons / "words.json").is_file(),
                    bool(re.search(r"_t\d+$", work.name)), work.stat().st_mtime)
            key = (rid, m.group(1))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, LocalScene(rid, m.group(1), work))
    return [best[k][1] for k in sorted(best, key=lambda k: (k[0], int(k[1][6:])))]


@dataclass
class Lines:
    """What the director is given beside the words (studio._scene_lines)."""
    script: str = ""
    topic: str = ""
    others: list[str] = field(default_factory=list)
    source: str = "unknown"          # "database" | "given" | "unknown"
    note: str = ""

    @property
    def known(self) -> bool:
        return self.source != "unknown"

    def public(self) -> dict:
        return {"script": self.script, "topic": self.topic, "others": list(self.others),
                "source": self.source, "note": self.note}


def lines_of(run: dict, target: str) -> Lines:
    """A run row's words for one scene, as the studio reads them
    (rebrand_pipeline._run_ctx, then studio._scene_lines)."""
    from .. import rebrand_pipeline as pipeline

    md = run.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            md = {}
    idx = int(target[6:])
    note = ""
    if int(md.get("scene_count") or 1) > 1 and md.get("scenes"):
        # The scenes a run includes: the ones with a video (a neighbour whose
        # video is missing on disk and in the bucket is left out by the studio,
        # not here).
        idxs = [i for i, sm in enumerate(md["scenes"])
                if i == idx or (not sm.get("excluded") and sm.get("animation_id"))]
        note = "the other scenes are the ones with a video"
    else:
        idxs = [0]
    segs = [pipeline.Segment(kind="scene", index=i, source=Path(), is_still=False) for i in idxs]
    rcx = pipeline._run_ctx(run, md, segs, "9:16")
    lines = {int(k): str(v or "") for k, v in (rcx.get("scene_dialogue") or {}).items()}
    others = [lines[k] for k in sorted(lines) if k != idx and lines[k].strip()]
    return Lines(script=lines.get(idx, ""), topic=str(rcx.get("topic") or ""), others=others,
                 source="database", note=note)


async def read_run(run_id: int) -> dict:
    """The run's row (its topic and metadata): one SELECT in a READ ONLY
    transaction, on a connection of its own (never the app's pool)."""
    import psycopg
    from psycopg.rows import dict_row

    from ...db import _dsn

    async def go() -> dict:
        async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=False, prepare_threshold=None,
                                                         row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute("SELECT id, topic, metadata FROM yas_generations WHERE id = %s", (run_id,))
                row = await cur.fetchone()
            await conn.rollback()
        if row is None:
            raise LookupError(f"run {run_id} is not in the database")
        return dict(row)

    return await asyncio.wait_for(go(), DB_TIMEOUT_SEC)


def _read_json(path: Path) -> Optional[dict]:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _take(scene: LocalScene) -> int:
    try:
        return int((_read_json(scene.icons / "take.json") or {})["take"])
    except (KeyError, TypeError, ValueError):
        return 0


def scene_words(scene: LocalScene, lines: Lines, cache: Path, *, listen: bool = True
                ) -> tuple[Optional[dict], str]:
    """The scene's Words and where they came from: the studio's own
    icons/words.json (read, never rewritten); else heard here with local
    Whisper into `cache` (free); else None."""
    saved = _read_json(scene.icons / "words.json")
    if saved is not None:
        return saved, "the studio's"
    if scene.clip is None:
        return None, "no words: the scene's clip is not on disk"
    if not lines.script:
        return None, "no words yet, and the scene's line is unknown (give --line, or let it read the database)"
    if not listen:
        return None, "no words yet (--no-listen)"
    return wordsmod.scene_words(scene.clip, lines.script, cache), "heard here (local Whisper)"


def scene_track(scene: LocalScene, cache: Path, *, listen: bool = True
                ) -> tuple[Optional[speaker_track.Track], str]:
    """The speaker track: the studio's (icons/track.json), else made here
    with local MODNet into `cache` (free), else None."""
    track = speaker_track.load(scene.icons)
    if track is not None:
        return track, "the studio's"
    if scene.clip is None or not listen:
        return None, "none"
    try:
        return speaker_track.build(scene.clip, cache), "made here (local MODNet)"
    except Exception as e:  # noqa: BLE001 — no matte here: the spots are a guess
        return None, f"could not be made here ({str(e)[:120]})"


# ---------------------------------------------------------------------------
# The stored answer: who refused what, and the answer judged again
# ---------------------------------------------------------------------------

_NO_ICON_FOR = re.compile(r"^no icon for '(.*)'$", re.DOTALL)
_NO_ICON = re.compile(r"^no icon '(.*)'$", re.DOTALL)


def rebuilt_answer(body: Optional[dict]) -> Optional[dict]:
    """An answer rebuilt from a director.json's stored ideas, for one kept
    before `raw` was: every idea the AI judged, with its own verdict (a rule's
    refusal was the AI's keep), its words, label, purpose and priority, the
    "other:<noun>" a library miss named, and the room it read. APPROXIMATE:
    what a rule corrected (an index slip, a brand logo's id) is not known any
    more; a quote refused as not said is rebuilt unsaid, so it is refused
    again."""
    if not body or body.get("source") != "ai":
        return None
    ideas = []
    for i in body.get("ideas") or []:
        by = dmod.refused_by(i)
        if i.get("source") != "ai" or by == "silent":
            continue
        reason = str(i.get("reason") or "")
        icon = str(i.get("icon") or "")
        if by == "check":
            m = _NO_ICON_FOR.match(reason)
            n = _NO_ICON.match(reason)
            icon = f"other:{m.group(1).rstrip('…')}" if m else n.group(1).rstrip("…") if n else icon
        ideas.append({
            "cand_id": i.get("cand_id") or "new", "concept": i.get("concept") or "",
            "quote": "" if reason == dmod.WHY_NOT_THERE else i.get("quote") or "",
            "w": i.get("w"), "w_end": i.get("w_end"),
            "mentions": [{"w": m["w"], "quote": m.get("quote") or ""} for m in i.get("mentions") or []],
            "icon": icon, "label": i.get("label") or "", "purpose": i.get("purpose") or "",
            "priority": i.get("priority"), "verdict": "refuse" if by == "ai" else "keep",
            "refuse_reason": reason if by == "ai" else "", "list_span": bool(i.get("list_span")),
        })
    room = body.get("room") or {}
    return {"ideas": ideas,
            "room": {"spots": [{"n": s.get("n"), "verdict": s.get("verdict"), "why": s.get("why") or ""}
                               for s in room.get("spots") or []],
                     "home": room.get("home_n") or 0, "home_why": room.get("home_why") or ""}}


def _plain(obj: Any) -> Any:
    """As stored (a JSON round trip), so a replay compares like with like."""
    return json.loads(json.dumps(obj, default=str))


def _idea_key(i: dict) -> str:
    return i.get("cand_id") or f"new:{i.get('w')}:{i.get('concept')}"


_DIFF_FIELDS = ("verdict", "reason", "auto", "icon", "priority", "w", "w_end", "label")


def diff_ideas(was: list[dict], now: list[dict]) -> list[dict]:
    """What a replay changed, idea by idea: [{idea, changed: {field: [was,
    now]}}], or an idea only one side has ({idea, was|now})."""
    a = {_idea_key(i): i for i in was}
    b = {_idea_key(i): i for i in now}
    out = []
    for k in [*a, *(k for k in b if k not in a)]:
        x, y = a.get(k), b.get(k)
        name = (x or y).get("label") or (x or y).get("concept") or k
        if x is None or y is None:
            out.append({"idea": name, ("now" if x is None else "was"): (x or y).get("verdict")})
            continue
        changed = {f: [x.get(f), y.get(f)] for f in _DIFF_FIELDS if x.get(f) != y.get(f)}
        if changed:
            out.append({"idea": name, "changed": changed})
    return out


# ---------------------------------------------------------------------------
# What shows: the studio's starting draft, planned
# ---------------------------------------------------------------------------

def starting_draft(run_id: int, target: str) -> rc.Recipe:
    """The draft a new scene starts from in the studio: its default recipe
    (the router's _default_recipe) plus automatic icons drawn as circles
    (rebrandIcons.withStartingIcons / startingIcons)."""
    try:
        from ...routers.rebrand_harness import _default_recipe

        base = rc.normalize(_default_recipe(run_id, target))
    except Exception:  # noqa: BLE001 — the router cannot load here: the 42 default
        from . import studio

        base = rc.preset_42_default(target).model_copy(update={"seed": studio.default_seed(run_id, target)})
    if rc.icon_element(base) is None:
        icons = rc.Element(id="icons", kind="icon", placement=rc.Placement(anchor="auto"),
                           animation=rc.Animation(entrance="auto"), props={"style": "circle"})
        base = rc.normalize(base.model_copy(update={"preset": None, "elements": [*base.elements, icons]}))
    return base


def plan_of(draft: rc.Recipe, words: dict, director: dict,
            track: Optional[speaker_track.Track]) -> icon_el.IconPlan:
    """The icon plan of `draft` on the scene (elements.icon.plan), as the
    studio makes it — without the pattern's side (no pattern preview here)."""
    el = rc.icon_element(draft)
    ctx = icon_el.IconCtx(
        words=words, director=director, track=track,
        width=track.width if track is not None else 720, height=track.height if track is not None else 1280,
        duration=float(words.get("duration") or 0.0), fps=icon_el.FPS, chunks=[],
        enters=any(e.animation.entrance != "none" for e in rc.pattern_elements(draft)),
        seed=draft.seed or "", face=draft.guards.face, pattern=None, backgrounds=None, base=None,
        notes=[] if track is not None else [icon_el.W_NO_TRACK])
    return icon_el.plan(el, ctx)


def _shown(plan: icon_el.IconPlan) -> dict:
    pub = plan.public()
    return {
        "items": [{"icon": it["glyph"]["id"], "label": it["label"], "source": it["source"],
                   "settle": it["settle"], "from": it["from"], "to": it["to"], "spot": it.get("spot"),
                   "word": it.get("word"), "box": it.get("box")} for it in pub["items"]],
        "ideas": [{"icon": i["glyph"]["id"], "label": i["label"], "state": i["state"], "reason": i["reason"],
                   "auto": i["auto"], "word": i["word"], "at": i["at"], "why": i.get("why")} for i in pub["ideas"]],
        "warnings": pub["warnings"], "home": pub["home"],
    }


def studio_plans(scene: LocalScene) -> tuple[list[dict], int]:
    """The plans the studio saved for the scene (icons/plans), newest first:
    ([{plan_key, when, items, added, removed}] up to PLANS_SHOWN, how many)."""
    files = sorted((scene.icons / "plans").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if (scene.icons / "plans").is_dir() else []
    out = []
    for p in files[:PLANS_SHOWN]:
        plan = (_read_json(p) or {}).get("plan") or {}
        out.append({
            "plan_key": plan.get("plan_key") or p.stem,
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
            "items": [{"icon": it["glyph"]["id"], "label": it.get("label"), "source": it.get("source"),
                       "settle": it.get("settle"), "spot": it.get("spot"), "word": it.get("word")}
                      for it in plan.get("items") or []],
            "added": [{"icon": i["glyph"]["id"], "label": i.get("label")} for i in plan.get("ideas") or []
                      if i.get("added")],
            "removed": [{"icon": i["glyph"]["id"], "label": i.get("label")} for i in plan.get("ideas") or []
                        if i.get("state") == "dismissed"],
        })
    return out, len(files)


# ---------------------------------------------------------------------------
# One scene's report
# ---------------------------------------------------------------------------

def call_cost(prompt: str, has_image: bool, answer: Optional[dict] = None) -> dict:
    """An estimate of one director call (vertex_client's Gemini rates): the
    system line + prompt (+ the room image) in; the thinking budget + an
    answer (the stored one's length, else ANSWER_TOKENS) out."""
    from ...config import config

    model = config.vertex_gemini_fast_model
    rates = getattr(vertex_client, "_GEMINI_RATES", {}).get(model) or {"in": 0.30, "out": 2.50}
    tokens_in = (len(dmod.DIRECTOR_SYSTEM) + len(prompt)) / 4 + (ROOM_TOKENS if has_image else 0)
    tokens_out = dmod.THINKING_BUDGET + (len(json.dumps(answer)) / 4 if answer else ANSWER_TOKENS)
    usd = tokens_in * rates["in"] / 1e6 + tokens_out * rates["out"] / 1e6
    return {"model": model, "tokens_in": round(tokens_in), "tokens_out": round(tokens_out), "usd": round(usd, 4)}


def _changed_inputs(was: Optional[dict], now: dict) -> list[str]:
    if not isinstance(was, dict):
        return []
    return [k for k in now if was.get(k) != now[k]]


async def _library(catalog: dmod.Catalog, name: str) -> dict:
    got = await dmod._resolve(catalog, name)
    return {"name": name, "best": got[0] if got else None, "score": round(got[1], 3) if got else None,
            "needs": dmod.MATCH_STRONG}


def _thumb(src: Optional[Path], out: Path, boxes: Optional[list[dict]] = None) -> Optional[str]:
    """`src` at THUMB_W px (a JPEG in the cache), with `boxes` ({box, label})
    outlined; its path, or None."""
    if src is None or not Path(src).is_file():
        return None
    import cv2

    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    img = cv2.resize(img, (THUMB_W, max(1, int(round(h * THUMB_W / w)))), interpolation=cv2.INTER_AREA)
    H, W = img.shape[:2]
    below: dict[tuple, int] = {}                # icons sharing a spot (at other times) stack their labels
    for b in boxes or []:
        x0, y0 = int(b["box"]["x"] * W), int(b["box"]["y"] * H)
        x1, y1 = int((b["box"]["x"] + b["box"]["w"]) * W), int((b["box"]["y"] + b["box"]["h"]) * H)
        n = below.get((x0, y0, x1, y1), 0)
        below[(x0, y0, x1, y1)] = n + 1
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 0, 0), 3)
        cv2.rectangle(img, (x0, y0), (x1, y1), (242, 43, 127), 2)
        at = (x0, min(H - 4, y1 + 13 + 13 * n))
        cv2.putText(img, b["label"][:18], at, cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, b["label"][:18], at, cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1, cv2.LINE_AA)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    return str(out)


async def _want_scores(catalog: dmod.Catalog, want: list[str], shown: list[str], kept: list[str]) -> dict:
    """Precision and recall of what showed (and of what the AI kept) against
    the icons wanted; a wanted name resolves like "other:<noun>"."""
    ids, unknown = [], []
    for w in want:
        key = re.sub(r"[\s_]+", "-", str(w).strip().lower())
        if catalog.exists(key):
            ids.append(key)
            continue
        got = await dmod._resolve(catalog, str(w))
        if got and got[1] >= dmod.MATCH_STRONG:
            ids.append(got[0])
        else:
            unknown.append(str(w))
    wanted = list(dict.fromkeys(ids))

    def score(picked: list[str]) -> dict:
        hit = [i for i in picked if i in wanted]
        return {"picked": picked, "hit": hit,
                "precision": round(len(hit) / len(picked), 3) if picked else None,
                "recall": round(len(hit) / len(wanted), 3) if wanted else None}

    return {"wanted": wanted, "unknown": unknown, "shown": score(shown), "kept": score(kept)}


async def scene_report(scene: LocalScene, lines: Lines, *, cache: Path,
                       catalog: Optional[dmod.Catalog] = None, listen: bool = True,
                       want: Optional[list[str]] = None) -> dict:
    """One scene's report (module docstring, 1-6), as JSON-able data. Free:
    every paid call is refused while it runs."""
    with blocked():
        return await _scene_report(scene, lines, cache=Path(cache) / str(scene.run_id) / scene.work.name,
                                   catalog=catalog, listen=listen, want=want)


async def _scene_report(scene: LocalScene, lines: Lines, *, cache: Path, catalog: Optional[dmod.Catalog],
                        listen: bool, want: Optional[list[str]]) -> dict:
    out: dict[str, Any] = {"scene": scene.name, "run_id": scene.run_id, "target": scene.target,
                           "folder": str(scene.work), "lines": lines.public(), "notes": []}
    words, words_from = await asyncio.to_thread(scene_words, scene, lines, cache, listen=listen)
    out["words_from"] = words_from
    if words is None:
        out["state"] = "no_words"
        return out
    track, track_from = await asyncio.to_thread(scene_track, scene, cache, listen=listen)
    out["track_from"] = track_from
    catalog = catalog or dmod.default_catalog()
    stored = _read_json(scene.icons / "director.json")
    take = _take(scene)

    # 1. What was said.
    cands = dmod.candidates(words)
    out["said"] = {
        "source": words.get("source"), "transcript": words.get("transcript") or "",
        "duration": words.get("duration"), "voiced": words.get("voiced"), "digest": words.get("digest"),
        "words": [{"w": w["w"], "text": w["text"], "s": w.get("s")} for w in words.get("words") or []
                  if w.get("said")],
        "line_matches": (wordsmod.cache_key(scene.clip, lines.script) == words.get("key"))
        if scene.clip is not None and lines.known and words.get("key") else None,
    }
    if out["said"]["line_matches"] is False:
        out["notes"].append("The words were heard against another line than this one (was the script "
                            "edited?), so the key below cannot match.")
    out["explain"] = dmod.explain(words)
    out["lists"] = [list(g) for g in dmod.list_groups(words, cands)]
    out["candidates"] = cands

    # 2. What the AI is asked, and whether the stored answer still answers it.
    spots = list(track.spots) if track is not None else []
    frame0 = track.frame0 if track is not None else None
    has_image = bool(frame0 and spots and Path(frame0).is_file())
    ctx = dmod.DirectorCtx(words=words, script=lines.script, work_dir=scene.icons, topic=lines.topic,
                           others=list(lines.others), frame0=frame0, spots=spots, take=take, catalog=catalog)
    prompt = dmod.build_prompt(ctx, cands, catalog, has_image=has_image)
    inputs = dmod.key_inputs(ctx, catalog)
    key = dmod.director_key(ctx, catalog)
    raw = stored.get("raw") if stored and dmod.well_formed(stored.get("raw")) else None
    asked_prompt = dmod.build_prompt(ctx, cands, catalog, has_image=has_image, missing=stored.get("reasked")) \
        if stored and stored.get("reasked") else prompt
    out["asked"] = {
        "prompt": prompt, "chars": len(prompt), "image": has_image, "take": take,
        "prompt_digest": dmod.prompt_digest(asked_prompt),
        "stored_prompt_digest": (stored or {}).get("prompt_digest"),
        "cost": call_cost(prompt, has_image, raw),
    }
    k: dict[str, Any] = {"today": key, "stored": (stored or {}).get("key"), "inputs": inputs,
                         "changed": _changed_inputs((stored or {}).get("inputs"), inputs)}
    if not lines.known:
        k["state"], k["why"] = "unchecked", "The scene's line is unknown here, so its key cannot be checked."
    elif stored is None:
        k["state"], k["why"] = "not_asked", "The AI has not been asked for this scene yet: its Icons tab asks it once."
    elif stored.get("key") == key and stored.get("version") == dmod.DIRECTOR_VERSION:
        if stored.get("source") == "ai":
            k["state"], k["why"] = "reused", "The stored answer still answers this scene: no call."
        else:
            k["state"], k["why"] = "failed", "The AI failed on this scene; it is asked again only on 'Ask again' " \
                                             "or after a 10-minute wait."
    else:
        k["state"] = "ask_again"
        k["why"] = "The studio would ask the AI again (one call)" + (
            f": {', '.join(k['changed'])} changed." if k["changed"]
            else " — this answer was stored before its inputs were, so which one changed cannot be told.")
    out["key"] = k

    # 3. The stored answer.
    if stored is not None:
        rows = []
        for i in stored.get("ideas") or []:
            row = {f: i.get(f) for f in ("id", "cand_id", "source", "concept", "label", "icon", "quote", "word",
                                         "s", "priority", "strong", "verdict", "auto", "reason", "purpose")}
            row["by"] = dmod.refused_by(i, raw)
            m = _NO_ICON_FOR.match(str(i.get("reason") or "")) if row["by"] == "check" else None
            if m:
                row["library"] = await _library(catalog, m.group(1).rstrip("…"))
            rows.append(row)
        out["answer"] = {
            "source": stored.get("source"), "note": stored.get("note"), "error": stored.get("error"),
            "failed_at": stored.get("failed_at"), "raw_kept": raw is not None,
            "raw_digest": stored.get("raw_digest"), "reasked": stored.get("reasked"),
            "when": time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime((scene.icons / "director.json").stat().st_mtime)),
            "ideas": rows, "room": stored.get("room") or {},
        }

    # 4. The same answer judged by today's rules.
    replayed = None
    if stored is not None and stored.get("source") in ("ai", "keywords"):
        answer = raw if raw is not None else rebuilt_answer(stored)
        if answer is not None:
            got = await dmod.replay(stored, words, catalog, spots=spots, answer=answer)
            if got is not None:
                ideas, room = _plain(got[0]), _plain(got[1])
                same = ideas == _plain(stored.get("ideas") or []) and room == _plain(stored.get("room") or {})
                replayed = {"ideas": ideas, "room": room}
                out["replay"] = {"from": "raw" if raw is not None else "rebuilt", "same": same,
                                 "changes": diff_ideas(stored.get("ideas") or [], ideas),
                                 "room_same": room == _plain(stored.get("room") or {})}
    if "replay" not in out:
        out["replay"] = {"from": None, "same": None, "changes": [],
                         "why": "no AI answer to judge again" if stored is None or stored.get("source") != "ai"
                         else "the answer was not kept"}

    # 5. What shows.
    draft = starting_draft(scene.run_id, scene.target)
    if stored is not None:
        director = stored
    else:
        director = {"version": dmod.DIRECTOR_VERSION, "key": None, "source": "keywords",
                    "ideas": dmod.fallback(words, cands, catalog, "AI not asked yet"),
                    "room": {"spots": [], "home": None, "home_n": None, "home_why": ""},
                    "candidates": cands, "note": "AI not asked yet"}
    plan = await asyncio.to_thread(plan_of, draft, words, director, track)
    out["shown"] = _shown(plan)
    out["shown"]["auto_max"] = icon_el.auto_max(float((words.get("voiced") or [0, words.get("duration") or 0])[1])
                                                - float((words.get("voiced") or [0, 0])[0]))
    if replayed is not None and not out["replay"]["same"]:
        again = await asyncio.to_thread(plan_of, draft, words, {**stored, **replayed}, track)
        out["shown_after_replay"] = _shown(again)
    out["studio_plans"], out["studio_plans_total"] = studio_plans(scene)

    # 6. Without the AI.
    out["fallback"] = [{"icon": i["icon"], "label": i["label"], "quote": i["quote"], "s": i.get("s")}
                       for i in dmod.fallback(words, cands, catalog, "keywords only")]

    # Pictures: the room the AI read, and where the shown icons sit.
    room = scene.icons / "director_room.jpg"
    if not room.is_file() and has_image:
        room = dmod.room_image(frame0, spots, cache / "room.jpg") or room
    out["pictures"] = {
        "room": _thumb(room if room.is_file() else None, cache / "room_thumb.jpg"),
        "shown": _thumb(frame0, cache / "shown_thumb.jpg",
                        [{"box": it["box"], "label": f"{it['label']} {it['settle']:.1f}s"}
                         for it in out["shown"]["items"] if it.get("box")]),
    }
    if want is not None:
        kept = [r["icon"] for r in (out.get("answer") or {}).get("ideas", []) if r["verdict"] == "keep" and r["icon"]]
        out["want"] = await _want_scores(catalog, want, [it["icon"] for it in out["shown"]["items"]],
                                         list(dict.fromkeys(kept)))
    out["state"] = "ok"
    out["summary"] = summary(out)
    return out


def _clock(s: Optional[float]) -> str:
    return f"{float(s):.1f} s" if s is not None else "—"


def summary(rep: dict) -> list[str]:
    """The scene in a few plain sentences (the report's headline)."""
    if rep.get("state") != "ok":
        return [rep.get("words_from") or "No words."]
    out = []
    items = rep["shown"]["items"]
    out.append("Shown: " + (", ".join(f"{it['label']} at {_clock(it['settle'])}" for it in items) or "nothing") + ".")
    by_icon = {i["icon"]: i for i in rep["shown"]["ideas"]}
    rows = (rep.get("answer") or {}).get("ideas") or []
    missed = [f"{r['label']} — {by_icon[r['icon']]['reason']}" for r in rows
              if r.get("auto") and r["icon"] in by_icon and by_icon[r["icon"]]["state"] not in ("on",)]
    if missed:
        out.append("Picked by the AI but not shown: " + "; ".join(missed) + ".")
    # A hopeful word the AI refused is offered in the studio as a one-tap
    # suggestion (director.as_suggestions): said so.
    ai = [f"{r['label']} (“{r['reason']}”)"
          + (" — the studio offers it as a suggestion, a hopeful word"
             if ((by_icon.get(r["icon"]) or {}).get("why") or {}).get("by") == "hopeful" else "")
          for r in rows if r["by"] == "ai"]
    if ai:
        out.append("Refused by the AI: " + "; ".join(ai) + ".")
    checks = []
    for r in rows:
        if r["by"] != "check":
            continue
        lib = r.get("library")
        # The library may know it by now (a new synonym): the stored answer
        # stays as it was judged, and says so.
        extra = ((f" — the library finds {lib['best']} at {lib['score']:.2f} today, so a new answer keeps it"
                  if lib["score"] >= lib["needs"] else
                  f" — the library's best is {lib['best']} at {lib['score']:.2f}, it needs {lib['needs']:.2f}")
                 if lib and lib.get("best") else " — nothing in the library" if lib else "")
        checks.append(f"{r['label']} ({r['reason']}{extra})")
    if checks:
        out.append("Kept by the AI, refused by our rules: " + "; ".join(checks) + ".")
    silent = [r["label"] for r in rows if r["by"] == "silent"]
    if silent:
        out.append("The AI gave no verdict on: " + ", ".join(silent) + ".")
    added = {}
    for p in rep.get("studio_plans") or []:
        for a in p["added"]:
            added.setdefault(a["icon"], a["label"])
    if added:
        out.append("In the studio you added: " + ", ".join(f"{v} ({k})" for k, v in added.items()) + ".")
    if rep["key"]["state"] != "reused":
        cost = rep["asked"]["cost"]["usd"]
        out.append(rep["key"]["why"] + (f" About ${cost:.3f} a call." if rep["key"]["state"] in
                                        ("not_asked", "ask_again") else ""))
    if rep.get("answer") is None and rep.get("fallback"):
        out.append("Until then, suggestions only (never placed on their own): "
                   + ", ".join(f"{f['label']} on “{f['quote']}”" for f in rep["fallback"]) + ".")
    rp = rep.get("replay") or {}
    if rp.get("from"):
        out.append("Judged again by today's rules" + (" (from an answer rebuilt from its ideas)"
                                                      if rp["from"] == "rebuilt" else "")
                   + (": the same." if rp["same"] else f": {len(rp['changes'])} change(s)."))
    return out


# ---------------------------------------------------------------------------
# The HTML report
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#f7f6f4;--card:#fff;--fg:#1d1b19;--muted:#6f6a64;--line:#e5e2dd;--violet:#7f2bf2;--ok:#1f7a3a;
--warn:#a25b00;--bad:#b3261e;--chip:#f1edf9}
@media (prefers-color-scheme:dark){:root{--bg:#121110;--card:#1c1a18;--fg:#f3f1ee;--muted:#a39d96;--line:#2e2b28;
--violet:#b48cff;--ok:#6fd08c;--warn:#f0b35a;--bad:#ff8a80;--chip:#2a2238}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,-apple-system,
"Segoe UI",sans-serif;padding:24px 16px}main{max-width:1080px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}
h2{font-size:18px;margin:0 0 4px}h3{font-size:14px;margin:18px 0 6px;text-transform:uppercase;letter-spacing:.04em;
color:var(--muted)}.sub{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px;margin:16px 0}.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;
font-weight:600;border:1px solid currentColor}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.muted{color:var(--muted)}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{color:var(--muted);
font-weight:600}ul.sum{margin:8px 0 0;padding-left:18px}.said{font-size:15px;line-height:1.8}
.said mark{background:none;color:inherit;border-radius:4px;padding:0 2px}.said mark.kept{background:var(--chip);
color:var(--violet);font-weight:600}.said mark.dropped{color:var(--muted);text-decoration:line-through}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:18px}@media (max-width:760px){.grid{grid-template-columns:1fr}}
.pics{display:flex;gap:10px;flex-wrap:wrap}.pics figure{margin:0}.pics img{width:100%;max-width:300px;border-radius:8px;
border:1px solid var(--line);display:block}figcaption{font-size:12px;color:var(--muted)}code{font-size:12px}
details pre{white-space:pre-wrap;font-size:12px;background:var(--bg);padding:10px;border-radius:8px;max-height:420px;
overflow:auto}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _img(path: Optional[str]) -> str:
    if not path or not Path(path).is_file():
        return ""
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f'<img alt="" src="data:image/jpeg;base64,{data}">'


def _marked(transcript: str, explain: list[dict]) -> str:
    """The transcript with every keyword it matched marked: kept (violet) or
    dropped (struck through, with why on hover)."""
    spans = sorted(((e["pos"], e["end"], e) for e in explain if e.get("pos") is not None),
                   key=lambda s: (s[0], s[2]["dropped"] is not None, -s[1]))
    out, at = [], 0
    for pos, end, e in spans:
        if pos < at:
            continue
        out.append(_e(transcript[at:pos]))
        cls = "kept" if e["dropped"] is None else "dropped"
        why = e["icon"] if e["dropped"] is None else f"{e['icon']}: {dmod.DROPPED_WHY.get(e['dropped'], e['dropped'])}"
        out.append(f'<mark class="{cls}" title="{_e(why)}">{_e(transcript[pos:end])}</mark>')
        at = end
    out.append(_e(transcript[at:]))
    return "".join(out)


_STATE = {"on": ("Shown", "ok"), "no_room": ("Not shown", "warn"), "near_end": ("Not shown", "warn"),
          "zone": ("Not shown", "warn"), "limit": ("Not shown", "warn"), "no_spot": ("Not shown", "warn"),
          "refused": ("Refused", "muted"), "available": ("Suggestion", "muted"), "dismissed": ("Removed", "muted"),
          "gone": ("Gone", "bad")}
_BY = {"ai": ("Refused by the AI", "bad"), "check": ("Kept by the AI, refused by our rule", "warn"),
       "silent": ("No verdict from the AI", "bad"), None: ("Kept", "ok")}


def _scene_html(rep: dict) -> str:
    h = [f'<section class="card" id="{_e(rep["scene"])}"><h2>Run {_e(rep["run_id"])} · {_e(rep["target"])}</h2>',
         f'<div class="sub">{_e(Path(rep["folder"]).name)} · words: {_e(rep.get("words_from"))}'
         + (f' · speaker: {_e(rep.get("track_from"))}' if rep.get("track_from") else "") + "</div>"]
    if rep.get("state") != "ok":
        h.append(f"<p>{_e(rep.get('words_from'))}</p></section>")
        return "".join(h)
    h.append('<ul class="sum">' + "".join(f"<li>{_e(s)}</li>" for s in rep["summary"]) + "</ul>")
    for n in rep.get("notes") or []:
        h.append(f'<p class="warn">{_e(n)}</p>')

    said = rep["said"]
    h.append('<div class="grid"><div>')
    h.append("<h3>1 · What was said</h3>")
    h.append(f'<p class="said">{_marked(said["transcript"], rep["explain"])}</p>')
    h.append(f'<p class="sub">{_e(said["source"])} · {_e(said["duration"])} s'
             + (" · heard against this line ✓" if said.get("line_matches") else
                " · <span class='warn'>heard against another line</span>" if said.get("line_matches") is False else "")
             + "</p>")
    rows = "".join(
        f"<tr><td>“{_e(e['said'])}”</td><td>{_e(_clock(e['s']))}</td><td><code>{_e(e['icon'])}</code></td>"
        f"<td>{'strong' if e['strong'] else 'weak'}</td><td>"
        + ("<span class='ok'>a candidate</span>" if e["dropped"] is None
           else f"<span class='muted'>dropped: {_e(dmod.DROPPED_WHY.get(e['dropped'], e['dropped']))}</span>")
        + "</td></tr>" for e in rep["explain"])
    h.append('<div class="scroll"><table><tr><th>Keyword</th><th>At</th><th>Icon</th><th>Match</th><th></th></tr>'
             + (rows or "<tr><td colspan=5 class='muted'>No keyword matched.</td></tr>") + "</table></div>")
    if rep["lists"]:
        h.append(f'<p class="sub">Said as a list (one icon at most): words {_e(rep["lists"])}</p>')

    lines, asked, key = rep["lines"], rep["asked"], rep["key"]
    h.append("<h3>2 · What the AI is asked</h3>")
    h.append(f"<p><b>Line:</b> {_e(lines['script']) or '<span class=muted>unknown</span>'}<br>"
             f"<b>Topic:</b> {_e(lines['topic']) or '—'}"
             + (f"<br><b>Other scenes:</b> {_e(' / '.join(lines['others']))}" if lines["others"] else "")
             + f"<br><span class='sub'>from {_e(lines['source'])}{(' · ' + _e(lines['note'])) if lines['note'] else ''}</span></p>")
    cls = {"reused": "ok", "not_asked": "muted", "unchecked": "warn", "failed": "warn", "ask_again": "bad"}[key["state"]]
    h.append(f"<p><span class='badge {cls}'>{_e(key['state'].replace('_', ' '))}</span> {_e(key['why'])}<br>"
             f"<span class='sub'>key {_e(key['today'])}"
             + (f" · stored {_e(key['stored'])}" if key.get("stored") else "") + "</span></p>")
    cost = asked["cost"]
    same = asked.get("stored_prompt_digest")
    h.append(f"<p class='sub'>{len(rep['candidates'])} candidates · prompt {asked['chars']:,} characters"
             f"{' + the room image' if asked['image'] else ''} · about {cost['tokens_in']:,} tokens in, "
             f"{cost['tokens_out']:,} out · <b>~${cost['usd']:.4f}</b> a call ({_e(cost['model'])}) · "
             + ("the same prompt the AI answered" if same == asked["prompt_digest"] else
                "<span class='warn'>the prompt changed since the AI answered</span>" if same else
                "the prompt it answered was not kept")
             + "</p>")
    h.append(f"<details><summary>The prompt</summary><pre>{_e(asked['prompt'])}</pre></details>")
    h.append("</div><div class='pics'>")
    if rep["pictures"].get("room"):
        h.append(f"<figure>{_img(rep['pictures']['room'])}<figcaption>The room the AI reads (numbered spots)"
                 "</figcaption></figure>")
    if rep["pictures"].get("shown"):
        h.append(f"<figure>{_img(rep['pictures']['shown'])}<figcaption>Where the shown icons sit</figcaption>"
                 "</figure>")
    h.append("</div></div>")

    ans = rep.get("answer")
    h.append("<h3>3 · What the AI answered, and what we kept</h3>")
    if ans is None:
        h.append("<p class='muted'>No answer yet.</p>")
    else:
        h.append(f"<p class='sub'>{_e(ans['source'])} · {_e(ans['when'])}"
                 + (" · its raw answer is kept" if ans["raw_kept"] else " · stored before raw answers were kept")
                 + (f" · asked again for {_e(', '.join(ans['reasked']))}" if ans.get("reasked") else "")
                 + (f" · <span class='bad'>{_e(ans['error'])}</span>" if ans.get("error") else "") + "</p>")
        shown = {i["icon"]: i for i in rep["shown"]["ideas"]}
        trs = []
        for r in ans["ideas"]:
            by_label, by_cls = _BY.get(r["by"], (r["by"], "muted"))
            if r["verdict"] == "suggest":
                by_label, by_cls = "Suggestion (no AI)", "muted"
            st = shown.get(r["icon"])
            st_label, st_cls = _STATE.get(st["state"], (st["state"], "muted")) if st else ("—", "muted")
            why = r["reason"] if r["verdict"] != "keep" else r["purpose"]
            lib = r.get("library")
            if lib:
                why += (f" — best in the library: {lib['best']} at {lib['score']:.2f} (needs {lib['needs']:.2f})"
                        if lib.get("best") else " — nothing in the library")
            if st and st.get("reason") and st["state"] != "refused":
                why += f" · {st['reason']}"
            trs.append(
                f"<tr><td><b>{_e(r['label'])}</b><br><span class='sub'>{_e(r['concept'])}</span></td>"
                f"<td>“{_e(r['quote'])}”<br><span class='sub'>{_e(_clock(r['s']))}</span></td>"
                f"<td><code>{_e(r['icon'] or '—')}</code></td>"
                f"<td>{_e(r['priority'])}{' · strong' if r['strong'] else ''}{' · auto' if r['auto'] else ''}</td>"
                f"<td class='{by_cls}'>{_e(by_label)}</td><td class='{st_cls}' style='white-space:nowrap'>"
                f"{_e(st_label)}"
                + (f" {_e(_clock(next((it['settle'] for it in rep['shown']['items'] if it['icon'] == r['icon']), None)))}"
                   if st and st["state"] == "on" else "") + f"</td><td>{_e(why)}</td></tr>")
        h.append('<div class="scroll"><table><tr><th>Idea</th><th>Said</th><th>Icon</th><th>Priority</th>'
                 "<th>Verdict</th><th>Shows</th><th>Why</th></tr>" + "".join(trs) + "</table></div>")
        room = ans.get("room") or {}
        if room.get("spots"):
            h.append("<p class='sub'>Room: " + ", ".join(
                f"{s['n']}·{_e(s.get('id'))} {_e(s['verdict'])}" for s in room["spots"])
                + (f" · home {_e(room.get('home'))} ({_e(room.get('home_why'))})" if room.get("home") else "")
                + "</p>")

    rp = rep["replay"]
    h.append("<h3>4 · The same answer, judged by today's rules</h3>")
    if not rp.get("from"):
        h.append(f"<p class='muted'>Nothing to judge again: {_e(rp.get('why'))}.</p>")
    else:
        src = "its raw answer" if rp["from"] == "raw" else \
            "an answer rebuilt from its stored ideas (approximate: stored before raw answers were kept)"
        if rp["same"]:
            h.append(f"<p><span class='badge ok'>same</span> From {_e(src)}: every idea and the room come out "
                     "exactly as stored.</p>")
        else:
            items = "".join(f"<li><b>{_e(c['idea'])}</b>: " + (
                ", ".join(f"{_e(f)} {_e(v[0])} → {_e(v[1])}" for f, v in c["changed"].items())
                if "changed" in c else ("new: " + _e(c["now"]) if "now" in c else "gone")) + "</li>"
                for c in rp["changes"])
            h.append(f"<p><span class='badge warn'>changed</span> From {_e(src)}:</p><ul>{items}</ul>")
            again = rep.get("shown_after_replay")
            if again:
                h.append("<p class='sub'>Would show then: " + (", ".join(
                    f"{_e(it['label'])} at {_e(_clock(it['settle']))}" for it in again["items"]) or "nothing")
                    + "</p>")

    sh = rep["shown"]
    h.append("<h3>5 · What shows</h3>")
    h.append(f"<p class='sub'>The studio's starting draft (its default recipe + automatic icons), planned here "
             f"without the pattern's side · at most {sh['auto_max']} automatic icons for this length.</p>")
    rows = "".join(f"<tr><td><b>{_e(it['label'])}</b> <code>{_e(it['icon'])}</code></td><td>“{_e(it['word'])}”</td>"
                   f"<td>{_e(_clock(it['from']))} → {_e(_clock(it['to']))} (lands {_e(_clock(it['settle']))})</td>"
                   f"<td>{_e(it['spot'])}</td></tr>" for it in sh["items"])
    h.append('<div class="scroll"><table><tr><th>Icon</th><th>On the word</th><th>On screen</th><th>Spot</th></tr>'
             + (rows or "<tr><td colspan=4 class='muted'>Nothing shows on its own.</td></tr>") + "</table></div>")
    for w in sh.get("warnings") or []:
        h.append(f"<p class='warn'>{_e(w)}</p>")
    plans = rep.get("studio_plans") or []
    if plans:
        h.append(f"<p class='sub'>The studio's own plans for this scene ({rep['studio_plans_total']} saved; "
                 "the newest first):</p><ul>")
        for p in plans:
            items = ", ".join(f"{_e(i['label'])}{' (yours)' if i['source'] == 'user' else ''} at "
                              f"{_e(_clock(i['settle']))}" for i in p["items"]) or "nothing"
            extra = (" · you added " + ", ".join(_e(a["label"]) for a in p["added"]) if p["added"] else "") + \
                (" · you removed " + ", ".join(_e(a["label"]) for a in p["removed"]) if p["removed"] else "")
            h.append(f"<li><span class='sub'>{_e(p['when'])}</span> {items}{extra}</li>")
        h.append("</ul>")

    h.append("<h3>6 · Without the AI</h3>")
    fb = rep["fallback"]
    h.append("<p>" + (("Suggestions only, never placed on their own: " + ", ".join(
        f"{_e(f['label'])} on “{_e(f['quote'])}”" for f in fb)) if fb else "<span class='muted'>No keyword "
        "suggestions.</span>") + "</p>")
    wt = rep.get("want")
    if wt:
        def pr(s: dict) -> str:
            p = "—" if s["precision"] is None else f"{s['precision']:.0%}"
            r = "—" if s["recall"] is None else f"{s['recall']:.0%}"
            return f"precision {p}, recall {r}"
        h.append(f"<h3>Against the icons you wanted</h3><p>Wanted: {_e(', '.join(wt['wanted']) or '—')}"
                 + (f" (not found: {_e(', '.join(wt['unknown']))})" if wt["unknown"] else "")
                 + f"<br>Shown: {pr(wt['shown'])} · Kept by the AI: {pr(wt['kept'])}</p>")
    h.append("</section>")
    return "".join(h)


def render_html(reports: list[dict], *, generated: str, attempts: list[str]) -> str:
    """The report as one self-contained HTML page (pictures inlined)."""
    guard = ("<span class='badge ok'>No model was called</span>" if not attempts else
             f"<span class='badge bad'>{len(attempts)} paid call(s) were attempted and blocked: "
             f"{_e(', '.join(attempts))}</span>")
    def short(r: dict) -> str:
        if r.get("state") != "ok":
            return _e(r.get("words_from"))
        key = r["key"]
        cls = {"reused": "ok", "ask_again": "bad"}.get(key["state"], "muted")
        return (f"{_e(r['summary'][0])} <span class='badge {cls}'>{_e(key['state'].replace('_', ' '))}</span>")

    head = "".join(f"<tr><td><a href='#{_e(r['scene'])}'>{_e(r['scene'])}</a></td><td>{short(r)}</td></tr>"
                   for r in reports)
    body = "".join(_scene_html(r) for r in reports)
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Icon director dry run</title><style>{_CSS}</style></head><body><main>"
            f"<h1>Icon director dry run</h1><p class='sub'>{_e(generated)} · {len(reports)} scene(s) · "
            f"from the files the studio already has {guard}</p>"
            f"<section class='card'><div class='scroll'><table><tr><th>Scene</th><th>In short</th></tr>{head}"
            f"</table></div></section>{body}</main></body></html>")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

async def _lines_for(scene: LocalScene, *, offline: bool, rows: dict[int, Any]) -> Lines:
    if offline:
        return Lines(note="--offline: the database was not read")
    if scene.run_id not in rows:
        try:
            rows[scene.run_id] = await read_run(scene.run_id)
        except Exception as e:  # noqa: BLE001 — the report says so
            rows[scene.run_id] = e
    row = rows[scene.run_id]
    if isinstance(row, Exception):
        return Lines(note=f"the database could not be read ({type(row).__name__}: {str(row)[:120]})")
    return lines_of(row, scene.target)


async def run(root: Optional[Path] = None, *, run_id: Optional[int] = None, target: Optional[str] = None,
              given: Optional[Lines] = None, offline: bool = False, listen: bool = True,
              want: Optional[dict] = None, cache: Path) -> list[dict]:
    """Every local scene's report, one scene at a time (Whisper and MODNet
    are loaded at most once each, and only for a scene without words)."""
    rows: dict[int, Any] = {}
    out = []
    for sc in local_scenes(root, run_id=run_id, target=target):
        lines = given or await _lines_for(sc, offline=offline, rows=rows)
        out.append(await scene_report(sc, lines, cache=cache, listen=listen,
                                      want=(want or {}).get(sc.name)))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.services.rebrand_harness.director_dryrun",
                                 description="The icon director, checked for free (no model call).")
    ap.add_argument("--run", type=int)
    ap.add_argument("--target")
    ap.add_argument("--line")
    ap.add_argument("--topic", default="")
    ap.add_argument("--other", action="append", default=[])
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-listen", action="store_true")
    ap.add_argument("--want")
    ap.add_argument("--root")
    ap.add_argument("--out")
    ap.add_argument("--html")
    args = ap.parse_args(argv)
    if args.line is not None and (args.run is None or args.target is None):
        ap.error("--line needs --run and --target (it is one scene's line)")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # the summaries' dashes and quotes
    out_dir = Path(args.out) if args.out else studio_root().parent / "_dryrun"
    want = json.loads(Path(args.want).read_text(encoding="utf-8")) if args.want else None
    given = Lines(script=args.line, topic=args.topic, others=list(args.other), source="given") \
        if args.line is not None else None
    ATTEMPTS.clear()
    # psycopg's async connection needs a selector loop on Windows: this run's
    # own loop, never the process's policy.
    loop = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with blocked(), asyncio.Runner(loop_factory=loop) as runner:
        reports = runner.run(run(Path(args.root) if args.root else None, run_id=args.run, target=args.target,
                                 given=given, offline=args.offline, listen=not args.no_listen, want=want,
                                 cache=out_dir / "cache"))
    generated = time.strftime("%Y-%m-%d %H:%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps({"generated": generated, "paid_calls_attempted": ATTEMPTS,
                                                     "scenes": reports}, indent=1, default=str, ensure_ascii=False),
                                         encoding="utf-8")
    page = Path(args.html) if args.html else out_dir / "report.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_html(reports, generated=generated, attempts=list(ATTEMPTS)), encoding="utf-8")
    for r in reports:
        print(f"== {r['scene']}")
        for s in r.get("summary") or [r.get("words_from") or ""]:
            print("  " + s)
    print(f"report: {page}")
    if ATTEMPTS:
        print(f"PAID CALLS ATTEMPTED AND BLOCKED: {', '.join(ATTEMPTS)}", file=sys.stderr)
        return EXIT_PAID
    return 0


if __name__ == "__main__":
    sys.exit(main())
