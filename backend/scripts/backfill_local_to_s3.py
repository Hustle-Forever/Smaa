"""Copy local-only media rows into the S3 store (MinIO), one row at a time.

Run inside the backend container, whose WORKDIR is /app/backend:

    docker compose exec backend python -m scripts.backfill_local_to_s3            # dry run
    docker compose exec backend python -m scripts.backfill_local_to_s3 --apply
    docker compose exec backend python -m scripts.backfill_local_to_s3 --table producer_assets --limit 5

The dry run (the default) only reads: it prints a per-table plan with totals,
the keys it had to sanitise, and every row whose file is missing. --apply needs
the S3 backend (STORAGE_BACKEND=s3, or S3_ENDPOINT set).

Row-driven over the 8 (bucket, key, local) triples. For each row that has a
local file and no bucket yet (D14, A15):
  - the file goes to the bucket its live writer uses, under the key that writer
    would have used, through asset_storage.put_s3_object only (no local-disk
    fallback, so a failure writes nothing anywhere);
  - producer_assets images also get their 480px WebP at thumbs/<key>.webp, and
    font rows their preview PNG, BEFORE the row points at the bucket;
  - the row is then updated in its own transaction, and only if it still has
    no bucket and the same local path. local_path is kept, so Remotion and the
    local-first readers carry on unchanged and rolling back is trivial.

Idempotent: rows that already have a bucket are skipped, so a re-run after a
failure only does what is left. Files that are missing are reported and their
rows left alone. Nothing else is touched: no directory mirroring, no jsonb
pointer except producer_assets attributes._thumb_path / _preview_path, and no
brand_documents row (that table has no local_path column).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# psycopg async refuses the Windows Proactor loop; same fix as run.py.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from psycopg.types.json import Jsonb  # noqa: E402

from app.config import config  # noqa: E402
from app.db import close_pool, get_conn, open_pool  # noqa: E402
from app.services import asset_storage, s3_backend, thumbs  # noqa: E402
from app.services.asset_storage import AssetStorageError  # noqa: E402


# ---------------------------------------------------------------------------
# What to copy, and where
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Triple:
    """One (bucket, key, local) column triple of one table."""
    name: str            # what --table and the report call it
    table: str
    bucket_col: str
    key_col: str
    local_col: str
    bucket_setting: str  # Config field that names the destination bucket
    # How the Content-Type is chosen: "file" sniffs the file and falls back to
    # the row's mime_type; "frame" sniffs the file only, because frame rows
    # carry the video's mime_type; "video" is always video/mp4 (Veo output).
    media: str

    @property
    def bucket(self) -> str:
        return getattr(config, self.bucket_setting)


# Buckets as the live writers choose them (the code is the source of truth):
# producer-media for yas_producer.py poster/animation/general-video uploads,
# poster_pipeline.py, yas_local_animation_poller.py and yas_general_video_poller.py;
# the default producer-assets for producer_assets, yas_generations (yas_producer
# and the Talking Head _store_image_bytes) and yas_animations_poller.py.
TRIPLES: tuple[Triple, ...] = (
    Triple("producer_assets", "producer_assets",
           "storage_bucket", "storage_path", "local_path", "producer_assets_bucket", "file"),
    Triple("yas_generations", "yas_generations",
           "storage_bucket", "storage_path", "local_path", "producer_assets_bucket", "file"),
    Triple("yas_poster_generations", "yas_poster_generations",
           "storage_bucket", "storage_path", "local_path", "producer_media_bucket", "file"),
    Triple("yas_animations", "yas_animations",
           "storage_bucket", "storage_path", "local_path", "producer_assets_bucket", "video"),
    Triple("yas_animation_generations.frame", "yas_animation_generations",
           "frame_storage_bucket", "frame_storage_path", "frame_local_path",
           "producer_media_bucket", "frame"),
    Triple("yas_animation_generations.video", "yas_animation_generations",
           "video_storage_bucket", "video_storage_path", "video_local_path",
           "producer_media_bucket", "video"),
    Triple("yas_general_video_generations.frame", "yas_general_video_generations",
           "frame_storage_bucket", "frame_storage_path", "frame_local_path",
           "producer_media_bucket", "frame"),
    Triple("yas_general_video_generations.video", "yas_general_video_generations",
           "video_storage_bucket", "video_storage_path", "video_local_path",
           "producer_media_bucket", "video"),
)


def table_choices() -> list[str]:
    return sorted({t.name for t in TRIPLES} | {t.table for t in TRIPLES})


def select_triples(name: str | None) -> list[Triple]:
    """All triples, or those of one table ("yas_animation_generations" picks
    both of its triples, "yas_animation_generations.frame" just the one)."""
    if not name:
        return list(TRIPLES)
    picked = [t for t in TRIPLES if name in (t.name, t.table)]
    if not picked:
        raise ValueError(f"unknown table {name!r}; choose from {', '.join(table_choices())}")
    return picked


# ---------------------------------------------------------------------------
# Paths and keys (pure apart from the disk checks)
# ---------------------------------------------------------------------------

# Under backend/data: where the yas_generations writers keep their local copy.
YAS_PRODUCER_DIR = "yas_producer"


def _backend_root() -> Path:
    # The base asset_storage._local_to_db_path stores paths against.
    return config.producer_assets_root.parent.parent


def _data_root() -> Path:
    # The other base: backend/data (services/storage.py, the Talking Head writers).
    return config.asset_root.parent


def _library_dir() -> str:
    """Data-relative home of asset_storage's local fallback. It writes
    producer_assets_root/<key>, so a path below it IS the bucket key."""
    try:
        return config.producer_assets_root.relative_to(_data_root()).as_posix()
    except ValueError:
        return "assets_library"


def data_relative(local_path: str) -> str | None:
    """`local_path` as a POSIX path relative to backend/data, whichever base it
    was stored against (backend/ or backend/data, or absolute). None for an
    absolute path outside both."""
    p = local_path.strip().replace("\\", "/")
    # A leading '/' is absolute on the server even when this runs on Windows.
    if Path(p).is_absolute() or p.startswith("/"):
        for root in (_data_root(), _backend_root()):
            try:
                p = Path(p).relative_to(root).as_posix()
                break
            except ValueError:
                continue
        else:
            return None
    p = p.lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("data/"):
        p = p[len("data/"):]
    return p


def derive_key(local_path: str) -> tuple[str, str]:
    """(key, rule): the bucket key the live writer would have used for this
    file, before the A4 check. The rules:
      "assets_library": data/assets_library/<key>, the upload() fallback, whose
          layout is the key (producer_assets, posters, animations, general video);
      "yas_producer": yas_producer/<rest> -> yas/<rest>, e.g.
          yas_producer/generations/12.png -> yas/generations/12.png and
          yas_producer/talking-head-v2/7/main.png -> yas/talking-head-v2/7/main.png;
      "as-is": anything else keeps its data-relative path.
    The key is "" (rule "outside") for an absolute path outside the data volume."""
    rel = data_relative(local_path)
    if rel is None:
        return "", "outside"
    lib = _library_dir() + "/"
    if rel.startswith(lib):
        return rel[len(lib):], "assets_library"
    if rel.startswith(YAS_PRODUCER_DIR + "/"):
        return "yas/" + rel[len(YAS_PRODUCER_DIR) + 1:], "yas_producer"
    return rel, "as-is"


def clean_key(raw: str) -> str:
    """`raw` if it is a valid key (A4), else its sanitised form; "" when
    nothing usable is left."""
    key = raw.lstrip("/")
    if s3_backend.valid_key(key):
        return key
    return s3_backend.sanitize_key(key)


def resolve_local(local_path: str) -> Path | None:
    """The file on disk, trying backend/ then backend/data exactly like
    asset_storage.local_to_abs. None when it is on neither."""
    p = asset_storage.local_to_abs(local_path.strip().replace("\\", "/"))
    return p.resolve() if p.is_file() else None


def resolve_preview(preview_path: str) -> Path | None:
    """A font preview's file. `_preview_path` is a bucket key, and upload()'s
    local fallback put the file at producer_assets_root/<key>; older rows may
    hold a plain local path instead, so try that too."""
    rel = preview_path.strip().replace("\\", "/").lstrip("/")
    for candidate in (config.producer_assets_root / rel, asset_storage.local_to_abs(rel)):
        if candidate.is_file():
            return candidate.resolve()
    return None


def inside_data(path: Path) -> bool:
    """A stored path must not lead out of the backend tree (a stray '..' or a
    symlink); never upload what the app did not write."""
    for root in (_backend_root(), _data_root()):
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Content-Type from the file itself (A15)
# ---------------------------------------------------------------------------

SNIFF_BYTES = 1024
_FTYP_BRANDS = {
    b"qt  ": "video/quicktime", b"M4A ": "audio/mp4", b"M4B ": "audio/mp4",
    b"heic": "image/heic", b"heix": "image/heic", b"mif1": "image/heif",
    b"avif": "image/avif", b"avis": "image/avif",
}
_FONT_MAGIC = {
    b"wOFF": "font/woff", b"wOF2": "font/woff2", b"OTTO": "font/otf",
    b"\x00\x01\x00\x00": "font/ttf", b"true": "font/ttf",
}
# Row mime values that say nothing about the bytes.
_VAGUE_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


def sniff_content_type(head: bytes) -> str | None:
    """Content-Type from a file's first bytes, or None when unrecognised."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF":
        return {b"WEBP": "image/webp", b"WAVE": "audio/wav"}.get(head[8:12])
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12], "video/mp4")
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head[:4] in _FONT_MAGIC:
        return _FONT_MAGIC[head[:4]]
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in text):
        return "image/svg+xml"
    return None


