"""Store for autonomous content campaigns (the "Producer" tab).

Owns two tables and is their SOLE reader/writer — the pattern proven by
`yas_animation_store` and `yas_general_video_store`. There is no migration tool
in this repo, so the schema is created lazily on first use behind a
double-checked lock, and later columns arrive as additive
`ADD COLUMN IF NOT EXISTS`.

A **campaign** is a standing brief: a topic plus weekly quotas ("3 posters and
2 videos per week"). Every 7 days it plans a *cycle* and fills it with **items**,
one per planned post.

## The two human gates

An item walks this path, and the actor for each hop is fixed:

    planned → generating → ready_for_review → approved → scheduled → published
       └─ worker ─────────┘   └ GATE 1: human ┘  └worker┘  └ GATE 2: human ┘

* **Gate 1 (content)** — `approve()` / `decline()` are the ONLY transitions out
  of `ready_for_review`, they are guarded UPDATEs, and nothing else in this
  module can leave that state except `expire_stale_reviews()` at cycle end. The
  worker is structurally unable to approve its own output.
* **Gate 2 (publishing)** — this module never publishes. `mark_scheduled()`
  only books a row in `scheduled_instagram_posts`; a human then clicks Post,
  which goes through the pre-existing roadmap approve endpoint. The worker later
  *mirrors* whatever happened via `mark_from_scheduled_row()`.

Guarded transitions return `None` when they matched no row, which the router
turns into a 409 — that is how a double-click or a stale UI loses the race
safely instead of corrupting state.

## Claims, not locks

`claim_planned()` flips a row to `generating` and stamps `claimed_at` in one
atomic statement, so two workers (a dev laptop and a deployed instance share
this database) can never generate the same item twice. Claims are deliberately
short transactions: claim, commit, THEN do the slow AI work. Holding a
transaction open across a 180-second render would get the connection killed by
Supabase's pooler for being idle-in-transaction.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Campaign lifecycle.
CAMPAIGN_ACTIVE = "active"
CAMPAIGN_PAUSED = "paused"
CAMPAIGN_ARCHIVED = "archived"
CAMPAIGN_STATUSES = (CAMPAIGN_ACTIVE, CAMPAIGN_PAUSED, CAMPAIGN_ARCHIVED)

# Item lifecycle. Terminal states can never be left.
ITEM_PLANNED = "planned"
ITEM_GENERATING = "generating"
ITEM_READY = "ready_for_review"      # ← GATE 1 waits here
ITEM_APPROVED = "approved"           # content approved; not yet booked
ITEM_SCHEDULED = "scheduled"         # booked; ← GATE 2 waits here
ITEM_PUBLISHED = "published"         # terminal
ITEM_REJECTED = "rejected"           # terminal — declined, no retries left
ITEM_FAILED = "failed"               # generation failed for good
ITEM_PUBLISH_FAILED = "publish_failed"  # the human's Post attempt failed
ITEM_SKIPPED = "skipped"             # terminal — Instagram not configured
ITEM_EXPIRED = "expired"             # terminal — cycle ended without review
ITEM_CANCELLED = "cancelled"         # terminal — booking cancelled

ITEM_TERMINAL = frozenset({
    ITEM_PUBLISHED, ITEM_REJECTED, ITEM_SKIPPED, ITEM_EXPIRED, ITEM_CANCELLED,
})
# What the UI should keep polling for.
ITEM_IN_FLIGHT = frozenset({ITEM_PLANNED, ITEM_GENERATING, ITEM_APPROVED})

KIND_POSTER = "poster"
KIND_VIDEO = "video"
ITEM_KINDS = (KIND_POSTER, KIND_VIDEO)

# How many autonomous regenerations a declined item gets before it is rejected.
MAX_ATTEMPTS = 3

# A claim older than this means the worker died mid-generation. Comfortably
# above the 180s Stage-2 render timeout plus upload.
STALE_CLAIM_MINUTES = 15


_CREATE_CAMPAIGNS = """
CREATE TABLE IF NOT EXISTS producer_campaigns (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL,
    topic_brief        TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'active',
    status_reason      TEXT,
    posters_per_week   INTEGER NOT NULL DEFAULT 3,
    videos_per_week    INTEGER NOT NULL DEFAULT 0,
    duration_weeks     INTEGER,
    posting_windows    JSONB NOT NULL DEFAULT '[]'::jsonb,
    poster_style       TEXT NOT NULL DEFAULT 'voxel',
    video_duration_sec INTEGER NOT NULL DEFAULT 6,
    video_with_audio   BOOLEAN NOT NULL DEFAULT true,
    weekly_budget_usd  NUMERIC,
    notify_email       BOOLEAN NOT NULL DEFAULT true,
    cycle_index        INTEGER NOT NULL DEFAULT 0,
    next_cycle_at      TIMESTAMPTZ,
    last_cycle_at      TIMESTAMPTZ,
    settings           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_CREATE_ITEMS = """
CREATE TABLE IF NOT EXISTS producer_campaign_items (
    id                 SERIAL PRIMARY KEY,
    campaign_id        INTEGER NOT NULL REFERENCES producer_campaigns(id) ON DELETE CASCADE,
    cycle_index        INTEGER NOT NULL DEFAULT 0,
    cycle_start        DATE,
    slot_index         INTEGER NOT NULL DEFAULT 0,
    kind               TEXT NOT NULL DEFAULT 'poster',
    status             TEXT NOT NULL DEFAULT 'planned',
    idea               JSONB NOT NULL DEFAULT '{}'::jsonb,
    idea_title         TEXT,
    gen_kind           TEXT,
    gen_id             BIGINT,
    caption            TEXT,
    proposed_time      TIMESTAMPTZ,
    scheduled_post_id  BIGINT,
    attempt            INTEGER NOT NULL DEFAULT 0,
    decline_note       TEXT,
    error              TEXT,
    cost_usd           NUMERIC NOT NULL DEFAULT 0,
    notified_at        TIMESTAMPTZ,
    published_at       TIMESTAMPTZ,
    permalink          TEXT,
    claimed_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Makes cycle planning idempotent: re-running a plan (a retry, an overlapping
# tick, a manual "run cycle now") can only ever DO NOTHING for a slot that
# already exists.
_ITEM_SLOT_UNIQUE = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_item_slot
    ON producer_campaign_items (campaign_id, cycle_index, slot_index)
"""
_ITEM_STATUS_IDX = """
CREATE INDEX IF NOT EXISTS idx_campaign_item_status
    ON producer_campaign_items (status, campaign_id)
"""

_CAMPAIGN_COLUMNS: tuple[str, ...] = (
    "name", "topic_brief", "status", "status_reason",
    "posters_per_week", "videos_per_week", "duration_weeks",
    "posting_windows", "poster_style", "video_duration_sec", "video_with_audio",
    "weekly_budget_usd", "notify_email", "cycle_index",
    "next_cycle_at", "last_cycle_at", "settings",
)
_ITEM_COLUMNS: tuple[str, ...] = (
    "campaign_id", "cycle_index", "cycle_start", "slot_index", "kind", "status",
    "idea", "idea_title", "gen_kind", "gen_id", "caption", "proposed_time",
    "scheduled_post_id", "attempt", "decline_note", "error", "cost_usd",
    "notified_at", "published_at", "permalink", "claimed_at",
)
# Columns whose Python value is a dict/list and must be adapted for JSONB.
_JSON_COLS = {"posting_windows", "settings", "idea"}

_table_ready = False
_table_lock = asyncio.Lock()


async def _ensure_tables() -> None:
    global _table_ready
    if _table_ready:
        return
    async with _table_lock:
        if _table_ready:
            return
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_CREATE_CAMPAIGNS)
                await cur.execute(_CREATE_ITEMS)
                await cur.execute(_ITEM_SLOT_UNIQUE)
                await cur.execute(_ITEM_STATUS_IDX)
        _table_ready = True


