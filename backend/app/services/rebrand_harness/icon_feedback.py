"""What the user did with the icon director's picks, logged at each Generate
— the free signal that later tunes the director (its "house taste").

  feedback_of(director, props, shown)  -> [row]   pure: one row per icon the
                                                   director offered or the
                                                   user added
  after_generate(exp, recipe, plan)    -> Task | None   the router's call after
                                                   a version starts: rows written
                                                   in the background, never in
                                                   Generate's way
  record(rows)                         -> bool    best-effort: never raises
  set_store(store) / MemoryStore / PgStore

Outcomes (OUTCOMES):
  shown_ai         the AI's own pick (placed on its own), shipped untouched
  kept             the AI's own pick the user pressed Keep on (theirs now)
  removed          the AI's own pick the user removed
  dropped          the AI's own pick with no room this time
  added_refused    an idea the AI (or one of our rules) refused, added anyway
  added_suggested  a suggestion (a hopeful word, one below the automatic
                   floor, a keyword match with the AI off), added
  added_library    a library icon the director never offered, added
  added_team       one of the team's own icons, added
  ignored          an idea offered in Add icon → Suggested (available, or
                   with no room), seen there while the draft was made
                   (options.seen_ideas), and not taken

Each row also says the scene's kind (scene_kind: main | intro | outro), the
idea's own source and state in the plan (idea_source: ai | keyword | team |
user; idea_state: on | available | no_room | near_end | refused | …) and
whether it was seen in Suggested (seen) — each only when known.

No rows for a version that keeps another's icons ("Keep v3's icons":
nothing was decided anew), nor without the director's answer.

The table (yas_rebrand_icon_feedback) is made by the app the first time a
row is written (additive, IF NOT EXISTS), never by a test: tests use
MemoryStore. Rows are facts about a version, never read back by the studio;
icon_director_report reads them (read only).
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Iterable, Optional, Protocol, Sequence

from . import recipe as rc

log = logging.getLogger("archivist.rebrand.harness.icon_feedback")

OUTCOMES = ("shown_ai", "kept", "removed", "dropped", "added_refused", "added_suggested", "added_library",
            "added_team", "ignored")
# An idea left in Suggested is one it could have been taken from.
# (each state an idea is left out in: elements/icon.DROP_STATES)
OFFERED = ("available", "no_room", "zone", "near_end", "limit", "no_spot")
RECORD_TIMEOUT_SEC = 20.0
TEXT_MAX = 120


def _clip(v: Any, n: int = TEXT_MAX) -> Optional[str]:
    s = " ".join(str(v).split()) if v is not None else ""
    return s[:n] if s else None


def _ideas_by_icon(director: dict) -> dict[str, dict]:
    """One idea per icon: the automatic one, else a kept one, else the best
    of the rest (the planner's rule, elements/icon._ideas_by_icon)."""
    order = {"keep": 0, "suggest": 1, "refuse": 2}
    out: dict[str, dict] = {}
    for i in director.get("ideas") or []:
        icon = i.get("icon")
        if not icon:
            continue
        rank = (0 if i.get("auto") else 1, order.get(i.get("verdict"), 3), -int(i.get("priority") or 0),
                int(i.get("w") or 0))
        have = out.get(icon)
        if have is None or rank < have[0]:
            out[icon] = (rank, i)
    return {k: v[1] for k, v in out.items()}


def _glyph_of(item: Any) -> str:
    g = item.get("glyph") if isinstance(item, dict) else getattr(item, "glyph", None)
    if isinstance(g, dict):
        return str(g.get("id") or "")
    return str(getattr(g, "id", "") or "")


def feedback_of(director: Optional[dict], props: rc.IconProps, shown: Iterable[Any], *,
                ideas: Optional[Sequence[dict]] = None, seen: Optional[Iterable[str]] = None) -> list[dict]:
    """What the user did with the director's picks (module docstring), as
    rows: {glyph, label, concept, quote, said_at, ai_verdict, ai_priority,
    ai_auto, outcome, shown, director_key, director_version, model, source,
    idea_source, idea_state, seen}. `shown`: the icons the version stamps
    (its props.resolved); `ideas`: the plan's ideas (their state and source
    in it); `seen`: the glyphs seen in Suggested while the draft was made —
    with them, an idea seen and left is an `ignored` row. Pure."""
    director = director or {}
    plan_ideas = {_glyph_of(i): i for i in ideas or [] if isinstance(i, dict)}
    seen_ids = set(seen) if seen is not None else None
    ideas = _ideas_by_icon(director)
    shown_ids = {_glyph_of(r) for r in shown}
    added = {a.glyph.id: a for a in props.added}
    dismissed = set(props.dismissed)
    meta = {"director_key": director.get("key"), "director_version": director.get("version"),
            "model": (director.get("inputs") or {}).get("model"), "source": director.get("source")}
    rows: list[dict] = []

    def row(gid: str, outcome: str, idea: Optional[dict], label: Optional[str]) -> None:
        idea = idea or {}
        planned = plan_ideas.get(gid) or {}
        rows.append({
            **meta, "glyph": gid, "label": _clip(label or idea.get("label"), 40),
            "concept": _clip(idea.get("concept"), 60), "quote": _clip(idea.get("quote"), 80),
            "said_at": round(float(idea["s"]), 3) if idea.get("s") is not None else None,
            "ai_verdict": idea.get("verdict"), "ai_priority": int(idea["priority"]) if idea.get("priority") else None,
            "ai_auto": bool(idea.get("auto")) if idea else None, "outcome": outcome, "shown": gid in shown_ids,
            "idea_source": planned.get("source"), "idea_state": planned.get("state"),
            "seen": (gid in seen_ids) if seen_ids is not None else None,
        })

    for gid, idea in sorted(ideas.items(), key=lambda kv: (int(kv[1].get("w") or 0), kv[0])):
        if not idea.get("auto"):
            continue
        add = added.get(gid)
        outcome = ("kept" if add is not None else "removed" if gid in dismissed
                   else "shown_ai" if gid in shown_ids else "dropped")
        row(gid, outcome, idea, add.label if add is not None else None)
    for gid, add in added.items():
        idea = ideas.get(gid)
        if idea is not None and idea.get("auto"):
            continue                                 # "kept", above
        if add.glyph.source == "custom":
            outcome = "added_team"
        elif idea is None:
            outcome = "added_library"
        elif idea.get("verdict") == "refuse":
            outcome = "added_refused"
        else:
            outcome = "added_suggested"
        row(gid, outcome, idea, add.label)
    if seen_ids:
        # Offered in Suggested, seen there, and left.
        done = {r["glyph"] for r in rows}
        for gid, planned in plan_ideas.items():
            if (gid in seen_ids and gid not in done and gid not in added and gid not in shown_ids
                    and planned.get("state") in OFFERED):
                row(gid, "ignored", ideas.get(gid), planned.get("label"))
    return rows


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

class FeedbackStore(Protocol):
    async def insert_many(self, rows: Sequence[dict]) -> int: ...


_COLUMNS = ("run_id", "target", "version_id", "director_key", "director_version", "model", "source", "glyph",
            "label", "concept", "quote", "said_at", "ai_verdict", "ai_priority", "ai_auto", "outcome", "shown",
            "created_by", "scene_kind", "idea_source", "idea_state", "seen")

_DDL = """
CREATE TABLE IF NOT EXISTS yas_rebrand_icon_feedback (
    id               bigserial PRIMARY KEY,
    created_at       timestamptz DEFAULT now(),
    run_id           integer,
    target           text,
    version_id       integer,
    director_key     text,
    director_version integer,
    model            text,
    source           text,
    glyph            text NOT NULL,
    label            text,
    concept          text,
    quote            text,
    said_at          real,
    ai_verdict       text,
    ai_priority      integer,
    ai_auto          boolean,
    outcome          text NOT NULL,
    shown            boolean,
    created_by       text NOT NULL DEFAULT ''
)
"""
# Added since the table was first made (additive, IF NOT EXISTS).
_DDL_COLUMNS = tuple(f"ALTER TABLE yas_rebrand_icon_feedback ADD COLUMN IF NOT EXISTS {c} {kind}"
                     for c, kind in (("scene_kind", "text"), ("idea_source", "text"), ("idea_state", "text"),
                                     ("seen", "boolean")))
_DDL_INDEX = ("CREATE INDEX IF NOT EXISTS yas_rebrand_icon_feedback_version "
              "ON yas_rebrand_icon_feedback (director_version, model, created_at)")
# Closed to the database's public API (as yas_rebrand_icons): RLS on, no policy.
_DDL_RLS = "ALTER TABLE yas_rebrand_icon_feedback ENABLE ROW LEVEL SECURITY"


class PgStore:
    """yas_rebrand_icon_feedback on the shared database, made lazily by the
    app (additive, IF NOT EXISTS). Writes only; icon_director_report reads."""

    def __init__(self) -> None:
        self._ready = False

    async def ensure_table(self) -> None:
        if self._ready:
            return
        from ...db import get_conn

        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_DDL)
                for sql in _DDL_COLUMNS:
                    await cur.execute(sql)
                await cur.execute(_DDL_INDEX)
        try:
            async with get_conn(autocommit=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_DDL_RLS)
        except Exception:  # noqa: BLE001 — the log works the same either way
            log.warning("[feedback] couldn't switch row-level security on for yas_rebrand_icon_feedback",
                        exc_info=True)
        self._ready = True

    async def insert_many(self, rows: Sequence[dict]) -> int:
        from ...db import get_conn

        if not rows:
            return 0
        await self.ensure_table()
        sql = (f"INSERT INTO yas_rebrand_icon_feedback ({', '.join(_COLUMNS)}) "
               f"VALUES ({', '.join(['%s'] * len(_COLUMNS))})")
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(sql, [tuple(r.get(c) for c in _COLUMNS) for r in rows])
        return len(rows)


