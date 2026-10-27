"""How the icon director's picks fare with the user — read from the log each
Generate writes (icon_feedback), free and read only.

  python -m app.services.rebrand_harness.icon_director_report [--days N]
         [--json FILE] [--dump FILE]

For each director version and model:
  picks      the AI's own picks (placed on its own) that reached a Generate
  accepted   shipped untouched (shown_ai) or kept on purpose (kept)
  removed    taken off by the user
  rate       accepted / (accepted + removed) — a pick with no room this time
             (dropped) was never judged, so it is left out
  added      what the user added: back from a refusal, a suggestion, the
             library, the team's icons
and, over all rows, the concepts removed most and the ones added back most
(a refusal or a suggestion the user wanted anyway) — what later becomes the
director's "house taste" examples.

The rows come from yas_rebrand_icon_feedback (one SELECT in a READ ONLY
transaction, on a connection of its own — never the app's pool; a table that
isn't there yet reads as no rows), or from --json FILE (a list of rows, as
--dump writes them). No model is ever called.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .icon_feedback import OUTCOMES

DB_TIMEOUT_SEC = 20.0
TOP = 10
ADDED = ("added_refused", "added_suggested", "added_library", "added_team")


def _concept(r: dict) -> str:
    return str(r.get("concept") or r.get("label") or r.get("glyph") or "?").strip().lower()


def summary(rows: Iterable[dict], *, top: int = TOP) -> dict:
    """The report's numbers (module docstring) from feedback rows:
    {rows, versions: [{director_version, model, picks, accepted, removed,
    dropped, rate, added: {outcome: n}}], removed_most: [(concept, n)],
    added_back_most: [(concept, n)], team_added: [(label, n)], outcomes:
    {outcome: n}}. Pure."""
    rows = [r for r in rows if r.get("outcome") in OUTCOMES]
    groups: dict[tuple, Counter] = {}
    for r in rows:
        key = (r.get("director_version"), r.get("model"))
        groups.setdefault(key, Counter())[r["outcome"]] += 1
    versions = []
    for (version, model), c in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        accepted = c["shown_ai"] + c["kept"]
        judged = accepted + c["removed"]
        versions.append({
            "director_version": version, "model": model,
            "picks": accepted + c["removed"] + c["dropped"], "accepted": accepted, "kept": c["kept"],
            "removed": c["removed"], "dropped": c["dropped"],
            "rate": round(accepted / judged, 3) if judged else None,
            "added": {o: c[o] for o in ADDED if c[o]},
        })
    removed = Counter(_concept(r) for r in rows if r["outcome"] == "removed")
    back = Counter(_concept(r) for r in rows if r["outcome"] in ("added_refused", "added_suggested"))
    team = Counter(str(r.get("label") or r.get("glyph")) for r in rows if r["outcome"] == "added_team")
    return {
        "rows": len(rows), "versions": versions,
        "removed_most": removed.most_common(top), "added_back_most": back.most_common(top),
        "team_added": team.most_common(top),
        "outcomes": {o: n for o, n in Counter(r["outcome"] for r in rows).items()},
    }


def render(s: dict) -> str:
    """The summary as plain text."""
    out = [f"{s['rows']} icon rows"]
    if not s["rows"]:
        out.append("Nothing logged yet: each Generate with icons adds its rows.")
        return "\n".join(out)
    out.append("")
    out.append("By director version and model:")
    for v in s["versions"]:
        rate = "—" if v["rate"] is None else f"{round(100 * v['rate'])} %"
        out.append(f"  v{v['director_version']} {v['model'] or '(no AI)'}: {v['picks']} picks, "
                   f"{v['accepted']} accepted ({v['kept']} kept on purpose), {v['removed']} removed, "
                   f"{v['dropped']} with no room — acceptance {rate}")
        if v["added"]:
            out.append("      added by the user: " + ", ".join(f"{k.removeprefix('added_')} {n}"
                                                             for k, n in v["added"].items()))

    def top(title: str, pairs: Sequence[tuple[str, int]]) -> None:
        if pairs:
            out.append("")
            out.append(title)
            out.extend(f"  {n:>3}  {name}" for name, n in pairs)

    top("Removed most:", s["removed_most"])
    top("Added back most (refused or suggested, wanted anyway):", s["added_back_most"])
    top("Team icons added most:", s["team_added"])
    return "\n".join(out)


async def read_rows(days: Optional[float] = None) -> list[dict]:
    """The log's rows (newest last): one SELECT in a READ ONLY transaction on
    a connection of its own. [] while the table isn't there yet."""
    import psycopg
    from psycopg.rows import dict_row

    from ...db import _dsn

    cols = ("created_at, run_id, target, version_id, director_key, director_version, model, source, glyph, "
            "label, concept, quote, said_at, ai_verdict, ai_priority, ai_auto, outcome, shown")

    async def go() -> list[dict]:
        async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=False, prepare_threshold=None,
                                                         row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute("SELECT to_regclass('public.yas_rebrand_icon_feedback') AS t")
                there = (await cur.fetchone() or {}).get("t")
                rows: list[dict] = []
                if there:
                    if days:
                        await cur.execute(f"SELECT {cols} FROM yas_rebrand_icon_feedback "
                                          "WHERE created_at > now() - make_interval(secs => %s) ORDER BY id",
                                          (float(days) * 86400.0,))
                    else:
                        await cur.execute(f"SELECT {cols} FROM yas_rebrand_icon_feedback ORDER BY id")
                    rows = [dict(r) for r in await cur.fetchall()]
            await conn.rollback()
        return rows

    return await asyncio.wait_for(go(), DB_TIMEOUT_SEC)


def _plain(rows: list[dict]) -> list[dict]:
    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="icon_director_report",
                                 description="How the icon director's picks fare (free, read only).")
    ap.add_argument("--days", type=float, default=None, help="only the last N days")
    ap.add_argument("--json", type=Path, default=None, help="read the rows from this file, not the database")
    ap.add_argument("--dump", type=Path, default=None, help="also write the rows read to this file")
    ap.add_argument("--as-json", action="store_true", help="print the numbers as JSON")
    args = ap.parse_args(argv)
    if args.json is not None:
        rows = json.loads(args.json.read_text(encoding="utf-8"))
    else:
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