def _row_out(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif hasattr(v, "isoformat"):     # date (cycle_start)
            out[k] = v.isoformat()
    return out


def _rows_out(rows: Iterable[dict]) -> list[dict]:
    return [r for r in (_row_out(x) for x in rows) if r is not None]


def _adapt(col: str, value: Any) -> Any:
    if col in _JSON_COLS and isinstance(value, (dict, list)):
        return Jsonb(value)
    return value


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

async def create_campaign(**fields: Any) -> dict:
    """Insert a campaign. Unsupplied columns take their SQL defaults."""
    await _ensure_tables()
    cols = [c for c in _CAMPAIGN_COLUMNS if c in fields]
    vals = [_adapt(c, fields[c]) for c in cols]
    placeholders = ", ".join(["%s"] * len(cols))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            if cols:
                await cur.execute(
                    f"INSERT INTO producer_campaigns ({', '.join(cols)}) "
                    f"VALUES ({placeholders}) RETURNING *",
                    vals,
                )
            else:
                await cur.execute("INSERT INTO producer_campaigns DEFAULT VALUES RETURNING *")
            row = await cur.fetchone()
    return _row_out(row) or {}


async def update_campaign(campaign_id: int, **fields: Any) -> Optional[dict]:
    await _ensure_tables()
    sets: list[str] = []
    vals: list[Any] = []
    for c in _CAMPAIGN_COLUMNS:
        if c in fields:
            sets.append(f"{c} = %s")
            vals.append(_adapt(c, fields[c]))
    if not sets:
        return await get_campaign(campaign_id)
    sets.append("updated_at = now()")
    vals.append(int(campaign_id))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE producer_campaigns SET {', '.join(sets)} WHERE id = %s RETURNING *",
                vals,
            )
            row = await cur.fetchone()
    return _row_out(row)


