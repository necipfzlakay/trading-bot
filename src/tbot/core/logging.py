"""Structured logging: human-readable on console, JSONL on disk.

The JSONL files are a first-class interface, not a debugging afterthought. Every
decision the bot makes is reconstructable from them after the fact, which is
what makes "why did it do that?" answerable by query instead of by guessing.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

_configured = False
_file_configured = False


def configure(
    log_dir: Path | None = None,
    level: str = "INFO",
    console: bool = True,
    json_file: str = "bot.jsonl",
    max_bytes: int = 50_000_000,
    backup_count: int = 5,
) -> None:
    """Set up structlog. Safe to call again.

    Re-entrant on purpose. Module-level ``get_logger()`` calls run at import
    time, before any CLI has read the config, and each one configures logging
    with no ``log_dir``. Treating that first call as final meant the file
    handler was never attached: every event went to stdout and ``bot.jsonl``
    stayed empty, which quietly removed the log that the whole observability
    story depends on. So a later call carrying a ``log_dir`` is allowed to
    upgrade the configuration.
    """
    global _configured, _file_configured
    if _configured and (log_dir is None or _file_configured):
        return

    handlers: list[logging.Handler] = []

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        # Rotating, not plain: a bot that runs for months writes JSONL
        # continuously, and an unbounded log eventually fills the disk that the
        # database also lives on.
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / json_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=_shared_processors(),
            )
        )
        handlers.append(file_handler)

    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
                foreign_pre_chain=_shared_processors(),
            )
        )
        handlers.append(stream_handler)

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ccxt and websockets are chatty at DEBUG and drown out our own events.
    for noisy in ("ccxt", "websockets", "urllib3", "asyncio", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    _file_configured = log_dir is not None


def _shared_processors() -> list[Any]:
    return [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]


def get_logger(name: str = "tbot") -> structlog.stdlib.BoundLogger:
    """Module-level logger. Bind context with ``.bind(symbol=..., bar_ts=...)``."""
    if not _configured:
        configure()
    return structlog.get_logger(name)
