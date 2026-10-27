"""Programmatic uvicorn launcher.

Why a custom launcher? On Windows, psycopg async refuses to run on the
ProactorEventLoop (the platform default). Uvicorn's `loop="asyncio"` option
*overwrites* the policy with WindowsProactorEventLoopPolicy on Windows,
which defeats setting WindowsSelectorEventLoopPolicy before importing it.

So: we set the Selector policy, then use uvicorn.Server directly with
`loop="none"` (which tells uvicorn not to touch the policy/loop), and run
the server via asyncio.run() — which honours OUR policy.

Set AUTO_RELOAD=1 in backend/.env to enable Python hot-reload (uvicorn
watches `app/` and re-execs on change). When reload is on, each reloaded
child re-imports `app.main` which sets the Selector policy at its module
top, so psycopg async stays happy. The trade-off: reload mode rebuilds
the DB pool on every change, which costs a few seconds per save.

Usage:
    python run.py
"""
from __future__ import annotations

import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402

from app.config import config  # noqa: E402


def _auto_reload_enabled() -> bool:
    raw = (os.getenv("AUTO_RELOAD") or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "y", "t"}


def main() -> None:
    if _auto_reload_enabled():
        # Reload mode: uvicorn forks a watcher + a worker. The worker re-imports
        # `app.main`, whose top-of-module sys.platform check restores the
        # Selector policy before psycopg ever runs.
        uvicorn.run(
            "app.main:app",
            host=config.api_host,
            port=config.api_port,
            log_level=config.log_level.lower(),
            reload=True,
            reload_dirs=["app"],
            access_log=True,
        )
        return

    cfg = uvicorn.Config(
        "app.main:app",
        host=config.api_host,
        port=config.api_port,
        log_level=config.log_level.lower(),
        loop="none",       # don't let uvicorn override our policy
        access_log=True,
    )
    server = uvicorn.Server(cfg)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