async def get_campaign(campaign_id: int) -> Optional[dict]:
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM producer_campaigns WHERE id = %s", (int(campaign_id),)
            )
            row = await cur.fetchone()
    return _row_out(row)


async def list_campaigns(*, include_archived: bool = False) -> list[dict]:
    """Campaigns newest-first, each with the counts the cards need.

    The per-status counts come back as one aggregate row per campaign rather
    than N+1 queries — the database is in Tokyo, so a round trip costs ~285 ms
    and is worth far more than the join.
    """
    await _ensure_tables()
    where = "" if include_archived else "WHERE c.status <> 'archived'"
    sql = f"""
        SELECT c.*,
               COALESCE(i.total, 0)        AS item_count,
               COALESCE(i.awaiting, 0)     AS awaiting_review,
               COALESCE(i.in_flight, 0)    AS in_flight,
               COALESCE(i.scheduled, 0)    AS scheduled_count,
               COALESCE(i.published, 0)    AS published_count,
               COALESCE(i.failed, 0)       AS failed_count,
               COALESCE(i.cycle_cost, 0)   AS cycle_cost_usd,
               i.next_post_at
          FROM producer_campaigns c
          LEFT JOIN (
              SELECT campaign_id,
                     COUNT(*)                                              AS total,
                     COUNT(*) FILTER (WHERE status = 'ready_for_review')   AS awaiting,
                     COUNT(*) FILTER (WHERE status IN ('planned','generating','approved')) AS in_flight,
                     COUNT(*) FILTER (WHERE status = 'scheduled')          AS scheduled,
                     COUNT(*) FILTER (WHERE status = 'published')          AS published,
                     COUNT(*) FILTER (WHERE status IN ('failed','publish_failed')) AS failed,
                     MIN(proposed_time) FILTER (WHERE status = 'scheduled') AS next_post_at,
                     SUM(cost_usd)                                         AS cycle_cost
                FROM producer_campaign_items
               GROUP BY campaign_id
          ) i ON i.campaign_id = c.id
          {where}
         ORDER BY c.created_at DESC
    """
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
    return _rows_out(rows)


async def due_campaigns(*, limit: int = 20) -> list[dict]:
    """Active campaigns whose next cycle is due. Ordered oldest-due first so a
    backlog drains fairly rather than starving one campaign."""
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM producer_campaigns "
                " WHERE status = 'active' "
                "   AND (next_cycle_at IS NULL OR next_cycle_at <= now()) "
                " ORDER BY next_cycle_at NULLS FIRST, id "
                " LIMIT %s",
                (int(limit),),
            )
            rows = await cur.fetchall()
    return _rows_out(rows)


