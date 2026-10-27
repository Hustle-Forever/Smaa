"""Rebrand experiment — persistence + the self-feedback memory.

One row per "Rebrand" click in `yas_rebrand_experiments`. The row is the single
source of truth for the UI (status, brief, both arms) AND the RAG memory the
next brief learns from (embedding + automatic critique + human rating).

Kept in its OWN table rather than the TH v2 run's metadata on purpose:
  - the two arms run concurrently and each patches only its own key with an
    atomic `jsonb_set` — no read-modify-write race on the run row;
  - the TH v2 pipeline never sees (or clobbers) rebrand state;
  - "nearest past rebrands" is a plain pgvector query.

The table is created lazily, once per process (the edit_recipes pattern).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn
from .vertex_client import gemini_embed

log = logging.getLogger("archivist.rebrand.store")

# "brand" = the 42 brand frame (graphics over the untouched footage). Rows from
# before it existed simply have no "brand" key; public() reports it skipped.
ARMS = ("omni", "brand")
# Arms no new run starts, still read so old experiments keep their results,
# ratings and lessons: Veo, retired from Rebrand on 2026-09-25.
LEGACY_ARMS = ("veo",)
ALL_ARMS = ARMS + LEGACY_ARMS

# Experiment lifecycle.
ST_QUEUED = "queued"
ST_BRIEFING = "briefing"
ST_RUNNING = "running"
ST_READY = "ready"          # at least one arm produced a video
ST_FAILED = "failed"        # no arm produced a video
ST_CANCELLED = "cancelled"
ST_INTERRUPTED = "interrupted"  # the process died mid-run (restart/redeploy)
ACTIVE_STATUSES = (ST_QUEUED, ST_BRIEFING, ST_RUNNING)

# Arm lifecycle.
ARM_QUEUED = "queued"
ARM_RUNNING = "running"
ARM_READY = "ready"
ARM_FAILED = "failed"
ARM_SKIPPED = "skipped"

_TABLE_READY = False

_DDL = """
CREATE TABLE IF NOT EXISTS yas_rebrand_experiments (
    id          bigserial PRIMARY KEY,
    run_id      bigint NOT NULL,
    status      text NOT NULL DEFAULT 'queued',
    stage       text,
    options     jsonb NOT NULL DEFAULT '{}'::jsonb,
    brief       jsonb,
    evidence    jsonb,
    arms        jsonb NOT NULL DEFAULT '{}'::jsonb,
    winner      text,
    rating      jsonb,
    lesson      text,
    embed_text  text,
    embedding   vector(768),
    error       text,
    cost_usd    real DEFAULT 0,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
)
"""

_IDX_RUN = """
CREATE INDEX IF NOT EXISTS yas_rebrand_experiments_run_idx
    ON yas_rebrand_experiments (run_id, id DESC)
"""

_COLS = (
    "id, run_id, status, stage, options, brief, evidence, arms, winner, "
    "rating, lesson, error, cost_usd, created_at, updated_at"
)


async def ensure_table() -> None:
    """Create the table on first use. Unlike the best-effort RAG tables this
    one is REQUIRED (it holds the experiment itself), so failures raise."""
    global _TABLE_READY
    if _TABLE_READY:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)
            await cur.execute(_IDX_RUN)
    _TABLE_READY = True


def _jsonish(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _row(r: Any) -> dict:
    d = dict(r)
    for k in ("options", "brief", "evidence", "arms", "rating"):
        d[k] = _jsonish(d.get(k))
    d["arms"] = d.get("arms") or {}
    d["options"] = d.get("options") or {}
    return d


async def create_experiment(run_id: int, options: dict) -> dict:
    await ensure_table()
    arms = {a: {"status": ARM_QUEUED, "steps": []} for a in ARMS}
    for a in ARMS:
        if not options.get(f"run_{a}", True):
            arms[a] = {"status": ARM_SKIPPED, "steps": [], "note": "turned off for this run"}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO yas_rebrand_experiments (run_id, status, stage, options, arms)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING {_COLS}
                """,
                (int(run_id), ST_QUEUED, "queued", Jsonb(options), Jsonb(arms)),
            )
            return _row(await cur.fetchone())


async def get_experiment(exp_id: int) -> Optional[dict]:
    await ensure_table()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLS} FROM yas_rebrand_experiments WHERE id = %s",
                (int(exp_id),),
            )
            r = await cur.fetchone()
    return _row(r) if r else None


async def list_for_run(
    run_id: int, *, limit: int = 12, target: Optional[str] = None,
) -> list[dict]:
    """This reel's experiments, newest first. `target` narrows to one clip
    ("reel" | "intro" | "outro" | "scene_N"); rows from before per-clip
    rebrands existed carry no target and count as "reel"."""
    await ensure_table()
    sql = f"SELECT {_COLS} FROM yas_rebrand_experiments WHERE run_id = %s"
    params: list[Any] = [int(run_id)]
    if target:
        sql += " AND COALESCE(options->>'target', 'reel') = %s"
        params.append(target)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(int(limit))
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            return [_row(r) for r in await cur.fetchall()]


