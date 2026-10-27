"""Industry Signal - the brief, and the log of every search somebody ran.

Two tables. `news_config` is a single row holding `brief`, the sentence the
owner writes about what to look for, which is the entire configuration surface.
`news_runs` is one row per search: what it found, what it kept, what it dropped
and why.

NOTHING IS ON A TIMER. This module used to own a schedule: `next_daily_run`,
`due`, `due_now` and a 7am Asia/Dubai anchor kept in `news_config.next_run_at`.
All of it was removed by request. A search nobody asked for spends money on
mornings nobody reads, and the screen had to explain a cadence just to stop a
quiet feed from looking broken. A person presses a button and says how many
stories they want at most.

`next_run_at` and `enabled` are still COLUMNS and nothing reads them. They were
not dropped on purpose: this is a shared production database, and dropping
columns to tidy up a feature is not a trade worth making.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn

log = logging.getLogger("archivist.news_store")

# NOBODY SEARCHES ON A TIMER ANY MORE.
#
# This used to run itself at 7am Dubai, and the schedule lived in
# `news_config.next_run_at`. It was removed by request: an unattended job that
# nobody asked for spends money on mornings nobody is reading, and the screen
# had to carry a sentence explaining a cadence just to stop a quiet feed looking
# broken. A person presses a button, says how many stories they want, and that
# is the whole contract.
#
# `next_run_at` IS STILL A COLUMN, deliberately not dropped. It costs nothing,
# it holds the last schedule that ever applied, and dropping a column from a
# shared production database to tidy up a feature is not a trade worth making.
# Nothing reads it.

# Bounds on the cap a person sets when they press the button. Below three a
# quiet search shows nothing at all; above twenty nobody reads to the bottom,
# and the whole reason the old collector was cut back was 1430 stored stories
# nobody could read.
MIN_STORIES = 3
MAX_STORIES = 20
DEFAULT_STORIES = 10

# A run left `running` for longer than this was killed mid-flight: the backend
# restarted, or the process died. Nothing will ever close it, and until it is
# closed the button on screen stays disabled behind a spinner that never stops.
# Comfortably longer than a real run, which measures at about two minutes.
STALE_RUN_MINUTES = 15

# How many recent headlines are handed to the model so it can skip what we have.
RECENT_HEADLINES = 40

# Run rows kept for the "recent mornings" list. Two weeks matches the story
# retention window, so the log and the feed age out together.
RUN_LOG_DAYS = 14

# The brief a fresh database starts with. Deliberately about what people can DO
# rather than about companies or technologies, because that is the axis the whole
# feature judges on. It is a starting point the owner is expected to edit.
DEFAULT_BRIEF = (
    "AI that changes how people learn to code, study, or build software. "
    "New AI tools and products developers and students can actually use. "
    "AI in education and teaching. How AI is changing tech careers and the jobs "
    "people are hired for. Big AI launches, when you can say what they let "
    "people do."
)

MAX_BRIEF_CHARS = 4000

# WHAT WE FOLLOW, and why it replaced the box.
#
# The brief was the whole configuration surface: one free-text field whose
# wording decided everything the feature found. It asked a person to write a
# search brief, which is a skill, and the screen carried an AI button to write
# it for them, which is the tell. What people actually want to say is short and
# concrete: Anthropic. OpenAI. AI in schools. theverge.com.
#
# So the surface is a LIST, and the prompt is composed from it (`watch_text`).
# `news_config.brief` is neither dropped nor read by the search any more: the
# first boot after this change turns its sentences INTO the first rows of the
# list, so the day before and the day after look for the same things.
#
# ONE TENSION, STATED OUT LOUD. The brief-writing prompt in news_search says
# that a brief naming companies returns press releases, and it is right. Naming
# companies is exactly what was asked for, so the instruction that carries the
# names says what to do with them: report what that company DID and what it
# lets people do. The judge still throws away the rest.
MAX_WATCH_CHARS = 120
MAX_WATCH_ROWS = 40

# The one-time changeover from the RSS collector, and it has to fire EXACTLY
# once. The old `news_sources` table is its own trigger: while that table still
# exists this is the first boot after the rewrite, so the stories the feed
# readers collected are cleared and the table goes with them. Every later boot
# finds nothing to do.
#
# Keying the wipe on the table rather than on a version number is what makes it
# safe to leave here forever. A bare `DELETE FROM trend_posts WHERE source =
# 'news'` in this file would empty the feed on every restart.
#
# The delete is deliberately unqualified: it takes saved and used rows too. The
# owner asked for a clean slate, and a half-migrated feed mixing publisher
# headlines with plain-English ones is harder to read than an empty one. The
# narrower form, if that is ever wanted again, is `AND status = 'new'`.
_MIGRATION = """
DO $$
BEGIN
    IF to_regclass('public.news_sources') IS NOT NULL THEN
        DELETE FROM trend_posts WHERE source = 'news';
        DROP TABLE news_sources;
        RAISE NOTICE 'Industry Signal: cleared the old feed-collector stories';
    END IF;