async def active_campaigns() -> list[dict]:
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM producer_campaigns WHERE status = 'active' ORDER BY id"
            )
            rows = await cur.fetchall()
    return _rows_out(rows)


async def pause_campaign(campaign_id: int, reason: str | None = None) -> Optional[dict]:
    """Stop planning and generating. Deliberately does NOT touch items that are
    already approved or booked — pausing a campaign must not silently unbook a
    post a human already approved."""
    return await update_campaign(campaign_id, status=CAMPAIGN_PAUSED, status_reason=reason)


async def resume_campaign(campaign_id: int) -> Optional[dict]:
    return await update_campaign(campaign_id, status=CAMPAIGN_ACTIVE, status_reason=None)


async def archive_campaign(campaign_id: int) -> Optional[dict]:
    return await update_campaign(campaign_id, status=CAMPAIGN_ARCHIVED)


async def delete_campaign(campaign_id: int) -> Optional[dict]:
    """Hard-delete a campaign and (by FK cascade) its items.

    Booked `scheduled_instagram_posts` rows and the generated posters/videos are
    NOT touched here — the caller cancels the bookings (it knows that published
    rows must be kept as an audit trail) and the generations stay in the Library
    as ordinary posters.
    """
    await _ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM producer_campaigns WHERE id = %s RETURNING *",
                (int(campaign_id),),
            )
            row = await cur.fetchone()
    return _row_out(row)


async def spent_this_cycle(campaign_id: int, cycle_index: int) -> float:
    """Derived, never stored — a cached copy would drift from the items."""
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS spent "
                "  FROM producer_campaign_items "
                " WHERE campaign_id = %s AND cycle_index = %s",
                (int(campaign_id), int(cycle_index)),
            )
            row = await cur.fetchone()
    return float(row["spent"]) if row else 0.0


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

async def plan_items(rows: list[dict]) -> list[dict]:
    """Insert a cycle's planned slots, skipping any that already exist.

    `ON CONFLICT DO NOTHING` against the (campaign_id, cycle_index, slot_index)
    unique index is what makes planning idempotent — a crashed-and-retried plan,
    or two overlapping ticks, cannot double-book a slot. Returns only the rows
    that were actually inserted.
    """
    if not rows:
        return []
    await _ensure_tables()
    cols = list(_ITEM_COLUMNS)
    placeholders = ", ".join(["%s"] * len(cols))
    out: list[dict] = []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            for r in rows:
                vals = [_adapt(c, r.get(c)) for c in cols]
                await cur.execute(
                    f"INSERT INTO producer_campaign_items ({', '.join(cols)}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT (campaign_id, cycle_index, slot_index) DO NOTHING "
                    f"RETURNING *",
                    vals,
                )
                row = await cur.fetchone()
                if row is not None:
                    out.append(row)
    return _rows_out(out)


async def get_item(item_id: int) -> Optional[dict]:
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM producer_campaign_items WHERE id = %s", (int(item_id),)
            )
            row = await cur.fetchone()
    return _row_out(row)


async def update_item(item_id: int, **fields: Any) -> Optional[dict]:
    """Unguarded patch — for worker bookkeeping (cost, gen ids, errors).

    Do NOT use this to cross a gate: `approve`/`decline` exist precisely so a
    human decision cannot be forged by a status write from anywhere else.
    """
    await _ensure_tables()
    sets: list[str] = []
    vals: list[Any] = []
    for c in _ITEM_COLUMNS:
        if c in fields:
            sets.append(f"{c} = %s")
            vals.append(_adapt(c, fields[c]))
    if not sets:
        return await get_item(item_id)
    sets.append("updated_at = now()")
    vals.append(int(item_id))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE producer_campaign_items SET {', '.join(sets)} "
                f"WHERE id = %s RETURNING *",
                vals,
            )
            row = await cur.fetchone()
    return _row_out(row)


