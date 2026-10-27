"""Rebrand memory: what each finished reel shipped, scene by scene — kept so
the studio can learn the team's style later (R9). This round collects and
retrieves; nothing reads it back on its own yet.

  distill(recipe, *, trim=None) -> dict     pure: the choices a shipped
                                            version made (patterns, icons,
                                            colours, trim), no model
  snapshot_ready(run_id, seq, md=None)      once a Finalize's render is
                                            ready: one row per reel segment
                                            (untouched ones too — a skip is
                                            data), from the frozen plan
  start_snapshot(run_id, seq, md)           the poller's call: the snapshot in
                                            the background, bounded, never
                                            in the poller's way
  nearest(kind, *, vec=None, run_id=None, row_id=None, brand_sha=None, k=8,
          exclude_run=None)                 the closest rows of that kind
                                            (read only; a query never pays)
  Store / PgStore / MemoryStore, set_store

What a row is made of, and what it is not:
  * the plan Finalize froze (composition_plan: each segment's source,
    version and frames), the version rows (their stored recipe — the recipe
    itself stays there, never copied), and words.json / director.json read
    from the scene's studio folder on disk (null when missing). A snapshot
    never asks the icon director, never plans icons, never hears words —
    nothing here can cause a paid call but the one embed below;
  * the brand from one free SELECT of brand_profile_latest (its colours,
    fonts, style pillars; brand_sha filters retrieval to one brand);
  * one batched gemini_embed per ready reel, of the new rows' embed_text
    only ("<kind> | <aspect> | topic: … | said: …") — a text embedded before
    reuses its vector by embed_sha, so finalizing the same scripts again is
    $0. REBRAND_MEMORY_EMBED (default on) off, or a failed embed, leaves a
    NULL vector for a later backfill.

A segment identical to its target's last row (the same source, version,
end and recipe) is not written again, and (run_id, finalize_seq, target) is
unique: a retried snapshot writes nothing twice. No database connection is
held while embedding.

The table (yas_rebrand_choices) is made lazily by the app, the first time
a row is written (CREATE / ADD COLUMN IF NOT EXISTS, row-level security on
with no policy), as icon_feedback's is — never by a test: tests get the
MemoryStore (the conftest guard)."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import socket
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

log = logging.getLogger("archivist.rebrand.harness.memory")

SNAPSHOT_TIMEOUT_SEC = 30.0
EMBED_DIM = 768
TOPIC_MAX, LINE_MAX, TRANSCRIPT_MAX = 200, 600, 1200
CHOICES_MAX_BYTES = 32 * 1024
EMBED_TEXT_MAX = 2000
KINDS = ("intro", "main", "outro")


def embed_on() -> bool:
    """REBRAND_MEMORY_EMBED: on unless set to off (about $0.0001 a reel)."""
    return os.getenv("REBRAND_MEMORY_EMBED", "on").strip().lower() not in ("0", "off", "false", "no")


def _clip(v: Any, n: int) -> Optional[str]:
    s = " ".join(str(v).split()) if v is not None else ""
    return s[:n] if s else None


def _sha(value: Any, n: int = 16) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


# ---------------------------------------------------------------------------
# distill: the choices a shipped version made (pure)
# ---------------------------------------------------------------------------

def _pattern_choice(el: dict) -> dict:
    props = el.get("props") or {}
    res = props.get("resolved") or {}
    pl = el.get("placement") or {}
    an = el.get("animation") or {}
    hand = res.get("hand")
    off = pl.get("offset") or [0.0, 0.0]
    moved = (any(abs(float(v or 0.0)) > 1e-9 for v in off) or pl.get("size") not in (None, 1, 1.0)
             or abs(float(pl.get("rotate") or 0.0)) > 1e-9)
    if hand is None and moved:
        # Moved by hand, not yet frozen: the move as the recipe asks it.
        hand = {"dx": off[0], "dy": off[1], "scale": pl.get("size"), "rotate": pl.get("rotate")}
    elif isinstance(hand, dict):
        hand = {k: hand.get(k) for k in ("dx", "dy", "scale", "rotate")}
    start, end = an.get("start") or {}, an.get("end") or {}
    return {
        "slug": props.get("slug"), "z": el.get("z"), "layout": res.get("layout") or props.get("layout"),
        "colour_asked": props.get("colour"), "colour_drawn": res.get("colour"), "hand": hand,
        "follow": res.get("follow") or props.get("follow"),
        "span": {"from": start.get("sec") if start.get("mode") == "at_sec" else None,
                 "to": end.get("sec") if end.get("mode") == "at_sec" else None},
        "entrance": an.get("entrance"), "entrance_sec": an.get("duration_sec"),
        "exit": an.get("exit"), "exit_sec": an.get("exit_sec"),
    }


def _icon_choice(el: dict) -> dict:
    props = el.get("props") or {}
    an = el.get("animation") or {}
    added = {((a.get("glyph") or {}).get("id")): a for a in props.get("added") or []}
    tweaks = props.get("tweaks") or {}
    looks = props.get("looks") or {}
    items = []
    for r in props.get("resolved") or []:
        gid = (r.get("glyph") or {}).get("id")
        add = added.get(gid)
        glyph_source = (r.get("glyph") or {}).get("source")
        source = ("team" if glyph_source == "custom" else "added" if add is not None else "ai")
        tw = tweaks.get(gid) or {}
        look = looks.get(gid) or {}
        items.append({
            "glyph": gid, "glyph_source": glyph_source, "source": source, "label": r.get("label"),
            "word": r.get("word"), "said_at": tw.get("sec") if tw.get("off_word") else None,
            "from": r.get("from_sec"), "settle": r.get("settle_sec"), "to": r.get("to_sec"),
            "box": r.get("box"), "size": look.get("size"), "style": r.get("style"),
            "layer": r.get("layer"), "fill": r.get("fill"), "ink": r.get("ink"),
            "enter": (r.get("fx") or {}).get("entrance"), "leave": (r.get("fx") or {}).get("exit"),
            "pinned": bool(tw), "off_word": bool(tw.get("off_word")),
        })
    return {"on": bool(el.get("enabled", True)), "ai": props.get("ai", True), "style": props.get("style"),
            "colour": props.get("colour"), "entrance": an.get("entrance"),
            "dismissed": list(props.get("dismissed") or []), "items": items}


def distill(recipe: Optional[dict], *, trim: Optional[dict] = None) -> dict:
    """What a shipped version chose, by a plain rule (no model): its
    patterns (slug, layer, place, colour asked and drawn, hand move, follow,
    span, entrance and exit), its icons (on, AI, style, colour, entrance, and
    each shown one: glyph, where it came from, word, times, box, look,
    pinned), its colours and the trim ({auto, end, user}). {} for an
    untouched scene (no recipe). Kept under CHOICES_MAX_BYTES (the icons'
    items are cut last)."""
    if not recipe:
        return {}
    els = [e for e in recipe.get("elements") or [] if isinstance(e, dict)]
    out = {
        "engine": recipe.get("engine") or "omni",
        "patterns": [_pattern_choice(e) for e in els if e.get("kind") == "pattern" and e.get("enabled", True)],
        "icons": next((_icon_choice(e) for e in els if e.get("kind") == "icon"), None),
        "colours": recipe.get("colours") or {},
        "trim": dict(trim) if trim else None,
    }
    while len(json.dumps(out, default=str).encode("utf-8")) > CHOICES_MAX_BYTES:
        items = (out.get("icons") or {}).get("items") or []
        if items:
            items.pop()
        elif out["patterns"]:
            out["patterns"].pop()
        else:
            break
    return out


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

COLUMNS = (
    "run_id", "finalize_seq", "target", "scene_kind", "scene_index", "scene_count", "aspect", "src",
    "version_id", "rebranded", "engine", "engine_v", "recipe_hash",
    "src_fps", "src_frames", "auto_end", "end_frame", "user_end", "duration_sec",
    "topic", "line", "transcript", "words_digest", "colours", "brand_sha", "brand",
    "choices", "director", "rating", "rating_notes", "embed_text", "embed_sha", "embedding", "created_by",
)
JSON_COLUMNS = ("colours", "brand", "choices", "director")

DDL = """
CREATE TABLE IF NOT EXISTS yas_rebrand_choices (
    id            bigserial PRIMARY KEY,
    created_at    timestamptz DEFAULT now(),
    run_id        integer NOT NULL,
    finalize_seq  integer NOT NULL,
    target        text NOT NULL
)
"""
# Every column added one by one (IF NOT EXISTS): a later build adds its own
# the same way, and a table made by an older one is completed.
ADD_COLUMNS = (
    ("scene_kind", "text"), ("scene_index", "integer"), ("scene_count", "integer"), ("aspect", "text"),
    ("src", "text"), ("version_id", "integer"), ("rebranded", "boolean"), ("engine", "text"),
    ("engine_v", "integer"), ("recipe_hash", "text"), ("src_fps", "real"), ("src_frames", "integer"),
    ("auto_end", "integer"), ("end_frame", "integer"), ("user_end", "boolean"), ("duration_sec", "real"),
    ("topic", "text"), ("line", "text"), ("transcript", "text"), ("words_digest", "text"),
    ("colours", "jsonb"), ("brand_sha", "text"), ("brand", "jsonb"), ("choices", "jsonb"),
    ("director", "jsonb"), ("rating", "integer"), ("rating_notes", "text"), ("embed_text", "text"),
    ("embed_sha", "text"), ("embedding", "vector(768)"), ("created_by", "text"),
)
INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS yas_rebrand_choices_seq ON yas_rebrand_choices (run_id, finalize_seq, target)",
    "CREATE INDEX IF NOT EXISTS yas_rebrand_choices_target ON yas_rebrand_choices (run_id, target, id DESC)",
    "CREATE INDEX IF NOT EXISTS yas_rebrand_choices_kind ON yas_rebrand_choices (scene_kind, created_at)",
    "CREATE INDEX IF NOT EXISTS yas_rebrand_choices_embed ON yas_rebrand_choices (embed_sha)",
    "CREATE INDEX IF NOT EXISTS yas_rebrand_choices_brand ON yas_rebrand_choices (brand_sha)",
)
# Closed to the database's public API (as the other rebrand tables): RLS on,
# no policy.
DDL_RLS = "ALTER TABLE yas_rebrand_choices ENABLE ROW LEVEL SECURITY"
BRAND_SQL = "SELECT payload FROM brand_profile_latest WHERE kind='final_json' LIMIT 1"
SCRIPT_VECTOR_SQL = "SELECT script_embedding FROM yas_generations WHERE id = %s"
# What a row carries back to a reader (never the vector itself).
READ_COLUMNS = ("id", "created_at", *[c for c in COLUMNS if c != "embedding"])


class Store(Protocol):
    async def last_rows(self, run_id: int, targets: Sequence[str]) -> dict[str, dict]: ...
    async def vectors(self, shas: Sequence[str]) -> dict[str, list[float]]: ...
    async def insert_many(self, rows: Sequence[dict]) -> int: ...
    async def brand(self) -> Optional[dict]: ...
    async def script_vector(self, run_id: int) -> Optional[list[float]]: ...
    async def row_vector(self, row_id: int) -> Optional[list[float]]: ...
    async def nearest(self, kind: str, vec: Optional[list[float]], *, exclude_run: Optional[int],
                      brand_sha: Optional[str], k: int) -> list[dict]: ...


def _vec(v: Any) -> Optional[list[float]]:
    """A pgvector value (a numpy array, a list or its text form) as floats."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    try:
        out = [float(x) for x in v]
    except TypeError:
        return None
    return out or None