END $$;
DROP TABLE IF EXISTS news_settings;
"""

_DDL = """
CREATE TABLE IF NOT EXISTS news_config (
    id              INT         PRIMARY KEY DEFAULT 1,
    brief           TEXT        NOT NULL,
    stories_per_day INT         NOT NULL DEFAULT 10,
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    next_run_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT news_config_singleton CHECK (id = 1),
    CONSTRAINT news_config_stories CHECK (stories_per_day BETWEEN 3 AND 20)
);

CREATE TABLE IF NOT EXISTS news_runs (
    id           BIGSERIAL   PRIMARY KEY,
    status       TEXT        NOT NULL DEFAULT 'running',
    found        INT         NOT NULL DEFAULT 0,
    kept         INT         NOT NULL DEFAULT 0,
    drops        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error        TEXT,
    trigger      TEXT        NOT NULL DEFAULT 'auto',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_news_runs_recent ON news_runs (started_at DESC);
"""

# The websites a fresh database starts with. Company sites on purpose: the
# owner asked for OpenAI, Anthropic and Google, and on those the company's own
# announcements are the stories. They still go through the same judge as
# everything else (`news_search.SITES_SYSTEM`), so a benchmark table or a
# pricing change from openai.com is dropped exactly as it would be from
# anywhere else. `blog.google` rather than google.com because that is where
# Google announces things; google.com is a search box.
DEFAULT_SITES = ("openai.com", "anthropic.com", "blog.google")

# Created and seeded in ONE guarded block, so the seed can only ever happen at
# creation. An unguarded "seed when empty" would refill the list the next time
# the backend restarted after somebody deliberately emptied it.
#
# The seed reads the owner's OWN brief rather than the shipped default: whatever
# the search was looking for yesterday is what the list says today. Sentences
# are the unit because that is how the brief was written, one thought per line.
_WATCH_DDL = """
DO $$
BEGIN
    IF to_regclass('public.news_watch') IS NULL THEN
        CREATE TABLE news_watch (
            id         BIGSERIAL   PRIMARY KEY,
            kind       TEXT        NOT NULL DEFAULT 'name',
            value      TEXT        NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT news_watch_kind CHECK (kind IN ('name', 'site'))
        );
        CREATE UNIQUE INDEX idx_news_watch_value ON news_watch (lower(value));

        INSERT INTO news_watch (kind, value)
        SELECT 'name', btrim(s, ' .')
        FROM news_config, unnest(string_to_array(brief, '. ')) AS s
        WHERE news_config.id = 1 AND length(btrim(s, ' .')) BETWEEN 3 AND 120;

        INSERT INTO news_watch (kind, value) VALUES __DEFAULT_SITES__
        ON CONFLICT DO NOTHING;

        RAISE NOTICE 'Industry Signal: the brief became the follow list';
    END IF;
END $$;
""".replace("__DEFAULT_SITES__", ", ".join(f"('site', '{s}')" for s in DEFAULT_SITES))

# Every status a run row can carry, and what each one means to the person reading
# the screen. Listed here rather than scattered through the poller so the UI and
# the worker cannot drift on the vocabulary.
RUN_STATUSES = (
    "running",        # in flight
    "ok",             # stories were written
    "empty",          # it ran, found nothing worth keeping. NOT an error
    "ungrounded",     # the model answered without searching; reply discarded
    "search_failed",  # the search call broke
    "judge_failed",   # the judging call broke or could not be read
    "no_budget",      # HISTORICAL. Nothing writes this any more: the spend cap
                      # that produced it is gone (vertex_client.VertexCostMeter).
                      # Kept so rows already in news_runs still read back.
    "unavailable",    # credentials or billing; a person has to fix it
    "cancelled",      # somebody pressed Stop. Not a failure, and never drawn as one
)


_ready = False


async def ensure_tables() -> None:
    """Run the changeover, create both tables, seed the config row.

    Idempotent and cached per process. The migration first, because the config
    row is meaningless until the old collector's tables are gone.
    """
    global _ready
    if _ready:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_MIGRATION)
            await cur.execute(_DDL)
            # ON CONFLICT DO NOTHING, so a redeploy never overwrites an edited
            # brief with the shipped default.
            await cur.execute(
                "INSERT INTO news_config (id, brief, stories_per_day) "
                "VALUES (1, %s, %s) ON CONFLICT (id) DO NOTHING",
                (DEFAULT_BRIEF, DEFAULT_STORIES),
            )
            # After the config row exists, because the follow list is seeded
            # from the brief that row holds.
            await cur.execute(_WATCH_DDL)
    _ready = True


async def get_config() -> dict:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, brief, stories_per_day, enabled, next_run_at, updated_at "
                "FROM news_config WHERE id = 1"
            )
            row = await cur.fetchone()
    if row is None:  # pragma: no cover - the seed above guarantees it
        raise RuntimeError("news_config row missing")
    return dict(row)


def clean_brief(raw: Any) -> str:
    """Validate a brief. Raises ValueError with a sentence a person can read.

    An empty brief is refused rather than defaulted, because the search prompt is
    built around it: a blank one asks the model to find "AI news" with no test to
    apply, which is the exact failure this feature was rebuilt to escape.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        raise ValueError("Tell us what to look for - the search needs something to go on.")
    if len(text) > MAX_BRIEF_CHARS:
        raise ValueError(f"That is too long. Keep it under {MAX_BRIEF_CHARS} characters.")
    return text


def clean_stories_per_day(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError("How many stories a day has to be a number.") from None
    if not (MIN_STORIES <= n <= MAX_STORIES):
        raise ValueError(f"Pick a number between {MIN_STORIES} and {MAX_STORIES}.")
    return n


async def update_config(patch: dict) -> dict:
    """Apply only the fields present. Validates before it writes; never clamps.

    Silently clamping an out-of-range number is how a control ends up doing
    something other than what it says, so the value is refused and the reason is
    handed back to the screen.
    """
    await ensure_tables()
    fields: dict[str, Any] = {}
    if "brief" in patch:
        fields["brief"] = clean_brief(patch["brief"])
    if "stories_per_day" in patch:
        fields["stories_per_day"] = clean_stories_per_day(patch["stories_per_day"])
    if "enabled" in patch:
        if patch["enabled"] is None:
            raise ValueError("Switched on has to be yes or no.")
        fields["enabled"] = bool(patch["enabled"])
    if not fields:
        raise ValueError("Nothing to change.")

    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE news_config SET {sets}, updated_at = now() WHERE id = 1 "
                f"RETURNING id, brief, stories_per_day, enabled, next_run_at, updated_at",
                fields,
            )
            row = await cur.fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError("news_config row missing")
    return dict(row)


# ---------------------------------------------------------------------------
# The follow list
# ---------------------------------------------------------------------------

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.I)

