"""Seed the self-hosted Postgres from the Supabase project, lightly.

Run once on the server, inside the backend image (it already has psycopg), with
the target database given as DB_* (the compose .env) and the source as SRC_*:

    set -a; . /tmp/seed.env; set +a
    docker compose run --rm --no-deps -v "$PWD/db:/seed:ro" \
      -e SRC_DB_HOST -e SRC_DB_PORT -e SRC_DB_NAME -e SRC_DB_USER -e SRC_DB_PASSWORD \
      -e SRC_SUPABASE_URL -e SRC_SUPABASE_SERVICE_ROLE_KEY \
      backend python /seed/seed_from_supabase.py

What it copies:
  * every producer_assets row in the Background (img_bg) and Human Characters
    (img_human_character) groups, plus the people cut from video frames
    (category='character'), with their files;
  * at most ONE row from every other table (the newest settled one), chosen so
    foreign keys still resolve: a row whose required parent was not copied is
    skipped, and an optional reference to an uncopied row is set to NULL.

Rows that would make the server act on their own are left behind: scheduled
Instagram posts, and any row still queued or running.

Media moves off Supabase Storage. Each copied row's bucket object is downloaded
into the shared data volume at data/assets_library/<bucket key> (where the app's
own local-disk fallback writes) and the row is rewritten to that local_path, so
nothing on the server depends on Supabase.

Idempotent: rows go in ON CONFLICT DO NOTHING and downloaded files are kept.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg

# DB local_path values are relative to the backend root (services/storage.py).
BACKEND_ROOT = Path("/app/backend")
ASSETS_DIR = "data/assets_library"

SEED_ASSETS_WHERE = (
    "(metadata->>'content_category' IN ('img_bg', 'img_human_character') "
    "OR category = 'character')"
)
SKIP_TABLES = {"scheduled_instagram_posts"}
ACTIVE_STATUSES = (
    "queued", "pending", "running", "processing", "scheduled", "in_progress",
    "submitted", "rendering", "generating", "claimed", "started",
)
# (bucket column, key column, local column) triples that point at media.
MEDIA_COLUMNS = (
    ("storage_bucket", "storage_path", "local_path"),
    ("video_storage_bucket", "video_storage_path", "video_local_path"),
    ("frame_storage_bucket", "frame_storage_path", "frame_local_path"),
)


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"missing env {name}")
    return value


def connect(prefix: str, sslmode: str, read_only: bool) -> psycopg.Connection:
    return psycopg.connect(
        host=env(f"{prefix}DB_HOST"), port=env(f"{prefix}DB_PORT"),
        dbname=env(f"{prefix}DB_NAME"), user=env(f"{prefix}DB_USER"),
        password=env(f"{prefix}DB_PASSWORD"), sslmode=sslmode,
        options="-c default_transaction_read_only=on" if read_only else "",
        prepare_threshold=None, autocommit=True,
    )


def table_names(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1"""
    ).fetchall()]