async def items_for(
    campaign_id: int,
    *,
    cycle_index: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """Items for one campaign, newest cycle first. Always capped."""
    await _ensure_tables()
    sql = ("SELECT * FROM producer_campaign_items WHERE campaign_id = %s")
    params: list[Any] = [int(campaign_id)]
    if cycle_index is not None:
        sql += " AND cycle_index = %s"
        params.append(int(cycle_index))
    sql += " ORDER BY cycle_index DESC, slot_index ASC LIMIT %s"
    params.append(int(limit))
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
    return _rows_out(rows)


async def items_by_status(status: str | Iterable[str], *, limit: int = 100) -> list[dict]:
    await _ensure_tables()
    statuses = (status,) if isinstance(status, str) else tuple(status)
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM producer_campaign_items "
                " WHERE status = ANY(%s) ORDER BY id LIMIT %s",
                (list(statuses), int(limit)),
            )
            rows = await cur.fetchall()
    return _rows_out(rows)


async def review_queue(*, limit: int = 100) -> list[dict]:
    """Everything waiting at gate 1, across all campaigns, soonest slot first.

    Joins the campaign name so the queue can say which campaign an item is from
    without a second round trip to Tokyo.
    """
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT i.*, c.name AS campaign_name, c.poster_style "
                "  FROM producer_campaign_items i "
                "  JOIN producer_campaigns c ON c.id = i.campaign_id "
                " WHERE i.status = 'ready_for_review' "
                " ORDER BY i.proposed_time NULLS LAST, i.id "
                " LIMIT %s",
                (int(limit),),
            )
            rows = await cur.fetchall()
    return _rows_out(rows)


async def awaiting_review_count() -> int:
    """Cheap count for the sidebar badge."""
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) AS n FROM producer_campaign_items "
                " WHERE status = 'ready_for_review'"
            )
            row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def recent_idea_titles(campaign_id: int, *, limit: int = 40) -> list[str]:
    """Titles already used by this campaign — the planner's cross-cycle
    avoid-list, so week 3 doesn't re-pitch week 1."""
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT idea_title FROM producer_campaign_items "
                " WHERE campaign_id = %s AND idea_title IS NOT NULL "
                " ORDER BY id DESC LIMIT %s",
                (int(campaign_id), int(limit)),
            )
            rows = await cur.fetchall()
    return [r["idea_title"] for r in rows if (r.get("idea_title") or "").strip()]


async def recent_decline_notes(campaign_id: int, *, limit: int = 10) -> list[str]:
    """Why the reviewer rejected things — fed back into the next plan so the
    campaign actually learns the user's taste."""
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT decline_note FROM producer_campaign_items "
                " WHERE campaign_id = %s AND decline_note IS NOT NULL "
                "   AND decline_note <> '' "
                " ORDER BY updated_at DESC LIMIT %s",
                (int(campaign_id), int(limit)),
            )
            rows = await cur.fetchall()
    return [r["decline_note"] for r in rows if (r.get("decline_note") or "").strip()]


# ---------------------------------------------------------------------------
# Guarded transitions — the state machine lives here, not in the router
# ---------------------------------------------------------------------------

async def claim_planned(campaign_id: int) -> Optional[dict]:
    """Atomically take ONE planned item for generation.

    `FOR UPDATE SKIP LOCKED` plus the status predicate means concurrent workers
    each get a different row, or none. Returns None when there is nothing to do.
    Commit happens on exit — the caller then does the slow AI work OUTSIDE this
    transaction.
    """
    await _ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE producer_campaign_items SET status = %s, claimed_at = now(), "
                "       error = NULL, updated_at = now() "
                " WHERE id = ( "
                "     SELECT id FROM producer_campaign_items "
                "      WHERE campaign_id = %s AND status = %s "
                "      ORDER BY slot_index, id "
                "      FOR UPDATE SKIP LOCKED LIMIT 1 "
                " ) RETURNING *",
                (ITEM_GENERATING, int(campaign_id), ITEM_PLANNED),
            )
            row = await cur.fetchone()
    return _row_out(row)