# A dot is not enough to call something a website: "Node.js", "Next.js" and
# "OpenAI vs. Google" all have one. So a bare value is only a site when its last
# part is an ending websites actually use. Anything with http:// in front of it
# is a website whatever it ends in, because somebody pasted a link.
_SITE_ENDINGS = frozenset("""
com net org io ai co dev app edu gov mil news tech xyz info blog me tv google
uk ae us eu de fr es it nl se no dk fi pl pt ie in cn jp kr au ca ch br za sa
""".split())


def _host_head(text: str) -> str:
    """The would-be host of whatever was typed: no scheme, no path, no www."""
    head = _SCHEME_RE.sub("", text).split("/")[0].split("?")[0].strip().lower()
    return head[4:] if head.startswith("www.") else head


def clean_watch(raw: Any) -> tuple[str, str]:
    """One thing to follow, as (kind, value). Raises ValueError a person can read.

    A website and a name are told apart HERE, not by asking which one is being
    typed, because nobody typing "Anthropic" or pasting a link thinks of that as
    a choice. A link keeps only its host, so `https://www.theverge.com/ai/123`
    is stored as `theverge.com`: the path is one article, and what we can
    actually follow is the site.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        raise ValueError("Type a company, a person, a subject or a website.")
    if len(text) > MAX_WATCH_CHARS:
        raise ValueError(f"That is too long. Keep it under {MAX_WATCH_CHARS} characters.")

    head = _host_head(text)
    ending = head.rsplit(".", 1)[-1] if "." in head else ""
    looks_like_link = bool(_SCHEME_RE.match(text)) or (
        " " not in text and ending in _SITE_ENDINGS
    )
    if looks_like_link:
        if not _HOST_RE.match(head):
            raise ValueError("That does not look like a website address.")
        return "site", head
    return "name", text


def resolve_watch(raw: Any, kind: Any = None) -> tuple[str, str]:
    """`clean_watch`, with the box it was typed into taken into account.

    The screen has two boxes now: one for names and subjects, one for the
    websites we always check. The TEXT still decides what a thing is, because a
    link pasted into the names box is a website whatever box it landed in, and
    it is filed as one. The box adds exactly one thing: a refusal. A name typed
    into the websites box would otherwise be followed as a subject, silently,
    and the person would wait for stories from a site that was never searched.
    """
    got_kind, value = clean_watch(raw)
    wanted = str(kind or "").strip().lower()
    if wanted == "site" and got_kind != "site":
        # The names box guesses from the ending, because "Node.js" is a name.
        # The websites box does not have to guess: anything shaped like a host
        # was meant as one, endings the list has never heard of included.
        head = _host_head(value)
        if " " not in value and _HOST_RE.match(head):
            return "site", head
        raise ValueError(
            "That does not look like a website address. Type it like theverge.com."
        )
    return got_kind, value


def watch_sites(rows: Iterable[dict]) -> list[str]:
    """The websites we always check, as bare hosts, in list order.

    Pure. These get a search of their OWN (`news_search.find_stories(sites=)`)
    rather than a sentence in the open search's brief: a sentence asking the
    model to prefer a site is a hope, and a person who added theverge.com wants
    to see something from The Verge.
    """
    out: list[str] = []
    for r in rows or []:
        if (r or {}).get("kind") != "site":
            continue
        value = str((r or {}).get("value") or "").strip().lower()
        if value and value not in out:
            out.append(value)
    return out


async def list_watch() -> list[dict]:
    """Everything we follow. Names first, then sites, oldest first within each."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, kind, value, created_at FROM news_watch "
                "ORDER BY (kind = 'site'), created_at, id"
            )
            return [dict(r) for r in await cur.fetchall()]


