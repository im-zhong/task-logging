"""Top-level wiring: install handlers + filters on the root logger.

Call `setup_logging()` once at process startup. Everything that uses stdlib
`logging` afterwards — your code, `requests`, `urllib3`, `boto3`, … — will be
captured and written as JSON to the configured log file (and optionally to the
console for humans).
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .filters import TaskContextFilter
from .formatters import JsonFormatter

# A stable name we attach to handlers we install, so repeated `setup_logging()`
# calls (e.g. in tests) replace rather than stack.
_HANDLER_TAG = "task_logging.handler"


def setup_logging(
    *,
    service: str,
    log_file: str | os.PathLike[str] | None = None,
    env: str | None = None,
    level: int | str = logging.INFO,
    console: bool = True,
    console_json: bool = False,
    capture_locals: bool = True,
    rotate_max_bytes: int = 100 * 1024 * 1024,
    rotate_backup_count: int = 5,
    static_fields: dict[str, Any] | None = None,
    quiet_loggers: dict[str, int] | None = None,
) -> logging.Logger:
    """Configure the root logger for Loki / Alloy ingestion.

    Args:
        service:
            Service name. Becomes a Loki label via Alloy and is stamped on
            every log record. **Use a low-cardinality value** (e.g.
            "OrderService"), not something per-request.
        log_file:
            Path to the JSON log file Alloy will tail. Required for shipping
            to Loki. If None, no file handler is installed (useful in tests).
            Parent directory is created automatically.
        env:
            Optional environment label, e.g. "prod" / "dev".
        level:
            Root log level. Stdlib levels (`logging.INFO`) or names ("INFO").
        console:
            If True, also write to stderr. Default True.
        console_json:
            If True, the console handler emits JSON too. If False (default),
            it emits a human-readable single-line format. The file handler
            is always JSON.
        capture_locals:
            If True, exception logs include a repr-snapshot of the local
            variables at the deepest frame. Disable in low-trust environments
            where locals might leak secrets.
        rotate_max_bytes / rotate_backup_count:
            `RotatingFileHandler` knobs. Defaults: 100 MiB × 5.
        static_fields:
            Extra fields stamped on every record (e.g. `{"region": "us-west-2"}`).
        quiet_loggers:
            Map of logger name -> level. Useful for taming chatty third-party
            libraries, e.g. `{"urllib3": logging.WARNING}`.

    Returns:
        The configured root logger.

    Example:
        >>> setup_logging(
        ...     service="OrderService",
        ...     log_file="/var/log/order-service/app.log",
        ...     env="prod",
        ...     quiet_loggers={"urllib3": logging.WARNING},
        ... )
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Drop any handlers we previously installed; leave foreign ones alone.
    for h in list(root.handlers):
        if getattr(h, "_task_logging_tag", None) == _HANDLER_TAG:
            root.removeHandler(h)
            h.close()

    ctx_filter = TaskContextFilter(service=service, env=env, extra=static_fields)
    json_formatter = JsonFormatter(capture_locals=capture_locals)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=path,
            maxBytes=rotate_max_bytes,
            backupCount=rotate_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(json_formatter)
        file_handler.addFilter(ctx_filter)
        _tag(file_handler)
        root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        if console_json:
            console_handler.setFormatter(json_formatter)
        else:
            console_handler.setFormatter(_HumanFormatter())
        console_handler.addFilter(ctx_filter)
        _tag(console_handler)
        root.addHandler(console_handler)

    if quiet_loggers:
        for name, lvl in quiet_loggers.items():
            logging.getLogger(name).setLevel(lvl)

    return root


def _tag(handler: logging.Handler) -> None:
    handler._task_logging_tag = _HANDLER_TAG  # type: ignore[attr-defined]  # noqa: SLF001


class _HumanFormatter(logging.Formatter):
    """A compact, human-friendly console format that surfaces the task_id."""

    def format(self, record: logging.LogRecord) -> str:
        task_id = getattr(record, "task_id", None)
        prefix = f"[{task_id}] " if task_id else ""
        base = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"{record.levelname:<8} "
            f"{record.name} "
            f"{prefix}{record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base