def content_type_for(triple: Triple, head: bytes, name: str, row_mime: str | None) -> str:
    """The type to store. Never the row's mime_type for a frame triple: those
    rows describe the finished MP4, and a PNG labelled video/mp4 would not
    render (MinIO sends nosniff)."""
    if triple.media == "video":
        return "video/mp4"
    sniffed = sniff_content_type(head)
    if sniffed:
        return sniffed
    if triple.media == "file":
        mime = (row_mime or "").split(";")[0].strip().lower()
        if mime not in _VAGUE_TYPES:
            return mime
    return s3_backend.content_type_for(name, None)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

PROBLEMS = {
    "missing": "file missing on disk",
    "outside": "path outside the data volume",
    "unreadable": "file unreadable",
    "bad_key": "no usable key",
    "conflict": "key taken by another file",
}


@dataclass
class Item:
    """One row's planned copy."""
    triple: Triple
    row_id: int
    local_path: str                   # exactly as stored: the UPDATE matches on it
    abs_path: Path | None = None
    raw_key: str = ""
    key: str = ""
    rule: str = ""
    size: int = 0
    content_type: str = ""
    asset_kind: str | None = None
    attributes: dict = field(default_factory=dict)
    thumb: bool = False               # write thumbs/<key>.webp (producer_assets images)
    preview_path: str = ""            # font rows: attributes._preview_path
    preview_abs: Path | None = None
    preview_key: str = ""
    problem: str = ""                 # a PROBLEMS key; "" means it will be copied

    @property
    def sanitised(self) -> bool:
        return bool(self.key) and self.key != self.raw_key