async def add_watch(raw: Any, kind: Any = None) -> dict:
    """Follow one more thing. Raises ValueError with a sentence a person reads.

    `kind` is the box it was typed into, or None from a caller with no boxes;
    see `resolve_watch` for the one thing it changes.

    The duplicate check is the unique index on lower(value) rather than a SELECT
    first: two people adding "Anthropic" at once would both pass the SELECT.
    """
    kind, value = resolve_watch(raw, kind)
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) AS n FROM news_watch")
            if int((await cur.fetchone())["n"]) >= MAX_WATCH_ROWS:
                raise ValueError(
                    f"That is {MAX_WATCH_ROWS} things already. Remove one first: a list "
                    "longer than this makes every search vaguer, not better."
                )
            await cur.execute(
                "INSERT INTO news_watch (kind, value) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING RETURNING id, kind, value, created_at",
                (kind, value),
            )
            row = await cur.fetchone()
    if row is None:
        raise ValueError(f"We already follow {value}.")
    return dict(row)


async def remove_watch(watch_id: Any) -> bool:
    """Stop following one thing. False if it was already gone."""
    try:
        wid = int(watch_id)
    except (TypeError, ValueError):
        return False
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM news_watch WHERE id = %s RETURNING id", (wid,))
            return await cur.fetchone() is not None


def watch_text(rows: Iterable[dict]) -> str:
    """The follow list as the instruction the search and the judge both read.

    Pure, so a test can read it without a database. Empty in, empty out: the
    caller falls back to the stored brief rather than searching for nothing.

    The sentence attached to the names is the load-bearing part. "Anthropic" on
    its own returns Anthropic's own announcements, which is the failure the
    brief-writing prompt warns about; asking what they DID and what it lets
    people do is what turns a company name back into news.
    """
    names, sites = [], []
    for r in rows or []:
        (sites if (r or {}).get("kind") == "site" else names).append(
            str((r or {}).get("value") or "").strip()
        )
    names = [n for n in names if n]
    sites = [s for s in sites if s]

    parts: list[str] = []
    if names:
        parts.append(
            "FOLLOW THESE. Each one is a company, a person or a subject this team "
            "cares about. For a company or a person, report what they actually DID "
            "and what it lets people do, never their own announcements about "
            "themselves:\n" + "\n".join(f"- {n}" for n in names)
        )
    if sites:
        parts.append(
            "THESE SITES ARE WORTH READING. Prefer a story from one of them when it "
            "is as good as the alternatives. Never limit the search to them, and "
            "never pad with a weak story just because it is on one:\n"
            + "\n".join(f"- {s}" for s in sites)
        )
    return "\n\n".join(parts)