def _jsonish(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


class PgStore:
    """yas_rebrand_choices on the shared database. Reads are autocommit and
    say nothing when the table isn't there yet; the table is made the first
    time a row is written."""

    def __init__(self) -> None:
        self._ready = False

    async def _there(self, cur) -> bool:
        await cur.execute("SELECT to_regclass('public.yas_rebrand_choices') AS t")
        return bool((await cur.fetchone() or {}).get("t"))

    async def ensure_table(self) -> None:
        if self._ready:
            return
        from ...db import get_conn

        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(DDL)
                for name, kind in ADD_COLUMNS:
                    await cur.execute(f"ALTER TABLE yas_rebrand_choices ADD COLUMN IF NOT EXISTS {name} {kind}")
                for sql in INDEXES:
                    await cur.execute(sql)
        try:
            async with get_conn(autocommit=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(DDL_RLS)
        except Exception:  # noqa: BLE001 — the table works the same either way
            log.warning("[memory] couldn't switch row-level security on for yas_rebrand_choices", exc_info=True)
        self._ready = True

    async def last_rows(self, run_id: int, targets: Sequence[str]) -> dict[str, dict]:
        from ...db import get_conn

        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                if not await self._there(cur):
                    return {}
                await cur.execute(
                    "SELECT DISTINCT ON (target) target, src, version_id, end_frame, recipe_hash, finalize_seq "
                    "FROM yas_rebrand_choices WHERE run_id = %s AND target = ANY(%s) ORDER BY target, id DESC",
                    (int(run_id), list(targets)))
                return {str(r["target"]): dict(r) for r in await cur.fetchall()}

    async def vectors(self, shas: Sequence[str]) -> dict[str, list[float]]:
        from ...db import get_conn

        if not shas:
            return {}
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                if not await self._there(cur):
                    return {}
                await cur.execute(
                    "SELECT DISTINCT ON (embed_sha) embed_sha, embedding FROM yas_rebrand_choices "
                    "WHERE embed_sha = ANY(%s) AND embedding IS NOT NULL ORDER BY embed_sha, id DESC",
                    (list(shas),))
                rows = await cur.fetchall()
        out = {}
        for r in rows:
            v = _vec(r.get("embedding"))
            if v:
                out[str(r["embed_sha"])] = v
        return out

    async def insert_many(self, rows: Sequence[dict]) -> int:
        from psycopg.types.json import Jsonb

        from ...db import get_conn

        if not rows:
            return 0
        await self.ensure_table()
        marks = ", ".join("%s::vector" if c == "embedding" else "%s" for c in COLUMNS)
        sql = (f"INSERT INTO yas_rebrand_choices ({', '.join(COLUMNS)}) VALUES ({marks}) "
               "ON CONFLICT (run_id, finalize_seq, target) DO NOTHING")

        def value(r: dict, c: str) -> Any:
            v = r.get(c)
            if c in JSON_COLUMNS and v is not None:
                return Jsonb(v)
            return v

        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(sql, [tuple(value(r, c) for c in COLUMNS) for r in rows])
        return len(rows)

    async def brand(self) -> Optional[dict]:
        from ...db import get_conn

        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(BRAND_SQL)
                row = await cur.fetchone()
        payload = _jsonish(row["payload"]) if row else None
        return payload if isinstance(payload, dict) else None

    async def script_vector(self, run_id: int) -> Optional[list[float]]:
        from ...db import get_conn

        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(SCRIPT_VECTOR_SQL, (int(run_id),))
                row = await cur.fetchone()
        return _vec(row.get("script_embedding")) if row else None

    async def row_vector(self, row_id: int) -> Optional[list[float]]:
        from ...db import get_conn

        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                if not await self._there(cur):
                    return None
                await cur.execute("SELECT embedding FROM yas_rebrand_choices WHERE id = %s", (int(row_id),))
                row = await cur.fetchone()
        return _vec(row.get("embedding")) if row else None

    async def nearest(self, kind: str, vec: Optional[list[float]], *, exclude_run: Optional[int],
                      brand_sha: Optional[str], k: int) -> list[dict]:
        from ...db import get_conn

        cols = ", ".join(READ_COLUMNS)
        where = ["scene_kind = %(kind)s", "run_id <> %(excl)s"]
        params: dict[str, Any] = {"kind": kind, "excl": int(exclude_run) if exclude_run is not None else -1,
                                  "k": int(k)}
        if brand_sha:
            where.append("brand_sha = %(brand)s")
            params["brand"] = brand_sha
        dist = "NULL::float8"
        if vec:
            where.append("embedding IS NOT NULL")
            dist = "(embedding <=> %(vec)s::vector)"
            params["vec"] = list(vec)
        sql = (f"SELECT * FROM (SELECT DISTINCT ON (run_id, target) {cols}, {dist} AS distance "
               f"FROM yas_rebrand_choices WHERE {' AND '.join(where)} ORDER BY run_id, target, id DESC) t "
               "ORDER BY (rating IS NOT NULL) DESC, rebranded DESC NULLS LAST, distance ASC NULLS LAST, "
               "created_at DESC LIMIT %(k)s")
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                if not await self._there(cur):
                    return []
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for c in JSON_COLUMNS:
                d[c] = _jsonish(d.get(c))
            out.append(d)
        return out


def _cosine(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb or len(a) != len(b):
        return None
    return 1.0 - sum(x * y for x, y in zip(a, b)) / (na * nb)


class MemoryStore:
    """The same, in memory: tests, and anything that must not reach the
    database. `rows` in insertion order (each with its own id); `brand_row`
    the brand profile's payload; `scripts` {run_id: script_embedding}."""

    def __init__(self, *, brand: Optional[dict] = None, scripts: Optional[dict] = None) -> None:
        self.rows: list[dict] = []
        self.brand_row = brand
        self.scripts = {int(k): v for k, v in (scripts or {}).items()}
        self.inserts = 0

    async def last_rows(self, run_id: int, targets: Sequence[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.rows:
            if r["run_id"] == int(run_id) and r["target"] in targets:
                out[r["target"]] = copy.deepcopy(r)
        return out

    async def vectors(self, shas: Sequence[str]) -> dict[str, list[float]]:
        return {r["embed_sha"]: list(r["embedding"]) for r in self.rows
                if r.get("embed_sha") in set(shas) and r.get("embedding")}

    async def insert_many(self, rows: Sequence[dict]) -> int:
        n = 0
        for r in rows:
            if any(x["run_id"] == r["run_id"] and x["finalize_seq"] == r["finalize_seq"]
                   and x["target"] == r["target"] for x in self.rows):
                continue                             # ON CONFLICT DO NOTHING
            self.rows.append({"id": len(self.rows) + 1, **copy.deepcopy(dict(r))})
            n += 1
        self.inserts += 1
        return n

    async def brand(self) -> Optional[dict]:
        return copy.deepcopy(self.brand_row)

    async def script_vector(self, run_id: int) -> Optional[list[float]]:
        v = self.scripts.get(int(run_id))
        return list(v) if v else None

    async def row_vector(self, row_id: int) -> Optional[list[float]]:
        r = next((x for x in self.rows if x["id"] == int(row_id)), None)
        return list(r["embedding"]) if r and r.get("embedding") else None

    async def nearest(self, kind: str, vec: Optional[list[float]], *, exclude_run: Optional[int],
                      brand_sha: Optional[str], k: int) -> list[dict]:
        latest: dict[tuple, dict] = {}
        for r in self.rows:
            if r.get("scene_kind") != kind or r["run_id"] == exclude_run:
                continue
            if brand_sha and r.get("brand_sha") != brand_sha:
                continue
            if vec and not r.get("embedding"):
                continue
            latest[(r["run_id"], r["target"])] = r          # the newest last
        out = []
        for r in latest.values():
            d = {c: copy.deepcopy(r.get(c)) for c in READ_COLUMNS}
            d["distance"] = _cosine(vec, r["embedding"]) if vec else None
            out.append(d)
        out.sort(key=lambda d: (d.get("rating") is None, not d.get("rebranded"),
                                d["distance"] if d["distance"] is not None else math.inf, -int(d["id"])))
        return out[: int(k)]


_store: Store = PgStore()


def store() -> Store:
    return _store


def set_store(new: Store) -> Store:
    """Swap where the memory reads and writes (tests); returns the previous."""
    global _store
    old, _store = _store, new
    return old


# ---------------------------------------------------------------------------
# The snapshot of a ready reel
# ---------------------------------------------------------------------------

def _machine() -> str:
    try:
        return socket.gethostname()[:64]
    except OSError:
        return ""


def brand_of(payload: Optional[dict]) -> tuple[Optional[str], Optional[dict]]:
    """(brand_sha, brand) from the brand profile's payload: its primary and
    secondary colours, style pillars and fonts (what a rebrand is judged
    by); (None, None) without one."""
    if not isinstance(payload, dict):
        return None, None
    vi = payload.get("visual_identity") or {}
    brand = {"primary_colors": list(vi.get("primary_colors") or []),
             "secondary_colors": list(vi.get("secondary_colors") or []),
             "style_pillars": list(vi.get("style_pillars") or []),
             "fonts": list(vi.get("fonts") or [])}
    if not any(brand.values()):
        return None, None
    return _sha(brand), brand


def _read_json(path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def director_of(body: Optional[dict], recipe: Optional[dict], seen: Sequence[str] = ()) -> Optional[dict]:
    """What the icon director said for the scene (its director.json, as on
    disk) and what became of it in the shipped version: its key, version,
    model and source, and the icons it placed (top), the user kept, removed,
    saw in Suggested and left (ignored), it refused, and all seen. None
    without an answer on disk (a scene never opened)."""
    if not body:
        return None
    ideas = [i for i in body.get("ideas") or [] if isinstance(i, dict) and i.get("icon")]
    icon_el = next((e for e in (recipe or {}).get("elements") or []
                    if isinstance(e, dict) and e.get("kind") == "icon"), None)
    props = (icon_el or {}).get("props") or {}
    added = {(a.get("glyph") or {}).get("id") for a in props.get("added") or []}
    dismissed = set(props.get("dismissed") or [])
    top = sorted({i["icon"] for i in ideas if i.get("auto")})
    seen_set = set(seen or ())
    return {"key": body.get("key"), "version": body.get("version"),
            "model": (body.get("inputs") or {}).get("model"), "source": body.get("source"),
            "top": top, "kept": sorted(added & set(top)), "removed": sorted(dismissed & set(top)),
            "ignored": sorted((seen_set - added - set(top)) & {i["icon"] for i in ideas}),
            "refused": sorted({i["icon"] for i in ideas if i.get("verdict") == "refuse"}),
            "seen": sorted(seen_set)}


def _line_of(md: dict, target: str) -> str:
    """The segment's words: a scene's dialogue, a card's headline."""
    script = md.get("script") if isinstance(md.get("script"), dict) else {}
    if target in ("intro", "outro"):
        return " — ".join(x for x in (script.get(f"{target}_headline"), script.get(f"{target}_subtitle")) if x)
    i = int(target.split("_", 1)[1]) if target.startswith("scene_") else 0
    if int(md.get("scene_count") or 1) > 1:
        scenes = md.get("scenes") or []
        return str((scenes[i] if i < len(scenes) else {}).get("dialogue") or "")
    return str(script.get("dialogue") or "")


def embed_text_of(kind: str, aspect: str, topic: Optional[str], line: Optional[str]) -> str:
    return f"{kind} | {aspect} | topic: {topic or ''} | said: {line or ''}"[:EMBED_TEXT_MAX]


def _folder_of(run_id: int, target: str, path: Optional[Path]) -> Optional[Path]:
    """The scene's studio folder (studio.scene_base) — read for its words and
    director.json only; None when its clip isn't here."""
    if path is None or not Path(path).is_file():
        return None
    try:
        from . import studio

        folder = studio.scene_base(int(run_id), target, Path(path))
    except Exception:  # noqa: BLE001 — no folder: null context
        return None
    return folder if folder.is_dir() else None


def _recipe_hash(exp: dict) -> Optional[str]:
    arm = ((exp.get("arms") or {}).get("omni") or {})
    got = arm.get("recipe_hash") or (exp.get("evidence") or {}).get("recipe_hash")
    if got:
        return str(got)
    recipe = (exp.get("options") or {}).get("recipe")
    if not recipe:
        return None
    try:
        from . import recipe as rc

        return rc.recipe_hash(rc.normalize(recipe))
    except Exception:  # noqa: BLE001 — an older recipe this build can't read
        return None


async def rows_for(run: dict, seq: int, *, brand: Optional[dict] = None) -> list[dict]:
    """The rows of one ready reel (module doc), from its frozen plan, without
    their vectors. Reads: the version rows (one SELECT), the reel's clips on
    this disk (th_reel's local lookups) and the scene folders' words.json /
    director.json. [] when the row's plan isn't that seq's."""
    from .. import th_reel

    md = run.get("metadata") or {}
    plan = md.get("composition_plan") or {}
    if int(plan.get("seq") or -1) != int(seq):
        return []
    segments = [s for s in plan.get("segments") or [] if isinstance(s, dict) and s.get("target")]
    vids = sorted({int(s["version_id"]) for s in segments if s.get("version_id")})
    exps = await th_reel.store().experiments(vids) if vids else {}
    anims = await th_reel.store().anims(th_reel.all_anim_ids(md))
    try:
        targets = {t.target: t for t in await th_reel.resolve_sources(run, th_reel.DbIo(anims))}
    except Exception:  # noqa: BLE001 — no folders then: null words and director
        log.warning("[memory] run=%s: the reel's clips couldn't be looked up", run.get("id"), exc_info=True)
        targets = {}
    brand_sha, brand_cols = brand_of(brand)
    aspect = th_reel.aspect_of(md)
    topic = _clip(run.get("topic"), TOPIC_MAX)
    mains = sum(1 for s in segments if s.get("role") == "main")
    rows = []
    for s in segments:
        target = str(s["target"])
        kind = str(s.get("role") or ("main" if target.startswith("scene_") else target))
        vid = int(s["version_id"]) if s.get("version_id") else None
        exp = exps.get(vid) if vid else None
        opts = (exp or {}).get("options") or {}
        recipe = opts.get("recipe") if exp else None
        arm = ((exp or {}).get("arms") or {}).get("omni") or {}
        end = s.get("end") or {}
        frames = int(s.get("frames") or 0)
        trim = {"auto": end.get("auto"), "end": frames, "user": end.get("user") is not None}
        t = targets.get(target)
        # scene_base hashes (and, on a cold cache, decodes) the clip: in a
        # thread, so the snapshot's wait_for can stop waiting on it.
        folder = await asyncio.to_thread(_folder_of, int(run["id"]), target, t.path if t is not None else None)
        words = _read_json(folder / "icons" / "words.json") if folder is not None else None
        director = (_read_json(folder / "icons" / "director.json") if folder is not None and kind == "main"
                    else None)
        colours = _read_json(folder / "colours.json") if folder is not None else None
        line = _clip(_line_of(md, target), LINE_MAX)
        rating = (exp or {}).get("rating") or {}
        stars = rating.get("omni_stars") or rating.get("brand_stars")
        embed_text = embed_text_of(kind, aspect, topic, line)
        rows.append({
            "run_id": int(run["id"]), "finalize_seq": int(seq), "target": target, "scene_kind": kind,
            "scene_index": int(target.split("_", 1)[1]) if target.startswith("scene_") else None,
            "scene_count": mains, "aspect": aspect, "src": s.get("src"),
            "version_id": vid, "rebranded": vid is not None,
            "engine": (opts.get("engine") or "omni") if exp else None,
            "engine_v": ((arm.get("house") or {}).get("engine_v")) if exp else None,
            "recipe_hash": _recipe_hash(exp) if exp else None,
            "src_fps": end.get("src_fps"), "src_frames": end.get("src_frames"), "auto_end": end.get("auto"),
            "end_frame": frames, "user_end": end.get("user") is not None,
            "duration_sec": round(frames / 30.0, 3) if frames else None,
            "topic": topic, "line": line,
            "transcript": _clip((words or {}).get("transcript")
                                or " ".join(w.get("text") or "" for w in (words or {}).get("words") or []
                                            if w.get("said")), TRANSCRIPT_MAX),
            "words_digest": (words or {}).get("digest"),
            "colours": ({k: colours.get(k) for k in ("colour", "ground", "ink", "fell_back")}
                        if colours else None),
            "brand_sha": brand_sha, "brand": brand_cols,
            "choices": distill(recipe, trim=trim) if recipe else {},
            "director": director_of(director, recipe, opts.get("seen_ideas") or ()) if director else None,
            "rating": int(stars) if stars else None, "rating_notes": _clip(rating.get("notes"), 1000),
            "embed_text": embed_text, "embed_sha": _sha(embed_text), "embedding": None,
            "created_by": _machine(),
        })
    return rows


def _same(row: dict, last: Optional[dict]) -> bool:
    """A segment unchanged since its target's last row."""
    return bool(last) and all(row.get(k) == last.get(k) for k in ("src", "version_id", "end_frame", "recipe_hash"))


async def _embed(texts: list[str]) -> list[Optional[list[float]]]:
    """One batched embed (the one paid call a snapshot may make); None per
    text when it is off or fails."""
    if not texts:
        return []
    if not embed_on():
        return [None] * len(texts)
    from .. import vertex_client

    try:
        vecs = await vertex_client.gemini_embed(list(texts), dim=EMBED_DIM)
    except Exception as e:  # noqa: BLE001 — a NULL vector, backfilled later
        log.warning("[memory] %d texts not embedded: %s", len(texts), str(e)[:200] or type(e).__name__)
        return [None] * len(texts)
    return [list(v) if v else None for v in vecs] + [None] * max(0, len(texts) - len(vecs))


async def _snapshot(run_id: int, seq: int, md: Optional[dict]) -> int:
    from .. import th_reel

    run = await th_reel.store().run(run_id)
    if run is None:
        return 0
    if md is not None:
        run = {**run, "metadata": md}
    try:
        brand = await store().brand()
    except Exception:  # noqa: BLE001 — rows without the brand
        log.warning("[memory] the brand profile couldn't be read", exc_info=True)
        brand = None
    rows = await rows_for(run, seq, brand=brand)
    if not rows:
        return 0
    last = await store().last_rows(int(run_id), [r["target"] for r in rows])
    rows = [r for r in rows if not _same(r, last.get(r["target"]))]
    if not rows:
        return 0
    # Every read done (no connection held): texts embedded before reuse
    # their vector; the new ones in one batched call.
    have = await store().vectors(sorted({r["embed_sha"] for r in rows}))
    new = sorted({r["embed_sha"]: r["embed_text"] for r in rows if r["embed_sha"] not in have}.items())
    got = await _embed([text for _sha_, text in new])
    have.update({sha: v for (sha, _t), v in zip(new, got) if v})
    for r in rows:
        r["embedding"] = have.get(r["embed_sha"])
    return await store().insert_many(rows)


async def snapshot_ready(run_id: int, seq: int, md: Optional[dict] = None) -> int:
    """Write the ready reel's rows (module doc): how many. Bounded
    (SNAPSHOT_TIMEOUT_SEC) and never raising — a memory row is never the
    reel's failure."""
    try:
        return await asyncio.wait_for(_snapshot(int(run_id), int(seq), md), SNAPSHOT_TIMEOUT_SEC)
    except Exception as e:  # noqa: BLE001
        log.warning("[memory] run=%s seq=%s: the reel's choices weren't kept: %s", run_id, seq,
                    str(e)[:200] or type(e).__name__)
        return 0


_PENDING: set[asyncio.Task] = set()


def start_snapshot(run_id: int, seq: int, md: Optional[dict] = None) -> Optional[asyncio.Task]:
    """snapshot_ready in the background (the poller's ready branch): it
    never waits for it. The task (for tests)."""
    try:
        task = asyncio.ensure_future(snapshot_ready(run_id, seq, copy.deepcopy(md) if md else None))
    except Exception:  # noqa: BLE001
        log.exception("[memory] the snapshot couldn't start")
        return None
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

async def nearest(kind: str, *, vec: Optional[Sequence[float]] = None, run_id: Optional[int] = None,
                  row_id: Optional[int] = None, brand_sha: Optional[str] = None, k: int = 8,
                  exclude_run: Optional[int] = None) -> list[dict]:
    """The rows of `kind` (intro | main | outro) closest to a query, the
    latest per (run, target), never of the excluded run (default: the query
    run), of one brand when given; rated rows first, then rebranded ones,
    then by cosine distance. The query vector is given (`vec`), a row's own
    (`row_id`) or the run's script_embedding (`run_id`) — never embedded
    here: a query never pays. Read only."""
    if kind not in KINDS:
        raise ValueError(f"kind is one of {KINDS}")
    q = list(vec) if vec else None
    if q is None and row_id is not None:
        q = await store().row_vector(int(row_id))
    if q is None and run_id is not None:
        q = await store().script_vector(int(run_id))
    excl = exclude_run if exclude_run is not None else run_id
    return await store().nearest(kind, q, exclude_run=excl, brand_sha=brand_sha, k=int(k))
