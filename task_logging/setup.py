"""Top-level wiring: install handlers + filters on the root logger.

Call `setup_logging()` once at process startup. Everything that uses stdlib
`logging` afterwards — your code, `requests`, `urllib3`, `boto3`, … — will be
captured and written as JSON to the configured log file (and optionally to the
console for humans).

Why install handlers on the ROOT logger (not on a per-module logger):
    stdlib propagation walks records UP the logger tree until it hits a
    handler. Installing handlers only on the root means every record emitted
    anywhere in the process — yours and third-party — flows through exactly
    one handler, gets enriched by exactly one filter, and is formatted by
    exactly one formatter. One source of truth for log shape.

Why ship to a FILE for Alloy to tail (not push to Loki directly):
    Loki accepts pushes via HTTP, but writing files and letting Alloy ship
    them is the Grafana-recommended path:
      - the app stays trivial (FileHandler + Formatter, no HTTP, no async
        queue, no retry logic)
      - if Loki is down or slow, the file is the buffer; Alloy handles
        backpressure and reliability with WAL
      - if the app crashes, every flushed line is on disk
      - one Alloy process can ship logs from many services on the same host
    See docs/design/why-json-logs.md for the format choice and the README's
    "Deployment" section for the Alloy config.

See docs/design/stdlib-logging-primer.md for the stdlib mechanics this
file relies on.
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

    # Idempotency: drop handlers we previously installed (identified by our
    # private tag) and leave foreign ones alone. Without this, calling
    # setup_logging() twice (in tests, hot-reloads, multi-step bootstrap)
    # would stack handlers and produce duplicated log lines. We don't blanket-
    # remove all handlers because a host application may have legitimately
    # installed its own (e.g. Sentry's breadcrumb handler).
    for h in list(root.handlers):
        if getattr(h, "_task_logging_tag", None) == _HANDLER_TAG:
            root.removeHandler(h)
            h.close()

    # The filter and JSON formatter are SHARED between handlers — one
    # filter instance running once per record, one formatter instance
    # used by both handlers when console_json=True. Both are stateless
    # after construction so sharing is safe.
    ctx_filter = TaskContextFilter(service=service, env=env, extra=static_fields)
    json_formatter = JsonFormatter(capture_locals=capture_locals)

    if log_file is not None:
        path = Path(log_file)
        # Create the parent directory for the user — a missing log dir is the
        # single most common configuration error and a fail-fast that produces
        # `FileNotFoundError` at startup is worse than just creating it.
        path.parent.mkdir(parents=True, exist_ok=True)
        # RotatingFileHandler (size-based) is chosen over TimedRotatingFileHandler
        # because Alloy doesn't care about rotation cadence — it follows
        # rotated files via inode tracking — and size caps are a hard ceiling
        # against runaway disk usage. 100MiB × 5 = 500MiB worst case, fine for
        # a single service on a normal host.
        file_handler = RotatingFileHandler(
            filename=path,
            maxBytes=rotate_max_bytes,
            backupCount=rotate_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(json_formatter)
        # Filter on the HANDLER, not on the root logger. Logger-filters only
        # see records emitted directly on that logger, not propagated child
        # records. Handler-filters see every record that reaches the handler,
        # which is what we want for whole-process enrichment.
        file_handler.addFilter(ctx_filter)
        _tag(file_handler)
        root.addHandler(file_handler)

    if console:
        # Console writes to stderr (stdlib convention for diagnostic output;
        # keeps stdout clean for the program's "real" output).
        console_handler = logging.StreamHandler(stream=sys.stderr)
        # Default to a human-readable formatter on the console — JSON is for
        # machines (Alloy reads the file), text is for humans (you read the
        # terminal). Toggle `console_json=True` if you also want a machine-
        # readable stdout for, e.g., Kubernetes log shipping where the kubelet
        # captures stdout into /var/log/containers and Alloy tails THAT.
        if console_json:
            console_handler.setFormatter(json_formatter)
        else:
            console_handler.setFormatter(_HumanFormatter())
        console_handler.addFilter(ctx_filter)
        _tag(console_handler)
        root.addHandler(console_handler)

    if quiet_loggers:
        # Setting a level on a parent logger silences every child below it
        # via stdlib's effective-level inheritance. So "urllib3"=WARNING
        # also silences "urllib3.connectionpool", "urllib3.util", etc.
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