@dataclass
class TablePlan:
    triple: Triple
    items: list[Item]
    in_bucket: int = 0      # already has a bucket: skipped (idempotent re-run)
    no_local: int = 0       # no bucket and no local path: nothing to copy
    candidates: int = 0     # rows with a local path and no bucket (before --limit)

    @property
    def todo(self) -> list[Item]:
        return [i for i in self.items if not i.problem]


def plan_row(triple: Triple, row: dict) -> Item:
    """Resolve one row: its file, key, Content-Type and extras. Reads only the
    first bytes of the file."""
    attrs = row.get("attributes")
    item = Item(
        triple=triple,
        row_id=int(row["id"]),
        local_path=row["local_path"],
        asset_kind=row.get("asset_kind"),
        attributes=dict(attrs) if isinstance(attrs, dict) else {},
    )
    item.raw_key, item.rule = derive_key(item.local_path)
    item.key = clean_key(item.raw_key)
    abs_path = resolve_local(item.local_path)
    if abs_path is None:
        item.problem = "missing"
        return item
    item.abs_path = abs_path
    if item.rule == "outside" or not inside_data(abs_path):
        item.problem = "outside"
        return item
    if not item.key:
        item.problem = "bad_key"
        return item
    try:
        item.size = abs_path.stat().st_size
        with abs_path.open("rb") as f:
            head = f.read(SNIFF_BYTES)
    except OSError:
        item.problem = "unreadable"
        return item
    item.content_type = content_type_for(triple, head, abs_path.name, row.get("mime_type"))
    if triple.table == "producer_assets":
        # Same rule as the upload hook: image rows only, raster types only.
        item.thumb = item.asset_kind == "image" and asset_storage.can_transform(item.content_type)
        preview = item.attributes.get("_preview_path")
        if item.asset_kind == "font" and isinstance(preview, str) and preview.strip():
            item.preview_path = preview
            item.preview_key = clean_key(derive_key(preview)[0])
            item.preview_abs = resolve_preview(preview)
    return item