_UPDATABLE = {
    "status", "stage", "brief", "evidence", "winner", "rating", "lesson",
    "error", "cost_usd",
}


async def update_experiment(exp_id: int, **fields: Any) -> None:
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in _UPDATABLE:
            raise ValueError(f"not an updatable experiment field: {k}")
        sets.append(f"{k} = %s")
        vals.append(Jsonb(v) if isinstance(v, (dict, list)) else v)
    if not sets:
        return
    sets.append("updated_at = now()")
    vals.append(int(exp_id))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE yas_rebrand_experiments SET {', '.join(sets)} WHERE id = %s",
                tuple(vals),
            )


async def patch_arm(exp_id: int, arm: str, patch: dict) -> None:
    """Shallow-merge `patch` into arms[arm] in ONE statement, so the arm tasks
    can report progress concurrently without overwriting each other."""
    if arm not in ALL_ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_rebrand_experiments
                   SET arms = jsonb_set(
                         arms, %s::text[],
                         COALESCE(arms -> %s::text, '{}'::jsonb) || %s::jsonb, true),
                       updated_at = now()
                 WHERE id = %s
                """,
                ([arm], arm, Jsonb(patch), int(exp_id)),
            )


async def append_arm_step(exp_id: int, arm: str, step: dict) -> None:
    """Append one progress step ({name, status, detail, at}) to arms[arm].steps."""
    step = {**step, "at": datetime.now(timezone.utc).isoformat()}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_rebrand_experiments
                   SET arms = jsonb_set(
                         arms, %s::text[],
                         COALESCE(arms -> %s::text -> 'steps', '[]'::jsonb) || %s::jsonb, true),
                       updated_at = now()
                 WHERE id = %s
                """,
                ([arm, "steps"], arm, Jsonb([step]), int(exp_id)),
            )


async def touch(exp_id: int) -> None:
    """Heartbeat: bump updated_at while a run is alive, so a long Veo/Omni wait
    is never mistaken for an orphaned run by another worker process."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE yas_rebrand_experiments SET updated_at = now() WHERE id = %s",
                (int(exp_id),),
            )


async def add_cost(exp_id: int, usd: float) -> None:
    if not usd:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_rebrand_experiments
                   SET cost_usd = COALESCE(cost_usd, 0) + %s, updated_at = now()
                 WHERE id = %s
                """,
                (float(usd), int(exp_id)),
            )


def base_of(exp: dict) -> Optional[dict]:
    """The version an experiment was built on — {version_id, mode} ("stack":
    added on top of it, "rebuild": changed and redone from the original) —
    or None. Lineage lives in the options JSON (options.base), so the table
    needs no column for it."""
    b = (exp.get("options") or {}).get("base")
    if isinstance(b, dict) and b.get("version_id") and b.get("mode") in ("stack", "rebuild"):
        return b
    return None


ORPHAN_ERROR = "The server restarted while this rebrand was running — start a new one."
# A run that has not moved for this long, with no live task in this process,
# is orphaned. One this very server started (options.instance) is known
# sooner: its task is registered right after the insert and a restart takes
# longer than a minute, so a live one is never caught — and a version left
# "being made" by a restart no longer holds the reel's Finalize for 10 min.
ORPHAN_AFTER = timedelta(minutes=10)
ORPHAN_AFTER_HERE = timedelta(seconds=60)


def is_orphaned(exp: dict, *, live_ids: set[int], now: Optional[datetime] = None) -> bool:
    """Whether an active experiment's task is gone (server restart,
    redeploy): not a live task of THIS process, and not moved for
    ORPHAN_AFTER — ORPHAN_AFTER_HERE for one this server started."""
    from .yas_video_editor import INSTANCE_ID

    if exp.get("status") not in ACTIVE_STATUSES or int(exp["id"]) in live_ids:
        return False
    updated = exp.get("updated_at")
    if isinstance(updated, str):
        try:
            updated = datetime.fromisoformat(updated)
        except ValueError:
            return False
    if not isinstance(updated, datetime):
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    here = (exp.get("options") or {}).get("instance") == INSTANCE_ID
    return (now or datetime.now(timezone.utc)) - updated >= (ORPHAN_AFTER_HERE if here else ORPHAN_AFTER)


async def mark_interrupted_if_orphaned(exp: dict, *, live_ids: set[int]) -> dict:
    """An experiment whose background task no longer exists (server restart,
    redeploy) would otherwise spin as "running" forever. If it is orphaned
    (is_orphaned), flip it to `interrupted` so the UI can offer a fresh
    run."""
    if not is_orphaned(exp, live_ids=live_ids):
        return exp
    await update_experiment(int(exp["id"]), status=ST_INTERRUPTED, error=ORPHAN_ERROR)
    return await get_experiment(int(exp["id"])) or exp