async def mark_ready_for_review(
    item_id: int, *, caption: str, gen_kind: str, gen_id: int, cost_usd: float = 0.0
) -> Optional[dict]:
    """Generation finished — hand the item to gate 1.

    Guarded on `generating` so a late-arriving worker cannot drag an item that a
    human has already approved back into the review queue.
    """
    await _ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE producer_campaign_items "
                "   SET status = %s, caption = %s, gen_kind = %s, gen_id = %s, "
                "       cost_usd = %s, error = NULL, claimed_at = NULL, updated_at = now() "
                " WHERE id = %s AND status = %s RETURNING *",
                (ITEM_READY, caption, gen_kind, int(gen_id), cost_usd,
                 int(item_id), ITEM_GENERATING),
            )
            row = await cur.fetchone()
    return _row_out(row)


async def record_attempt_failure(
    item_id: int, error: str, *, max_attempts: int = MAX_ATTEMPTS
) -> Optional[dict]:
    """A generation attempt failed. Retry until the budget of attempts is spent,
    then land on `failed` so the item stops consuming provider quota."""
    await _ensure_tables()
    item = await get_item(item_id)
    if item is None:
        return None
    attempt = int(item.get("attempt") or 0) + 1
    exhausted = attempt >= max_attempts
    return await update_item(
        item_id,
        status=ITEM_FAILED if exhausted else ITEM_PLANNED,
        attempt=attempt,
        error=(error or "")[:500],
        claimed_at=None,
    )


async def approve(
    item_id: int, *, caption: str | None = None, proposed_time: Any | None = None
) -> Optional[dict]:
    """GATE 1 — the human approves the content.

    Guarded on `ready_for_review`, so this is the only door out of the review
    state and only a real human request can open it. Optional overrides let the
    reviewer fix the caption or move the slot in the same click.

    Approving does NOT publish. It marks the content good; the worker then books
    it, and a human still has to press Post (gate 2).
    """
    await _ensure_tables()
    sets = ["status = %s", "error = NULL", "updated_at = now()"]
    vals: list[Any] = [ITEM_APPROVED]
    if caption is not None:
        sets.append("caption = %s")
        vals.append(caption)
    if proposed_time is not None:
        sets.append("proposed_time = %s::timestamptz")
        vals.append(proposed_time)
    vals.extend([int(item_id), ITEM_READY])
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE producer_campaign_items SET {', '.join(sets)} "
                f" WHERE id = %s AND status = %s RETURNING *",
                vals,
            )
            row = await cur.fetchone()
    return _row_out(row)


async def decline(
    item_id: int, *, note: str | None = None, regenerate: bool = True,
    max_attempts: int = MAX_ATTEMPTS,
) -> Optional[dict]:
    """GATE 1 — the human rejects the content.

    With `regenerate` the item goes back to `planned` and the worker tries again,
    folding the note into the next prompt. Without it (or once the attempt budget
    is gone) the item is terminally `rejected`. The note is kept either way — it
    is taste feedback for future cycles.
    """
    await _ensure_tables()
    current = await get_item(item_id)
    if current is None or current.get("status") != ITEM_READY:
        return None
    attempt = int(current.get("attempt") or 0) + 1
    retry = regenerate and attempt < max_attempts
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE producer_campaign_items "
                "   SET status = %s, decline_note = %s, attempt = %s, "
                "       gen_kind = NULL, gen_id = NULL, caption = NULL, "
                "       claimed_at = NULL, updated_at = now() "
                " WHERE id = %s AND status = %s RETURNING *",
                (ITEM_PLANNED if retry else ITEM_REJECTED, note, attempt,
                 int(item_id), ITEM_READY),
            )
            row = await cur.fetchone()
    return _row_out(row)


async def mark_scheduled(item_id: int, scheduled_post_id: int) -> Optional[dict]:
    """Booked into the calendar. Gate 2 (a human pressing Post) comes later.

    Guarded on `approved` so a booking can only follow a real approval.
    """
    await _ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE producer_campaign_items "
                "   SET status = %s, scheduled_post_id = %s, error = NULL, updated_at = now() "
                " WHERE id = %s AND status = %s RETURNING *",
                (ITEM_SCHEDULED, int(scheduled_post_id), int(item_id), ITEM_APPROVED),
            )
            row = await cur.fetchone()
    return _row_out(row)