def plan_table(triple: Triple, rows: list[dict], *, in_bucket: int = 0,
               no_local: int = 0, candidates: int | None = None) -> TablePlan:
    return TablePlan(
        triple=triple,
        items=[plan_row(triple, r) for r in rows],
        in_bucket=in_bucket,
        no_local=no_local,
        candidates=len(rows) if candidates is None else candidates,
    )


def mark_conflicts(plans: list[TablePlan]) -> None:
    """Two different files must never share a (bucket, key): the second upload
    would replace the first row's media. The first claimant keeps the key and
    the others are reported. Rows that point at the same file may share it."""
    owner: dict[tuple[str, str], Path] = {}
    for plan in plans:
        for item in plan.items:
            if item.problem:
                continue
            first = owner.setdefault((plan.triple.bucket, item.key), item.abs_path)
            if first != item.abs_path:
                item.problem = "conflict"


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def format_plan(plans: list[TablePlan], *, limit: int | None = None) -> str:
    lines: list[str] = []
    total_rows = total_bytes = total_thumbs = total_skipped = total_in_bucket = 0
    for plan in plans:
        t = plan.triple
        todo = plan.todo
        size = sum(i.size for i in todo)
        n_thumbs = sum(1 for i in todo if i.thumb)
        lines.append(f"{t.name} -> {t.bucket}")
        upload = f"  upload {len(todo)} row(s), {_mb(size)}"
        if t.table == "producer_assets":
            upload += f", {n_thumbs} thumbnail(s)"
        lines.append(upload)
        if limit and plan.candidates > len(plan.items):
            lines.append(f"  (--limit {limit}: {len(plan.items)} of {plan.candidates} rows looked at)")
        problems = Counter(i.problem for i in plan.items if i.problem)
        for code, label in PROBLEMS.items():
            if problems.get(code):
                lines.append(f"  not copied, {label}: {problems[code]}")
        lines.append(
            f"  already in a bucket: {plan.in_bucket}; no local file recorded: {plan.no_local}"
        )
        types = Counter(i.content_type for i in todo)
        if types:
            lines.append("  content types: " + ", ".join(
                f"{ct} {n}" for ct, n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))
            ))
        sanitised = [i for i in todo if i.sanitised]
        if sanitised:
            lines.append("  keys sanitised:")
            lines += [f"    id {i.row_id}: {i.raw_key!r} -> {i.key!r}" for i in sanitised]
        # Not under a writer's layout, so no writer would have picked this key.
        as_is = [i for i in todo if i.rule == "as-is"]
        if as_is:
            lines.append("  keys kept as the data path (no writer layout matched):")
            lines += [f"    id {i.row_id}: {i.local_path} -> {i.key}" for i in as_is]
        skipped = [i for i in plan.items if i.problem]
        if skipped:
            lines.append("  rows left alone:")
            lines += [
                f"    id {i.row_id} ({PROBLEMS[i.problem]}): {i.local_path}" for i in skipped
            ]
        previews = [i for i in todo if i.preview_path and not i.preview_abs]
        if previews:
            lines.append("  font previews missing (the font itself is still copied):")
            lines += [f"    id {i.row_id}: {i.preview_path}" for i in previews]
        total_rows += len(todo)
        total_bytes += size
        total_thumbs += n_thumbs
        total_skipped += len(skipped)
        total_in_bucket += plan.in_bucket
    lines.append(
        f"TOTAL: {total_rows} row(s) to upload ({_mb(total_bytes)}), "
        f"{total_thumbs} thumbnail(s); {total_skipped} row(s) left alone; "
        f"{total_in_bucket} already in a bucket"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Applying it
# ---------------------------------------------------------------------------

async def _write_thumbnail(item: Item) -> str | None:
    raw = await asyncio.to_thread(item.abs_path.read_bytes)
    data = await asyncio.to_thread(thumbs.make_thumb_bytes, raw)
    if not data:
        return None
    key = thumbs.thumb_object_path(item.key)
    await asset_storage.put_s3_object(item.triple.bucket, key, data=data, mime="image/webp")
    return key


async def copy_item(item: Item, done: set[tuple[str, str]]) -> tuple[dict, list[str], list[str]]:
    """Upload the row's file, then its thumbnail / font preview. Returns
    (attributes to set, attribute keys to drop, notes). Raises
    AssetStorageError when the file itself could not be written."""
    bucket = item.triple.bucket
    if (bucket, item.key) not in done:
        await asset_storage.put_s3_object(
            bucket, item.key, path=item.abs_path, mime=item.content_type,
        )
        done.add((bucket, item.key))
    patch: dict = {}
    drop: list[str] = []
    notes: list[str] = []
    if item.triple.table != "producer_assets":
        return patch, drop, notes

    # Always recomputed: seeded rows carry Supabase-era _thumb_path values whose
    # objects exist nowhere, and a grid would sign them into 404s.
    thumb_key = None
    if item.thumb:
        try:
            thumb_key = await _write_thumbnail(item)
            if not thumb_key:
                notes.append("thumbnail skipped: not a decodable image")
        except (AssetStorageError, OSError) as e:
            notes.append(f"thumbnail failed: {e}")
    if thumb_key:
        patch["_thumb_path"] = thumb_key
    else:
        drop.append("_thumb_path")

    if item.preview_path and item.preview_abs and not item.preview_key:
        notes.append(f"font preview skipped: no usable key for {item.preview_path!r}")
    elif item.preview_path and item.preview_abs:
        try:
            await asset_storage.put_s3_object(
                bucket, item.preview_key, path=item.preview_abs, mime="image/png",
            )
            if item.preview_key != item.preview_path:
                patch["_preview_path"] = item.preview_key
        except AssetStorageError as e:
            notes.append(f"font preview failed: {e}")
    return patch, drop, notes


def build_update(item: Item, patch: dict, drop: list[str]) -> tuple[str, tuple]:
    """Conditional UPDATE: only a row that still has no bucket and the same
    local path is moved, so a row a live writer changed meanwhile is never
    overwritten. local_path is left as it is, and so is updated_at: the bytes
    did not change, only where a second copy lives."""
    t = item.triple
    where = f" WHERE id = %s AND {t.bucket_col} IS NULL AND {t.local_col} = %s"
    if t.table == "producer_assets":
        sql = (
            f"UPDATE {t.table} SET {t.bucket_col} = %s, {t.key_col} = %s, "
            "attributes = (attributes - %s::text[]) || %s::jsonb" + where
        )
        return sql, (t.bucket, item.key, drop, Jsonb(patch), item.row_id, item.local_path)
    sql = f"UPDATE {t.table} SET {t.bucket_col} = %s, {t.key_col} = %s" + where
    return sql, (t.bucket, item.key, item.row_id, item.local_path)


async def update_row(item: Item, patch: dict, drop: list[str]) -> int:
    """Run the UPDATE in its own transaction; returns the rows it changed."""
    sql, params = build_update(item, patch, drop)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount


@dataclass
class Summary:
    updated: int = 0
    changed: int = 0      # the row moved on meanwhile; reported, not retried
    failed: int = 0
    thumbs: int = 0
    notes: int = 0
    stopped: bool = False


async def apply_plans(plans: list[TablePlan]) -> Summary:
    summary = Summary()
    done: set[tuple[str, str]] = set()
    for plan in plans:
        for item in plan.todo:
            label = f"{item.triple.name} id {item.row_id} -> {item.triple.bucket}/{item.key}"
            try:
                patch, drop, notes = await copy_item(item, done)
                changed = await update_row(item, patch, drop)
            except Exception as e:  # noqa: BLE001 - one bad row must not end the run
                summary.failed += 1
                print(f"FAILED {label}: {e}", flush=True)
                if s3_backend.breaker_open():
                    # Every further put would fail fast the same way.
                    summary.stopped = True
                    print("S3 storage is unreachable; stopping. Re-run later: "
                          "finished rows are skipped.", flush=True)
                    return summary
                continue
            summary.notes += len(notes)
            summary.thumbs += 1 if "_thumb_path" in patch else 0
            suffix = "".join(f" [{n}]" for n in notes)
            if changed:
                summary.updated += 1
                print(f"ok {label}{suffix}", flush=True)
            else:
                summary.changed += 1
                print(f"CHANGED {label}: the row was modified meanwhile; "
                      f"left as it is (the object stays in the bucket){suffix}", flush=True)
    return summary


# ---------------------------------------------------------------------------
# Database reads
# ---------------------------------------------------------------------------

async def fetch_rows(conn, triple: Triple, limit: int | None) -> list[dict]:
    t = triple
    extra = ", asset_kind, attributes" if t.table == "producer_assets" else ""
    sql = (
        f"SELECT id, {t.local_col} AS local_path, mime_type{extra} FROM {t.table} "
        f"WHERE {t.bucket_col} IS NULL AND {t.local_col} IS NOT NULL AND {t.local_col} <> '' "
        "ORDER BY id"
    )
    params: tuple = ()
    if limit:
        sql += " LIMIT %s"
        params = (int(limit),)
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return list(await cur.fetchall())


async def fetch_counts(conn, triple: Triple) -> dict:
    t = triple
    has_local = f"{t.local_col} IS NOT NULL AND {t.local_col} <> ''"
    sql = (
        f"SELECT count(*) FILTER (WHERE {t.bucket_col} IS NOT NULL) AS in_bucket, "
        f"count(*) FILTER (WHERE {t.bucket_col} IS NULL AND NOT ({has_local})) AS no_local, "
        f"count(*) FILTER (WHERE {t.bucket_col} IS NULL AND {has_local}) AS candidates "
        f"FROM {t.table}"
    )
    async with conn.cursor() as cur:
        await cur.execute(sql)
        row = await cur.fetchone()
    return {k: int(row.get(k) or 0) for k in ("in_bucket", "no_local", "candidates")}


async def build_plans(triples: list[Triple], limit: int | None) -> list[TablePlan]:
    plans = []
    async with get_conn(autocommit=True) as conn:
        for t in triples:
            rows = await fetch_rows(conn, t, limit)
            counts = await fetch_counts(conn, t)
            plans.append(plan_table(t, rows, **counts))
    mark_conflicts(plans)
    return plans


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m scripts.backfill_local_to_s3",
        description="Copy local-only media rows into the S3 store. Dry run unless --apply.",
    )
    ap.add_argument("--apply", action="store_true",
                    help="upload the files and update the rows (default: print the plan only)")
    ap.add_argument("--table", choices=table_choices(), metavar="TABLE",
                    help="only this table; a table name covers both of its frame/video "
                         "triples. One of: " + ", ".join(table_choices()))
    ap.add_argument("--limit", type=int, metavar="N",
                    help="look at no more than N rows per triple")
    args = ap.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be at least 1")
    return args


