"""One-shot bulk import of a local brand-asset folder into producer_assets.

Reuses the existing upload pipeline (`_process_asset_bytes` from
app.routers.producer_assets) so every file gets the full treatment:
Gemini analysis -> 768d embedding -> Supabase Storage (local fallback) ->
producer_assets row.

Usage (from anywhere; script dir is auto-added to sys.path):

    backend\\.venv\\Scripts\\python.exe backend\\import_brand_assets.py [folder] [--dry-run]

- `folder` defaults to C:\\Users\\LTSoft\\Desktop\\42\\marketing\\New folder\\assets-for-ag
- `--dry-run` prints the planned action per file without importing anything.

Idempotent: files whose original filename is already present in
producer_assets.metadata->>'original_filename' are skipped, so re-running
after a partial failure only imports what's missing.

Does NOT require the backend server to be running — it opens its own DB pool
(app.db.open_pool, same as the FastAPI lifespan) and closes it at the end.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# psycopg async + Windows: ProactorEventLoop (the platform default) is
# incompatible. Must be set BEFORE asyncio.run creates the loop — same fix
# as backend/run.py / app/main.py.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# app.config runs load_dotenv() on import, so env (DB creds, Supabase,
# Google ADC project) is available without the server.
from app.db import close_pool, get_conn, open_pool  # noqa: E402
from app.routers.producer_assets import _process_asset_bytes  # noqa: E402

DEFAULT_FOLDER = r"C:\Users\LTSoft\Desktop\42\marketing\New folder\assets-for-ag"

ICON_HINT = "Flat brand icon for short-form video overlays."
TEXTBOX_HINT = (
    "Retro browser-window caption textbox frame: red title bar with window "
    "dots, empty white body, drop shadow. Used as the frame layer behind "
    "popup text."
)

AUDIO_MIMES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}
FONT_MIMES = {
    ".otf": "font/otf",
    ".ttf": "font/ttf",
}


def _clean_stem(path: Path) -> str:
    """'arrow_up-right' -> 'arrow up right' (for display names)."""
    return re.sub(r"[-_]+", " ", path.stem).strip() or path.stem


def _parent_dir_kind(path: Path, root: Path) -> str | None:
    """'icons' / 'textboxes' if any ancestor dir (under root) has that name."""
    try:
        rel_parents = path.relative_to(root).parents
    except ValueError:
        rel_parents = []
    names = {p.name.lower() for p in rel_parents}
    if "icons" in names:
        return "icons"
    if "textboxes" in names:
        return "textboxes"
    return None


def classify(path: Path, root: Path) -> dict | None:
    """Map a file to _process_asset_bytes kwargs, or None to skip it."""
    ext = path.suffix.lower()

    if ext == ".png":
        where = _parent_dir_kind(path, root)
        if where == "textboxes":
            return dict(
                mime="image/png",
                asset_kind="image",
                category="popup_frame",
                display_name=f"Textbox frame — {path.stem}",
                hint=TEXTBOX_HINT,
            )
        # icons dir — and loose PNGs are treated as stickers too
        return dict(
            mime="image/png",
            asset_kind="image",
            category="sticker",
            display_name=f"Icon — {_clean_stem(path)}",
            hint=ICON_HINT,
        )

    if ext in FONT_MIMES:
        name = "VG5000" if "vg5000" in path.stem.lower() else path.stem
        return dict(
            mime=FONT_MIMES[ext],
            asset_kind="font",
            category=None,
            display_name=name,
            hint=None,
        )

    if ext in AUDIO_MIMES:
        return dict(
            mime=AUDIO_MIMES[ext],
            asset_kind="audio",
            category=None,
            display_name=_clean_stem(path),
            hint=None,
        )

    return None  # unknown type — caller prints a note


async def _already_imported() -> set[str]:
    """Original filenames already in producer_assets (idempotency key —
    _process_asset_bytes writes metadata.original_filename on every insert)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT metadata->>'original_filename' AS fn
                  FROM producer_assets
                 WHERE metadata->>'original_filename' IS NOT NULL
                """
            )
            rows = await cur.fetchall()
    return {r["fn"] for r in rows if r.get("fn")}


async def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv[1:]
    root = Path(args[0]) if args else Path(DEFAULT_FOLDER)

    if not root.is_dir():
        print(f"ERROR: folder not found: {root}")
        return 1

    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )
    print(f"Folder: {root}  ({len(files)} file(s)){'  [DRY RUN]' if dry_run else ''}")

    await open_pool()
    imported = skipped = failed = 0
    try:
        existing = await _already_imported()

        for path in files:
            spec = classify(path, root)
            if spec is None:
                print(f"SKIP {path.name} (unsupported type {path.suffix or '<none>'})")
                skipped += 1
                continue

            if path.name in existing:
                print(f"SKIP {path.name} (already imported)")
                skipped += 1
                continue

            if dry_run:
                print(
                    f"PLAN {path.name} -> {spec['asset_kind']}"
                    f"/{spec['category']} \"{spec['display_name']}\""
                )
                imported += 1
                continue

            await asyncio.sleep(2.5)  # base throttle to stay under the Vertex rate limit
            try:
                # Vertex throttles bursts (429 RESOURCE_EXHAUSTED) — retry each
                # file with exponential backoff so the whole folder completes.
                attempt = 0
                while True:
                    try:
                        row = await _process_asset_bytes(
                            raw=path.read_bytes(),
                            filename=path.name,
                            mime=spec["mime"],
                            asset_kind=spec["asset_kind"],
                            category=spec["category"],
                            archetype=None,
                            display_name=spec["display_name"],
                            hint=spec["hint"],
                        )
                        break
                    except Exception as exc:  # noqa: BLE001
                        is_429 = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
                        attempt += 1
                        if not is_429 or attempt > 5:
                            raise
                        wait = min(120, 15 * (2 ** (attempt - 1)))
                        print(f"   …429 on {path.name}, backoff {wait}s (try {attempt}/5)")
                        await asyncio.sleep(wait)
                embedded = bool(row.get("embedded"))
                line = (
                    f"#{row.get('id')} {row.get('asset_kind')}"
                    f"/{row.get('category')} \"{row.get('display_name')}\" "
                    f"embedded={embedded}"
                )
                if not embedded:
                    line = "!! NOT EMBEDDED  " + line + "  <- re-run needed"
                print(line)
                imported += 1
                existing.add(path.name)
            except Exception as exc:  # noqa: BLE001 — keep the batch going
                print(f"FAIL {path.name}: {type(exc).__name__}: {exc}")
                failed += 1
    finally:
        await close_pool()

    verb = "planned" if dry_run else "imported"
    print(f"\nDone: {imported} {verb}, {skipped} skipped, {failed} failed")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
