"""Record, on every Talking Head v2 run, the still each up-to-date clip was
made from (its video_source), for clips made before clips recorded it: a
DRY RUN unless --apply.

  python -m scripts.th_record_clip_sources             what it would do
  python -m scripts.th_record_clip_sources --apply     do it
         [--run ID ...] [--limit N]

A clip records its still when it is made, so an Undo back to that still
keeps it up to date. A clip made before that records nothing, and every
Undo then left it out of date (run 830's clips 351 and 354): the user was
asked to pay for it again. The app now records the still a clip shows just
before that still is replaced; this records it, once, for the clips whose
still has not changed since: every clip that is NOT out of date now and has
no record gets video_source = the scene's current still (main_video_source
for a one-scene run's main clip, metadata.scenes[i].video_source for a
multi-scene run's scene clips). A clip that is out of date, or records a
still already, is left alone, so running it again changes nothing.

The dry run only reads (autocommit SELECTs). --apply writes each run in one
transaction under its row lock, deciding again on the row as locked, and
sets only those keys (updated_at is left alone). No model is called.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODE = "talking_head_v2_preview"  # yas_talking_head_v2.MODE_PREVIEW
RUNS_SQL = ("SELECT id, local_path, storage_path, metadata FROM yas_generations "
            "WHERE mode = %s ORDER BY id")
LOCK_SQL = ("SELECT id, local_path, storage_path, metadata FROM yas_generations "
            "WHERE id = %s AND mode = %s FOR UPDATE")
MAIN_SQL = "UPDATE yas_generations SET metadata = metadata || %s::jsonb WHERE id = %s"
SCENE_SQL = ("UPDATE yas_generations SET metadata = jsonb_set(metadata, %s::text[], to_jsonb(%s::text), true) "
             "WHERE id = %s")


def _conn(**kw):
    from app.db import get_conn

    return get_conn(**kw)


def _md(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = {}
    return v if isinstance(v, dict) else {}


def plan_for(row: dict) -> dict[int, str]:
    """{scene index: the still to record} for one run row: each clip that is
    up to date and records no still gets its scene's current still
    (yas_talking_head_v2._clip_source_to_record with the current still)."""
    from app.services import yas_talking_head_v2 as th

    md = _md(row.get("metadata"))
    run = {"local_path": row.get("local_path"), "storage_path": row.get("storage_path")}
    count = int(md.get("scene_count") or 1)
    out: dict[int, str] = {}
    for i in range(count if count > 1 else 1):
        src = th._clip_source_to_record(md, i, th._still_key(run, md, i))
        if src:
            out[i] = src
    return out


async def _rows(run_ids: Optional[Sequence[int]]) -> list[dict]:
    async with _conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(RUNS_SQL, (MODE,))
            rows = [dict(r) for r in await cur.fetchall()]
    if run_ids:
        wanted = {int(r) for r in run_ids}
        rows = [r for r in rows if int(r["id"]) in wanted]
    return rows


async def _apply_one(run_id: int) -> dict[int, str]:
    """Record one run's missing sources, deciding on the row as locked.
    Returns what was written."""
    async with _conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(LOCK_SQL, (run_id, MODE))
            row = await cur.fetchone()
            if not row:
                return {}
            row = dict(row)
            plan = plan_for(row)
            if not plan:
                return {}
            if int(_md(row.get("metadata")).get("scene_count") or 1) <= 1:
                from psycopg.types.json import Jsonb

                await cur.execute(MAIN_SQL, (Jsonb({"main_video_source": plan[0]}), run_id))
            else:
                for i, src in sorted(plan.items()):
                    await cur.execute(SCENE_SQL, (["scenes", str(i), "video_source"], src, run_id))
    return plan


def _clips(md: dict, plan: dict[int, str]) -> list[dict]:
    multi = int(md.get("scene_count") or 1) > 1
    return [{"clip": f"scene_{i}" if multi else "main", "video_source": src}
            for i, src in sorted(plan.items())]


async def run(apply: bool, run_ids: Optional[Sequence[int]] = None, limit: Optional[int] = None) -> dict:
    rows = await _rows(run_ids)
    report: dict[str, Any] = {"dry_run": not apply, "runs": len(rows), "clips": 0, "written": 0, "by_run": []}
    for row in rows:
        plan = plan_for(row)
        if not plan:
            continue
        if limit is not None and len(report["by_run"]) >= limit:
            break
        rid = int(row["id"])
        entry: dict[str, Any] = {"run_id": rid, "clips": _clips(_md(row.get("metadata")), plan)}
        report["clips"] += len(plan)
        if apply:
            done = await _apply_one(rid)
            entry["written"] = _clips(_md(row.get("metadata")), done)
            report["written"] += len(done)
        report["by_run"].append(entry)
    return report


async def _run_with_pool(apply: bool, run_ids: Optional[Sequence[int]], limit: Optional[int]) -> dict:
    """run() with the app's database pool open: a script has no app startup
    to open it (get_conn would raise "DB pool not initialised")."""
    from app import db

    await db.open_pool()
    try:
        return await run(apply, run_ids, limit)
    finally:
        await db.close_pool()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="th_record_clip_sources",
        description="Record the still each up-to-date Talking Head v2 clip was made from (dry run unless --apply).",
    )
    ap.add_argument("--apply", action="store_true", help="write; without it nothing is written")
    ap.add_argument("--run", type=int, action="append", default=None, metavar="ID",
                    help="only this run (may be given more than once)")
    ap.add_argument("--limit", type=int, default=None, help="at most N runs with clips to record")
    args = ap.parse_args(argv)
    # psycopg on Windows needs the selector loop.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        out = runner.run(_run_with_pool(args.apply, args.run, args.limit))
    print(json.dumps(out, indent=1, default=str))
    if not args.apply:
        print("Dry run: nothing was written. Run again with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