async def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    triples = select_triples(args.table)
    backend = asset_storage.backend_name()
    if args.apply and backend != "s3":
        print(f"--apply needs the S3 backend (STORAGE_BACKEND=s3 or S3_ENDPOINT set); "
              f"this process resolves to {backend!r}. Nothing was written.")
        return 2
    mode = "APPLY" if args.apply else "DRY RUN (nothing is written)"
    print(f"Backfill local media into S3: {mode}")
    print(f"storage backend: {backend}" + (f", endpoint {config.s3_endpoint}" if backend == "s3" else ""))

    await open_pool()
    try:
        plans = await build_plans(triples, args.limit)
        print(format_plan(plans, limit=args.limit), flush=True)
        if not args.apply:
            print("\nDry run: nothing was written. Re-run with --apply to copy these rows.")
            return 0
        buckets = sorted({p.triple.bucket for p in plans if p.todo})
        for bucket in buckets:
            try:
                await asset_storage.ensure_bucket(bucket)
            except AssetStorageError as e:
                print(f"Cannot use bucket {bucket}: {e}. Nothing was written.")
                return 1
        print("")
        summary = await apply_plans(plans)
    finally:
        await close_pool()
    print(
        f"\nDone: {summary.updated} row(s) updated, {summary.thumbs} thumbnail(s), "
        f"{summary.changed} changed meanwhile, {summary.failed} failed, "
        f"{summary.notes} note(s)" + (" (stopped early)" if summary.stopped else "")
    )
    return 0 if not (summary.failed or summary.stopped) else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
