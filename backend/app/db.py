"""Async Postgres connection pool with pgvector type adapter.

One pool per process, opened in main.lifespan and closed on shutdown.

- All long-lived FastAPI requests acquire a connection via `async with pool.connection() as conn`.
- pgvector binding is registered on each new connection via configure callback.
- dict_row factory: queries return dicts (matches the legacy psycopg sync style).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import config

log = logging.getLogger("archivist.db")

_pool: AsyncConnectionPool | None = None


def _dsn() -> str:
    # TCP keepalives + a connect timeout are essential when the DB is a remote
    # Supabase session pooler reached over the internet: the pooler/NAT silently
    # drops idle connections, and WITHOUT keepalives a query on a now-dead socket
    # hangs forever — pinning a pooled connection until the whole pool is wedged
    # (the `PoolTimeout after 30s` death spiral). keepalives make the OS notice
    # the dead peer and error the query instead; connect_timeout caps a stuck
    # initial connect.
    return (
        f"host={config.db_host} "
        f"port={config.db_port} "
        f"dbname={config.db_name} "
        f"user={config.db_user} "
        f"password={config.db_password} "
        f"sslmode={config.db_sslmode} "
        f"connect_timeout=10 "
        f"keepalives=1 keepalives_idle=30 keepalives_interval=10 keepalives_count=5"
    )


async def _configure_connection(conn: psycopg.AsyncConnection) -> None:
    """Per-connection setup: dict rows + statement_timeout + pgvector binding.

    pgvector binding is mandatory for cols like website_pages.embedding. The
    register_vector call has to run after the connection is open.
    """
    conn.row_factory = dict_row

    # Cap how long any single query may run on this connection. Without it a slow
    # query (e.g. an un-indexed vector KNN scan) can hold a pooled connection for
    # tens of seconds and — under load — drain the whole pool, surfacing as
    # `PoolTimeout: couldn't get a connection after 30s` elsewhere. Set as the
    # SESSION default and committed so it survives the per-use rollback that the
    # pool does on connection return. 0 disables. Callers that genuinely need a
    # long query can raise it locally with `SET LOCAL statement_timeout`.
    timeout_ms = int(getattr(config, "db_statement_timeout_ms", 0) or 0)
    if timeout_ms > 0:
        try:
            async with conn.cursor() as cur:
                await cur.execute(f"SET statement_timeout = {timeout_ms}")
            await conn.commit()
        except Exception:
            log.exception("Failed to set statement_timeout")

    try:
        from pgvector.psycopg import register_vector_async
        await register_vector_async(conn)
    except ImportError:
        log.warning("pgvector not installed; vector columns will return as strings")
    except Exception:
        log.exception("Failed to register pgvector adapter")


async def open_pool() -> AsyncConnectionPool:
    """Open the shared pool. Idempotent (won't re-open if already open)."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = AsyncConnectionPool(
        conninfo=_dsn(),
        min_size=config.db_pool_min,
        max_size=config.db_pool_max,
        # prepare_threshold=None disables psycopg's automatic named prepared
        # statements, and it is REQUIRED because DB_PORT is Supavisor's
        # transaction-pooling port (6543) rather than session mode (5432).
        #
        # psycopg names its prepares per CLIENT connection: `_pg3_0`, `_pg3_1`,
        # and so on. Transaction pooling hands each transaction whichever
        # upstream server connection is free, so those names collide across
        # clients on a shared server: two different queries both believe they
        # are `_pg3_0`. The failures are not subtle and not rare. Measured on
        # this database with a 6-connection pool and 24 concurrent tasks running
        # two queries of different arity: 211 errors without this line, 0 with
        # it. They arrive as DuplicatePreparedStatement, InvalidSqlStatementName
        # ("prepared statement _pg3_0 does not exist"), and ProtocolViolation
        # ("bind message supplies 2 parameters, but prepared statement _pg3_0
        # requires 10").
        #
        # The reason this is a correctness bug and not just noise: one of the
        # observed failures was `invalid byte sequence for encoding "UTF8":
        # 0x00` on `unnamed portal parameter $2`. Parameters bound against the
        # WRONG statement can be reinterpreted rather than rejected, so this
        # could return or write wrong values, not merely raise.
        #
        # Cost: no statement reuse, so the server re-plans each query. That is
        # the accepted price of transaction pooling. If DB_PORT ever goes back
        # to 5432, this can be dropped, but leaving it on is harmless.
        kwargs={"autocommit": False, "prepare_threshold": None},
        configure=_configure_connection,
        # Recycle connections so a stale Supabase session-pooler connection is
        # retired rather than handed out dead: validate on acquire (check), drop
        # ones idle too long (max_idle) and cap total lifetime (max_lifetime).
        check=AsyncConnectionPool.check_connection,
        max_idle=300.0,        # 5 min — shrink idle connections back toward min_size
        max_lifetime=1800.0,   # 30 min — retire long-lived pooler connections
        open=False,
    )
    await _pool.open(wait=True, timeout=30)
    log.info(
        "DB pool open: %s@%s:%s/%s (min=%d max=%d)",
        config.db_user, config.db_host, config.db_port, config.db_name,
        config.db_pool_min, config.db_pool_max,
    )
    return _pool