def foreign_keys(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT cl.relname, a.attname, pc.relname, pa.attname, NOT a.attnotnull
           FROM pg_constraint con
           JOIN pg_class cl ON cl.oid = con.conrelid
           JOIN pg_class pc ON pc.oid = con.confrelid
           JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
           JOIN pg_attribute pa ON pa.attrelid = con.confrelid AND pa.attnum = con.confkey[1]
           WHERE con.contype = 'f' AND con.connamespace = 'public'::regnamespace
             AND cardinality(con.conkey) = 1"""
    ).fetchall()
    return [dict(child=r[0], col=r[1], parent=r[2], pcol=r[3], nullable=r[4]) for r in rows]


def columns(conn: psycopg.Connection, table: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s", (table,),
    ).fetchall()}


def primary_key(conn: psycopg.Connection, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        """SELECT a.attname FROM pg_index i
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
           WHERE i.indrelid = format('public.%%I', %s::text)::regclass AND i.indisprimary""",
        (table,),
    ).fetchall()]


def topo_order(names: list[str], fks: list[dict]) -> list[str]:
    deps = {t: {f["parent"] for f in fks if f["child"] == t and f["parent"] != t} for t in names}
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(names):
        ready = [t for t in names if t not in done and deps[t] <= done]
        if not ready:  # a cycle: take the rest as they are
            ready = [t for t in names if t not in done]
        for t in ready:
            order.append(t)
            done.add(t)
    return order


def pick_rows(src, table, cols, pk, fks, chosen) -> list[dict]:
    where: list[str] = []
    params: list = []
    for f in fks:
        if f["child"] != table or f["nullable"] or f["parent"] == table:
            continue
        values = chosen.get((f["parent"], f["pcol"]))
        if not values:
            return []  # a required parent was not copied
        where.append(f'"{f["col"]}"::text = ANY(%s::text[])')
        params.append(sorted(values))
    if "status" in cols:
        where.append("(status IS NULL OR status::text <> ALL(%s::text[]))")
        params.append(list(ACTIVE_STATUSES))
    if table == "producer_assets":
        where.append(SEED_ASSETS_WHERE)
    sql = f'SELECT row_to_json(t)::text FROM public."{table}" t'
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = ['"created_at" DESC NULLS LAST'] if "created_at" in cols else []
    order += [f'"{c}" DESC' for c in pk]
    if order:
        sql += " ORDER BY " + ", ".join(order)
    if table != "producer_assets":
        sql += " LIMIT 1"
    rows = [json.loads(r[0]) for r in src.execute(sql, params).fetchall()]

    # Optional references to rows that were not copied become NULL.
    for f in fks:
        if f["child"] != table or not f["nullable"]:
            continue
        if f["parent"] == table:
            keep = {str(r.get(f["pcol"])) for r in rows}
        else:
            keep = chosen.get((f["parent"], f["pcol"]), set())
        for row in rows:
            if row.get(f["col"]) is not None and str(row[f["col"]]) not in keep:
                row[f["col"]] = None
    return rows


def download(base: str, key: str, bucket: str, obj: str, dest: Path) -> tuple[bool, str]:
    if dest.exists() and dest.stat().st_size > 0:
        return True, "kept"
    url = f"{base}/storage/v1/object/{urllib.parse.quote(bucket)}/{urllib.parse.quote(obj)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "apikey": key})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return True, f"{len(data)} bytes"


def reset_sequences(dst: psycopg.Connection) -> None:
    rows = dst.execute(
        """SELECT c.relname, a.attname,
                  pg_get_serial_sequence(format('public.%I', c.relname), a.attname)
           FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
           WHERE n.nspname = 'public' AND c.relkind = 'r'"""
    ).fetchall()
    for table, col, seq in rows:
        if not seq:
            continue
        dst.execute(
            f'SELECT setval(%s, GREATEST(COALESCE((SELECT max("{col}") FROM public."{table}"), 0), 1), '
            f'(SELECT count(*) > 0 FROM public."{table}"))',
            (seq,),
        )


def main() -> None:
    src = connect("SRC_", "require", read_only=True)
    dst = connect("", os.environ.get("DB_SSLMODE", "disable") or "disable", read_only=False)
    base = env("SRC_SUPABASE_URL").rstrip("/")
    key = env("SRC_SUPABASE_SERVICE_ROLE_KEY")

    target = set(table_names(dst))
    names = [t for t in table_names(src) if t in target and t not in SKIP_TABLES]
    fks = [f for f in foreign_keys(src) if f["child"] in names and f["parent"] in names]
    order = topo_order(names, fks)

    chosen: dict[tuple[str, str], set[str]] = {}
    picked: dict[str, tuple[set[str], list[dict]]] = {}
    for table in order:
        cols = columns(src, table)
        rows = pick_rows(src, table, cols, primary_key(src, table), fks, chosen)
        picked[table] = (cols, rows)
        for f in fks:
            if f["parent"] == table:
                chosen[(table, f["pcol"])] = {str(r.get(f["pcol"])) for r in rows}

    jobs = []
    for table, (cols, rows) in picked.items():
        for row in rows:
            for bcol, kcol, lcol in MEDIA_COLUMNS:
                if {bcol, kcol, lcol} <= cols and row.get(bcol) and row.get(kcol):
                    jobs.append((row, bcol, kcol, lcol, f"{ASSETS_DIR}/{row[kcol]}"))
    print(f"downloading {len(jobs)} files from Supabase Storage", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda j: download(base, key, j[0][j[1]], j[0][j[2]], BACKEND_ROOT / j[4]), jobs,
        ))
    failed = 0
    for (row, bcol, kcol, lcol, rel), (ok, info) in zip(jobs, results):
        if ok:
            row[bcol], row[kcol], row[lcol] = None, None, rel
        else:
            failed += 1
            print(f"  download failed {row[bcol]}/{row[kcol]}: {info}", flush=True)

    dst.execute("SET session_replication_role = replica")
    total = 0
    for table in order:
        _, rows = picked[table]
        inserted = 0
        for row in rows:
            cur = dst.execute(
                f'INSERT INTO public."{table}" SELECT * FROM '
                f'json_populate_record(NULL::public."{table}", %s::json) ON CONFLICT DO NOTHING',
                (json.dumps(row),),
            )
            inserted += cur.rowcount
        total += inserted
        if rows:
            print(f"{table}: {inserted}/{len(rows)} rows", flush=True)
    dst.execute("SET session_replication_role = DEFAULT")
    reset_sequences(dst)

    orphans = 0
    for f in fks:
        n = dst.execute(
            f'SELECT count(*) FROM public."{f["child"]}" c WHERE c."{f["col"]}" IS NOT NULL AND NOT EXISTS '
            f'(SELECT 1 FROM public."{f["parent"]}" p WHERE p."{f["pcol"]}" = c."{f["col"]}")'
        ).fetchone()[0]
        if n:
            orphans += n
            print(f"  orphan {f['child']}.{f['col']} -> {f['parent']}: {n}", flush=True)
    print(f"done: {total} rows, {len(jobs) - failed}/{len(jobs)} files, {orphans} orphan references")


if __name__ == "__main__":
    main()