async def outlets(limit: int = 60) -> list[dict]:
    """Where the stories we kept actually came from, newest first.

    This is the RECORD, not the configuration. The websites we always check are
    the `site` rows of the follow list (`watch_sites`) and they get their own
    search; the open search then looks across the whole web on top. So the
    truthful answer to "where do the stories come from" has two halves, the
    list we read every time and this tally of everything that actually
    arrived. Measured on 2026-09-20, before the fixed list existed: 22 outlets
    across 23 stories, almost all seen exactly once, the shape of an open
    search.
    """
    try:
        n = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        n = 60
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT platform AS outlet, count(*) AS stories, "
                "       max(posted_at) AS last_seen "
                "FROM trend_posts "
                "WHERE source = 'news' AND coalesce(platform, '') <> '' "
                "GROUP BY platform ORDER BY max(posted_at) DESC NULLS LAST "
                "LIMIT %s",
                (n,),
            )
            return [dict(r) for r in await cur.fetchall()]


async def start_run(*, trigger: str = "auto") -> int:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO news_runs (status, trigger) VALUES ('running', %s) "
                "RETURNING id",
                (str(trigger)[:20],),
            )
            row = await cur.fetchone()
    return int(row["id"])


async def finish_run(run_id: int, *, status: str, found: int = 0, kept: int = 0,
                     drops: Optional[dict] = None,
                     error: Optional[str] = None) -> None:
    """Close a run row. Nothing else: there is no schedule to move on."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE news_runs SET status = %s, found = %s, kept = %s, "
                "drops = %s, error = %s, finished_at = now() WHERE id = %s",
                (str(status)[:40], int(found), int(kept),
                 Jsonb(drops or {}), (error or None), int(run_id)),
            )


async def cancel_running() -> int:
    """Close whatever is in flight as `cancelled`. Returns how many rows moved.

    Deliberately closes EVERY open row rather than one id. The screen's Stop
    button means "whatever is going on, stop", and a second stranded row from an
    earlier crash would otherwise keep the button locked after the cancel
    appeared to work.
    """
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE news_runs SET status = 'cancelled', finished_at = now() "
                " WHERE status = 'running'"
            )
            return cur.rowcount or 0


async def fail_stale_runs() -> int:
    """Close runs that were killed mid-flight. Returns how many.

    A run row is opened before the work starts and closed after, so a backend
    that dies in between leaves it `running` for ever. That is not cosmetic: the
    screen reads "a search is running" off exactly this, so one killed run left
    the button disabled behind a spinner that never stopped, which is what the
    owner saw and reported as "it was rolling for sooo long but nothing is
    happening".
    """
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE news_runs SET status = 'search_failed', "
                "       finished_at = now(), "
                "       error = COALESCE(error, 'the backend restarted while this "
                "was running') "
                " WHERE status = 'running' "
                "   AND started_at < now() - %s::interval",
                (f"{STALE_RUN_MINUTES} minutes",),
            )
            return cur.rowcount or 0


async def recent_runs(limit: int = 14) -> list[dict]:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, status, found, kept, drops, error, trigger, "
                "started_at, finished_at FROM news_runs "
                "ORDER BY started_at DESC LIMIT %s",
                (int(limit),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def prune_runs() -> int:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM news_runs WHERE started_at < now() - %s::interval",
                (f"{RUN_LOG_DAYS} days",),
            )
            return cur.rowcount or 0


async def health() -> dict:
    """Everything the Configure screen and the feed's status line need, in one read.

    Never raises for a caller: the route wraps it, and a status line that 500s is
    worse than one that says it does not know.
    """
    cfg = await get_config()
    runs = await recent_runs(limit=14)
    last_done = next((r for r in runs if r["status"] != "running"), None)
    return {
        "enabled": bool(cfg["enabled"]),
        "brief": cfg["brief"],
        # The number the button's own control starts on. It is remembered
        # between visits so nobody has to re-pick the same cap every time, but
        # it schedules nothing.
        "stories_per_day": int(cfg["stories_per_day"]),
        "ever_ran": bool(last_done),
        "last_run": last_done,
        "running": any(r["status"] == "running" for r in runs),
        "runs": runs,
    }
