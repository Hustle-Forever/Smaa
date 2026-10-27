"""What the finished reels shipped, scene by scene — read from the rebrand
memory (yas_rebrand_choices, rebrand_memory), free and read only.

  python scripts/rebrand_choices_report.py [--days N] [--json FILE]
         [--dump FILE] [--as-json]

For intros, main scenes and outros apart:
  segments    how many rows (one per reel segment, per finished reel)
  rebranded   how many shipped a version (the rest shipped the original —
              a skip is data too)
  trimmed     how many ended where the user put the end (not the automatic
              cut), and the median length shipped
  patterns    the patterns chosen most (their slug), and where they sat
  icons       how the shown icons came in: the AI's own, added by the user,
              the team's; the ideas the director placed that were removed,
              and those seen in Suggested and left
  rated       how many rows carry the team's stars, and their mean
and, over all rows, how many have a search vector yet (the rest wait for a
backfill: REBRAND_MEMORY_EMBED off, or an embed that failed).

The rows come from one SELECT in a READ ONLY transaction, on a connection
of its own — never the app's pool; a table that isn't there yet reads as no
rows — or from --json FILE (a list of rows, as --dump writes them). No
model is ever called.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB_TIMEOUT_SEC = 20.0
TOP = 8
KINDS = ("intro", "main", "outro")
COLS = ("id, created_at, run_id, finalize_seq, target, scene_kind, aspect, src, version_id, rebranded, engine, "
        "end_frame, auto_end, user_end, duration_sec, choices, director, rating, brand_sha, "
        "(embedding IS NOT NULL) AS has_vector")


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def summary(rows: Iterable[dict], *, top: int = TOP) -> dict:
    """The report's numbers (module docstring) from choice rows. Pure."""
    rows = [dict(r) for r in rows]
    out: dict = {"rows": len(rows), "runs": len({r.get("run_id") for r in rows}),
                 "with_vector": sum(1 for r in rows if r.get("has_vector") or r.get("embedding")),
                 "brands": len({r.get("brand_sha") for r in rows if r.get("brand_sha")}), "kinds": {}}
    for kind in KINDS:
        rs = [r for r in rows if r.get("scene_kind") == kind]
        if not rs:
            continue
        patterns, places, sources, removed, ignored = Counter(), Counter(), Counter(), Counter(), Counter()
        for r in rs:
            ch = _json(r.get("choices")) or {}
            for p in ch.get("patterns") or []:
                patterns[str(p.get("slug"))] += 1
                places[str((p.get("layout") or {}).get("placement") or "auto")] += 1
            for it in (ch.get("icons") or {}).get("items") or []:
                sources[str(it.get("source"))] += 1
            d = _json(r.get("director")) or {}
            removed.update(d.get("removed") or [])
            ignored.update(d.get("ignored") or [])
        lengths = [float(r["duration_sec"]) for r in rs if r.get("duration_sec")]
        stars = [int(r["rating"]) for r in rs if r.get("rating")]
        out["kinds"][kind] = {
            "segments": len(rs), "rebranded": sum(1 for r in rs if r.get("rebranded")),
            "trimmed": sum(1 for r in rs if r.get("user_end")),
            "median_sec": round(statistics.median(lengths), 2) if lengths else None,
            "patterns": patterns.most_common(top), "placements": places.most_common(top),
            "icon_sources": dict(sources), "removed_most": removed.most_common(top),
            "ignored_most": ignored.most_common(top),
            "rated": len(stars), "stars_mean": round(sum(stars) / len(stars), 2) if stars else None,
        }
    return out


def render(s: dict) -> str:
    out = [f"{s['rows']} segments from {s['runs']} reels ({s['with_vector']} with a search vector, "
           f"{s['brands']} brand(s))"]
    if not s["rows"]:
        out.append("Nothing kept yet: each finished reel adds its segments.")
        return "\n".join(out)
    for kind, k in s["kinds"].items():
        out.append("")
        out.append(f"{kind}: {k['segments']} segments, {k['rebranded']} rebranded, {k['trimmed']} with the "
                   f"user's end, median {k['median_sec']} s; rated {k['rated']}"
                   + (f" (mean {k['stars_mean']} stars)" if k["stars_mean"] is not None else ""))
        for title, pairs in (("patterns", k["patterns"]), ("placed", k["placements"]),
                             ("removed most", k["removed_most"]), ("seen and left", k["ignored_most"])):
            if pairs:
                out.append(f"  {title}: " + ", ".join(f"{name} {n}" for name, n in pairs))
        if k["icon_sources"]:
            out.append("  icons shown: " + ", ".join(f"{src} {n}" for src, n in sorted(k["icon_sources"].items())))
    return "\n".join(out)


async def read_rows(days: Optional[float] = None) -> list[dict]:
    """The table's rows (oldest first): one SELECT in a READ ONLY
    transaction on a connection of its own. [] while the table isn't there."""
    import psycopg
    from psycopg.rows import dict_row

    from app.db import _dsn

    async def go() -> list[dict]:
        async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=False, prepare_threshold=None,
                                                         row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute("SELECT to_regclass('public.yas_rebrand_choices') AS t")
                rows: list[dict] = []
                if (await cur.fetchone() or {}).get("t"):
                    if days:
                        await cur.execute(f"SELECT {COLS} FROM yas_rebrand_choices "
                                          "WHERE created_at > now() - make_interval(secs => %s) ORDER BY id",
                                          (float(days) * 86400.0,))
                    else:
                        await cur.execute(f"SELECT {COLS} FROM yas_rebrand_choices ORDER BY id")
                    rows = [dict(r) for r in await cur.fetchall()]
            await conn.rollback()
        return rows

    return await asyncio.wait_for(go(), DB_TIMEOUT_SEC)


def _plain(rows: list[dict]) -> list[dict]:
    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="rebrand_choices_report",
                                 description="What the finished reels shipped (free, read only).")
    ap.add_argument("--days", type=float, default=None, help="only the last N days")
    ap.add_argument("--json", type=Path, default=None, help="read the rows from this file, not the database")
    ap.add_argument("--dump", type=Path, default=None, help="also write the rows read to this file")
    ap.add_argument("--as-json", action="store_true", help="print the numbers as JSON")
    args = ap.parse_args(argv)
    if args.json is not None:
        rows = json.loads(args.json.read_text(encoding="utf-8"))
    else:
        # psycopg on Windows needs the selector loop.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            rows = runner.run(read_rows(args.days))
    rows = _plain(list(rows))
    if args.dump is not None:
        args.dump.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    s = summary(rows)
    print(json.dumps(s, indent=1, default=str) if args.as_json else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