# How a scheduled_instagram_posts.status maps onto our item status. The worker
# only ever mirrors — it never publishes, so these are observations, not actions.
_SCHED_TO_ITEM = {
    "published": ITEM_PUBLISHED,
    "failed": ITEM_PUBLISH_FAILED,
    "cancelled": ITEM_CANCELLED,
}


async def mark_from_scheduled_row(item_id: int, sched_row: Optional[dict]) -> Optional[dict]:
    """Mirror a booking's outcome onto the item.

    `sched_row is None` means the booking vanished — which really happens: the
    Library hard-deletes non-published bookings when their source generation is
    deleted. That is terminal for the item, not a transient miss.

    `permalink`/`published_at` are copied onto the item on purpose. The booking
    row can disappear later, and a campaign's history of what actually went out
    should survive that.
    """
    if sched_row is None:
        return await update_item(
            item_id, status=ITEM_FAILED,
            error="the scheduled post disappeared (its source generation was deleted)",
        )
    target = _SCHED_TO_ITEM.get(str(sched_row.get("status") or ""))
    if target is None:
        return None  # still pending/reminded — nothing to mirror yet
    fields: dict[str, Any] = {"status": target}
    if target == ITEM_PUBLISHED:
        fields["permalink"] = sched_row.get("permalink")
        fields["published_at"] = sched_row.get("published_at")
        fields["error"] = None
    elif target == ITEM_PUBLISH_FAILED:
        fields["error"] = (sched_row.get("error") or "publishing failed")[:500]
    return await update_item(item_id, **fields)


async def cancel_item(item_id: int) -> Optional[dict]:
    """Human cancels a booked or approved item before it goes out."""
    await _ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE producer_campaign_items SET status = %s, updated_at = now() "
                " WHERE id = %s AND status = ANY(%s) RETURNING *",
                (ITEM_CANCELLED, int(item_id), [ITEM_APPROVED, ITEM_SCHEDULED,
                                                ITEM_PUBLISH_FAILED]),
            )
            row = await cur.fetchone()
    return _row_out(row)


async def reclaim_stale(*, minutes: int = STALE_CLAIM_MINUTES) -> list[dict]:
    """Recover items whose worker died mid-generation.

    Without this an item claimed just before a crash would sit in `generating`
    forever. Mirrors `db.cleanup_orphan_runs`, which does the same for agent runs.
    The row goes back to `planned` (or `failed` once attempts run out) via
    `record_attempt_failure`.
    """
    await _ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM producer_campaign_items "
                " WHERE status = %s AND claimed_at IS NOT NULL "
                "   AND claimed_at < now() - make_interval(mins => %s)",
                (ITEM_GENERATING, int(minutes)),
            )
            rows = await cur.fetchall()
    out = []
    for r in rows:
        fixed = await record_attempt_failure(
            int(r["id"]), "generation stalled (the worker restarted mid-render)"
        )
        if fixed:
            out.append(fixed)
    return out


async def expire_stale_reviews(*, cycle_days: int = 7) -> list[dict]:
    """Items nobody reviewed before their cycle ended.

    The one case where the worker may leave `ready_for_review` — it does not
    decide anything about the content, it just stops offering a stale slot.
    """
    await _ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE producer_campaign_items SET status = %s, updated_at = now() "
                " WHERE status = %s "
                "   AND cycle_start IS NOT NULL "
                "   AND cycle_start < (now() - make_interval(days => %s))::date "
                " RETURNING *",
                (ITEM_EXPIRED, ITEM_READY, int(cycle_days)),
            )
            rows = await cur.fetchall()
    return _rows_out(rows)


async def mark_notified(item_ids: list[int]) -> None:
    """Stamp the review-ready notification so it is sent once, not every tick."""
    if not item_ids:
        return
    await _ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE producer_campaign_items SET notified_at = now() "
                " WHERE id = ANY(%s)",
                ([int(i) for i in item_ids],),
            )
