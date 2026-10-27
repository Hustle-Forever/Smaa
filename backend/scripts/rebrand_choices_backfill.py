"""Fill the rebrand memory (yas_rebrand_choices) for reels finished before
it was collected, and give rows without a search vector one — a DRY RUN
unless --yes.

  python scripts/rebrand_choices_backfill.py            what it would do
  python scripts/rebrand_choices_backfill.py --yes      do it
         [--limit N] [--vectors-only] [--rows-only]

  rows      every talking-head reel finalized by the Rebrand stage whose last
            good render is its frozen plan (composition_good_seq ==
            composition_plan.seq) gets its segments' rows, exactly as a
            ready render writes them (rebrand_memory.snapshot_ready: the
            plan, the version rows, the scene folders on this disk; a
            segment unchanged since its last row is skipped, a seq already
            kept writes nothing)
  vectors   rows with no vector (REBRAND_MEMORY_EMBED off, or an embed that
            failed) get one: their texts embedded in batches, a text
            embedded before reused

The dry run only reads (autocommit SELECTs) and never embeds; --yes writes
and — for vectors, and for new rows with REBRAND_MEMORY_EMBED on — pays
the embed, about $0.0001 a reel. It never asks the icon director, plans
icons or hears words.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RUNS_SQL = ("SELECT id, topic, metadata FROM yas_generations WHERE mode = 'talking_head_v2_preview' "
            "AND metadata ? 'composition_plan' AND metadata ? 'composition_good_seq' ORDER BY id")
NULL_SQL = ("SELECT embed_sha, min(embed_text) AS embed_text FROM yas_rebrand_choices "
            "WHERE embedding IS NULL AND embed_sha IS NOT NULL GROUP BY embed_sha ORDER BY embed_sha LIMIT %s")
SET_SQL = "UPDATE yas_rebrand_choices SET embedding = %s::vector WHERE embed_sha = %s AND embedding IS NULL"
BATCH = 50


def _md(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = {}
    return v if isinstance(v, dict) else {}


async def _runs(limit: Optional[int]) -> list[dict]:
    from app.db import get_conn

    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(RUNS_SQL)
            rows = [dict(r) for r in await cur.fetchall()]
    out = []
    for r in rows:
        md = _md(r.get("metadata"))
        seq = (md.get("composition_plan") or {}).get("seq")
        if seq is not None and md.get("composition_good_seq") == seq:
            out.append({"id": int(r["id"]), "topic": r.get("topic"), "metadata": md, "seq": int(seq)})
    return out[:limit] if limit else out


async def _null_vectors(limit: int) -> list[dict]:
    from app.db import get_conn

    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('public.yas_rebrand_choices') AS t")
            if not (await cur.fetchone() or {}).get("t"):
                return []
            await cur.execute(NULL_SQL, (int(limit),))
            return [dict(r) for r in await cur.fetchall()]


class _ReadOnlyReel:
    """th_reel's store for the reads a snapshot makes, without the one DDL
    its experiments read runs first (rebrand_store.ensure_table): the dry
    run only ever SELECTs."""

    def __init__(self):
        from app.services import th_reel

        self._pg = th_reel.PgStore()

    def __getattr__(self, name):
        return getattr(self._pg, name)

    async def experiments(self, ids):
        from app.db import get_conn
        from app.services import rebrand_store, th_reel

        ids = sorted({int(i) for i in ids if i})
        if not ids:
            return {}
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(th_reel._exps_sql(), (ids,))
                rows = await cur.fetchall()
        return {int(r["id"]): rebrand_store._row(r) for r in rows}


async def rows(yes: bool, limit: Optional[int]) -> dict:
    from app.services import th_reel
    from app.services.rebrand_harness import rebrand_memory as M

    if not yes:
        th_reel.set_store(_ReadOnlyReel())

    store = M.store()
    brand = await store.brand()
    report = {"reels": 0, "new_rows": 0, "written": 0, "by_reel": []}
    for run in await _runs(limit):
        body = await M.rows_for(run, run["seq"], brand=brand)
        last = await store.last_rows(run["id"], [r["target"] for r in body])
        new = [r for r in body if not M._same(r, last.get(r["target"]))
               and (last.get(r["target"]) or {}).get("finalize_seq") != run["seq"]]
        report["reels"] += 1
        report["new_rows"] += len(new)
        entry = {"run_id": run["id"], "seq": run["seq"], "segments": len(body), "new": [r["target"] for r in new]}
        if yes and new:
            entry["written"] = await M.snapshot_ready(run["id"], run["seq"], run["metadata"])
            report["written"] += entry["written"]
        report["by_reel"].append(entry)
    return report


async def vectors(yes: bool, limit: int) -> dict:
    from app.db import get_conn
    from app.services.rebrand_harness import rebrand_memory as M

    todo = await _null_vectors(limit)
    report = {"texts_without_vector": len(todo), "embedded": 0}
    if not yes or not todo:
        return report
    for i in range(0, len(todo), BATCH):
        part = todo[i:i + BATCH]
        got = await M._embed([str(t["embed_text"] or "") for t in part])
        pairs = [(v, t["embed_sha"]) for t, v in zip(part, got) if v]
        if not pairs:
            continue
        # Embedded first, then one short write (no connection held meanwhile).
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(SET_SQL, pairs)
        report["embedded"] += len(pairs)
    return report


async def run(args) -> dict:
    out: dict = {"dry_run": not args.yes}
    if not args.vectors_only:
        out["rows"] = await rows(args.yes, args.limit)
    if not args.rows_only:
        out["vectors"] = await vectors(args.yes, args.limit or 5000)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="rebrand_choices_backfill",
                                 description="Fill the rebrand memory for older reels (dry run unless --yes).")
    ap.add_argument("--yes", action="store_true", help="write (and embed); without it nothing is written")
    ap.add_argument("--limit", type=int, default=None, help="at most N reels (and N texts)")
    ap.add_argument("--vectors-only", action="store_true")
    ap.add_argument("--rows-only", action="store_true")
    args = ap.parse_args(argv)
    # psycopg on Windows needs the selector loop.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        out = runner.run(run(args))
    print(json.dumps(out, indent=1, default=str))
    if not args.yes:
        print("Dry run: nothing was written. Run again with --yes to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
