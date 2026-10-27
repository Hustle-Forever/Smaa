"""Single-source logging setup. Call once at process start."""
from __future__ import annotations

import logging

from .config import config


class _HealthCheckAccessFilter(logging.Filter):
    """Drop uvicorn access-log lines for the liveness probe.

    The Docker HEALTHCHECK curls ``/api/health`` every 30s (backend/Dockerfile);
    those 200s bury every genuinely useful access log. uvicorn.access formats each
    line from ``record.args = (client_addr, method, full_path, http_version,
    status_code)`` — so we inspect the path (index 2) and suppress ONLY the health
    probe. The isinstance/len guards fail safe (keep the line) if a future uvicorn
    changes that tuple shape.
    """

    _PATH = "/api/health"

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = args[2]
            if isinstance(path, str) and path.split("?", 1)[0] == self._PATH:
                return False  # drop this record
        return True  # keep everything else


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Tone down noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # Silence the constant /api/health probe (the Docker healthcheck, every 30s)
    # while keeping every other access-log line intact. Attached to the logger
    # (not a handler) so it survives uvicorn's own dictConfig.
    logging.getLogger("uvicorn.access").addFilter(_HealthCheckAccessFilter())
