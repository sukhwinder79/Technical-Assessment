"""Structured logging (structlog).

Two sinks, always:
  * stdout  — pretty and colourised for demos, or JSON lines when LOG_JSON=true
  * a file  — always JSON lines, one file per day, under LOG_DIR

Every log line carries the `run_id` once `bind_run` has been called, so a single
run can be grepped out of a busy log with `jq 'select(.run_id=="...")'`.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import date
from pathlib import Path

import structlog

_CONFIGURED = False


def configure_logging(log_dir: Path, level: str = "INFO", json_console: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"agent-{date.today().isoformat()}.jsonl"

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console_renderer = (
        structlog.processors.JSONRenderer()
        if json_console
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, console_renderer],
        )
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(level.upper())

    # Third-party chatter we never want in a demo.
    for noisy in ("httpx", "httpcore", "anthropic", "googleapiclient.discovery_cache", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def bind_run(run_id: str, **extra) -> None:
    """Attach a run_id (and anything else) to every subsequent log line."""
    structlog.contextvars.bind_contextvars(run_id=run_id, **extra)


def clear_run() -> None:
    structlog.contextvars.clear_contextvars()


def get_logger(name: str):
    return structlog.get_logger(name)