class MemoryStore:
    """The log in memory (tests, and anything that must not reach the
    database)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def insert_many(self, rows: Sequence[dict]) -> int:
        self.rows.extend(dict(r) for r in rows)
        return len(rows)


_store: FeedbackStore = PgStore()


def set_store(store: FeedbackStore) -> FeedbackStore:
    """Swap where the rows go (tests); returns the previous store."""
    global _store
    old, _store = _store, store
    return old


def _machine() -> str:
    try:
        return socket.gethostname()[:64]
    except OSError:
        return ""


async def record(rows: Sequence[dict]) -> bool:
    """Write the rows (best-effort: a table that can't be reached is logged,
    never raised)."""
    if not rows:
        return True
    try:
        await asyncio.wait_for(_store.insert_many(list(rows)), RECORD_TIMEOUT_SEC)
        return True
    except Exception as e:  # noqa: BLE001 — a log line, never Generate's failure
        log.warning("[feedback] %d icon rows not written: %s", len(rows), str(e)[:200])
        return False


_PENDING: set[asyncio.Task] = set()


def rows_for(exp: dict, recipe: rc.Recipe, plan: Any) -> list[dict]:
    """The rows of one Generate: the version (exp), its stored recipe (the
    icons it stamps: props.resolved) and the plan it froze (its director's
    answer). [] for none to write."""
    el = rc.icon_element(recipe)
    if el is None or plan is None or getattr(plan, "frozen", None) is not None:
        return []
    director = getattr(plan, "director", None)
    if not isinstance(director, dict):
        return []                                  # not a plan the director made
    props = rc.icon_props(el)
    if not director.get("ideas") and not props.added:
        return []
    shown = props.resolved if props.resolved is not None else plan.resolved()
    opts = exp.get("options") or {}
    base = {"run_id": int(exp["run_id"]) if exp.get("run_id") is not None else None,
            "target": recipe.target or opts.get("target"),
            "version_id": int(exp["id"]) if exp.get("id") is not None else None, "created_by": _machine(),
            "scene_kind": opts.get("scene_kind")}
    seen = opts.get("seen_ideas")
    return [{**base, **r} for r in feedback_of(director, props, shown, ideas=getattr(plan, "ideas", None),
                                               seen=seen if isinstance(seen, list) else None)]


def after_generate(exp: dict, recipe: rc.Recipe, plan: Any) -> Optional[asyncio.Task]:
    """Log what the user did with the icons of the version Generate just
    started (rows_for), in the background: Generate never waits for it and
    never fails for it. The task (for tests), or None when there is nothing
    to write."""
    try:
        rows = rows_for(exp, recipe, plan)
    except Exception:  # noqa: BLE001 — a log line, never Generate's failure
        log.exception("[feedback] couldn't work out the icon rows")
        return None
    if not rows:
        return None
    task = asyncio.ensure_future(record(rows))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task