# ---------------------------------------------------------------------------
# Self-feedback memory (the RAG loop that feeds on its own results)
# ---------------------------------------------------------------------------

async def store_memory(exp_id: int, embed_text: str) -> None:
    """Embed the experiment's summary so future briefs can retrieve it.
    Best-effort: a failed embed leaves the row un-retrievable, nothing else."""
    text = (embed_text or "").strip()
    if not text:
        return
    try:
        vecs = await gemini_embed([text[:6000]])
        vec = vecs[0] if vecs and vecs[0] else None
    except Exception:  # noqa: BLE001
        log.exception("[rebrand] embed failed for experiment #%d (non-fatal)", exp_id)
        vec = None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_rebrand_experiments
                   SET embed_text = %s, embedding = %s::vector, updated_at = now()
                 WHERE id = %s
                """,
                (text[:6000], vec, int(exp_id)),
            )


_MEMORY_COLS = (
    "id, run_id, status, options, brief, arms, winner, rating, lesson, created_at"
)


async def past_lessons(
    query_text: str, *, exclude_id: int | None = None, k: int = 4,
) -> list[dict]:
    """Past FINISHED experiments to learn from: the k nearest by meaning, plus
    the most recent human-rated ones (a rating is the strongest signal we have,
    so it is surfaced even when the topic is unrelated). Deduplicated, newest
    rated first. Best-effort: [] on any failure."""
    try:
        await ensure_table()
        out: dict[int, dict] = {}
        excl = int(exclude_id) if exclude_id is not None else -1
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT {_MEMORY_COLS} FROM yas_rebrand_experiments
                     WHERE rating IS NOT NULL AND id <> %s
                     ORDER BY updated_at DESC LIMIT 3
                    """,
                    (excl,),
                )
                for r in await cur.fetchall():
                    out[int(r["id"])] = _row(r)
        q = (query_text or "").strip()
        if q and k > 0:
            vecs = await gemini_embed([q[:4000]])
            qvec = vecs[0] if vecs and vecs[0] else None
            if qvec:
                async with get_conn(autocommit=True) as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            f"""
                            SELECT {_MEMORY_COLS} FROM yas_rebrand_experiments
                             WHERE embedding IS NOT NULL AND id <> %s
                               AND status IN ('ready', 'failed')
                             ORDER BY embedding <=> %s::vector
                             LIMIT %s
                            """,
                            (excl, qvec, int(k)),
                        )
                        for r in await cur.fetchall():
                            out.setdefault(int(r["id"]), _row(r))
        rows = list(out.values())
        rows.sort(key=lambda r: (r.get("rating") is not None, int(r["id"])), reverse=True)
        return rows[: k + 3]
    except Exception:  # noqa: BLE001 — memory is best-effort by contract
        log.exception("[rebrand] past_lessons failed (non-fatal)")
        return []


def format_lessons_block(rows: list[dict]) -> str:
    """Compress past experiments into what the next brief should repeat or
    avoid. Human ratings are marked as the authoritative signal; automatic
    critiques are labelled as such."""
    if not rows:
        return ""
    lines = [
        "LESSONS FROM PAST REBRAND EXPERIMENTS (our own results — repeat what "
        "scored well, fix what scored badly; HUMAN ratings outrank auto-critiques):",
    ]
    for r in rows:
        brief = r.get("brief") or {}
        arms = r.get("arms") or {}
        rating = r.get("rating") or {}
        bits: list[str] = []
        for a in ALL_ARMS:
            arm = arms.get(a) or {}
            human = rating.get(f"{a}_stars")
            auto = (arm.get("auto_review") or {}).get("score")
            if human:
                bits.append(f"{a} rated {human}/5 by the team")
            elif auto is not None:
                bits.append(f"{a} auto-scored {auto}/10")
            elif arm.get("status") == ARM_FAILED:
                bits.append(f"{a} failed ({str(arm.get('error') or '')[:80]})")
        winner = r.get("winner")
        target = (r.get("options") or {}).get("target") or "reel"
        head = f"  • Rebrand #{r['id']}" + ("" if target == "reel" else f" ({target} only)")
        if winner:
            head += f" — winner: {winner}"
        if bits:
            head += " — " + "; ".join(bits)
        lines.append(head)
        used = []
        for key, label in (
            ("background", "background"), ("transition_in", "in"),
            ("transition_out", "out"),
        ):
            v = brief.get(key)
            if isinstance(v, dict):
                v = v.get("description") or v.get("style")
            if v:
                used.append(f"{label}: {str(v)[:110]}")
        if used:
            lines.append("      USED — " + " | ".join(used))
        notes = (rating.get("notes") or "").strip()
        if notes:
            lines.append(f"      TEAM SAID: \"{notes[:240]}\"")
        lesson = (r.get("lesson") or "").strip()
        if lesson:
            lines.append(f"      LESSON: {lesson[:320]}")
    return "\n".join(lines) + "\n"