# How long shutdown waits for the pool to drain before giving up on it.
CLOSE_POOL_TIMEOUT_SEC = 5.0


async def close_pool() -> None:
    """Close the pool, but never let shutdown hang on it.

    `pool.close()` waits for in-flight queries to finish. Against a remote
    Supabase pooler a query that cannot be cancelled ("cancellation timeout
    expired") never returns, so the process hangs with the port still bound and
    Ctrl+C does nothing, because the signal handler is already past the point of
    no return. The connections die with the process anyway, so a bounded wait
    loses nothing and always exits.
    """
    global _pool
    if _pool is None:
        return
    pool, _pool = _pool, None          # unpublish first: a slow close must not
    try:                               # leave callers holding a dying pool
        await asyncio.wait_for(pool.close(), timeout=CLOSE_POOL_TIMEOUT_SEC)
        log.info("DB pool closed")
    except asyncio.TimeoutError:
        log.warning(
            "DB pool did not close within %.0fs (a query is stuck upstream); "
            "abandoning it so shutdown can finish", CLOSE_POOL_TIMEOUT_SEC,
        )
    except Exception:
        log.warning("DB pool close failed; abandoning it", exc_info=True)


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call open_pool() first")
    return _pool


@asynccontextmanager
async def get_conn(*, autocommit: bool = False) -> AsyncIterator[psycopg.AsyncConnection]:
    """Acquire a pooled connection.

    Default (autocommit=False): one transaction spans the whole block — commit on
    clean exit, rollback on exception. Use for writes / multi-statement atomicity.

    autocommit=True: each statement commits immediately, so the connection is
    NEVER held "idle in transaction" between queries. Use for read-only endpoints.
    Supabase's session pooler terminates a backend left idle-in-transaction past
    its timeout; the next statement on it then 500s with
    IdleInTransactionSessionTimeout. That is exactly what hangs the asset picker:
    `list_assets` opens a transaction on its rows query, then sits idle while it
    maps ~500 rows before its second (totals) query — long enough, under a busy
    event loop, for the pooler to kill the connection. Autocommit removes the
    transaction entirely, so there is no idle-in-transaction window to kill. The
    flag is restored before the connection returns to the pool.
    """
    pool = get_pool()
    async with pool.connection() as conn:
        if autocommit:
            await conn.set_autocommit(True)
            try:
                yield conn
            finally:
                # Restore the pool default so the next (possibly writing) borrower
                # gets transactional semantics. Guarded: a truly-dead connection is
                # discarded by the pool on return regardless.
                try:
                    await conn.set_autocommit(False)
                except Exception:
                    log.exception("Failed to restore autocommit on pooled connection")
        else:
            try:
                yield conn
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise


async def ping() -> bool:
    """Connectivity probe.

    autocommit: the frontend polls /api/health constantly, and the transactional
    default made every probe cost four remote round trips (pool check + BEGIN +
    SELECT + COMMIT) instead of two — ~1.0s rather than ~0.5s against a pooler in
    another region.
    """
    try:
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 AS ok")
                row = await cur.fetchone()
                return bool(row and row.get("ok") == 1)
    except Exception:
        log.exception("DB ping failed")
        return False


async def cleanup_orphan_runs() -> int:
    """Flip any `started` runs to `failed` — they were running when the backend
    died, so they're zombies from the user's POV. Called once at startup.

    Returns the number of rows touched.
    """
    n = 0
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE scrape_runs "
                    "   SET status='failed', "
                    "       finished_at=COALESCE(finished_at, NOW()), "
                    "       notes=COALESCE(notes, '') || ' [orphaned at backend restart]' "
                    " WHERE status='started' "
                    " RETURNING id"
                )
                n += len(await cur.fetchall())
                await cur.execute(
                    "UPDATE brand_profile_runs "
                    "   SET status='failed', "
                    "       finished_at=COALESCE(finished_at, NOW()), "
                    "       notes=COALESCE(notes, '') || ' [orphaned at backend restart]' "
                    " WHERE status='started' "
                    " RETURNING id"
                )
                n += len(await cur.fetchall())
        if n:
            log.info("Cleanup: marked %d orphan run(s) as failed", n)
    except Exception:
        log.exception("Orphan-run cleanup failed (non-fatal)")
    return n
