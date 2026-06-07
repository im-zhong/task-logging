"""Top-level wiring: install handlers + filters on the root logger.

Call `setup_logging()` once at process startup. Everything that uses stdlib
`logging` afterwards — your code, `requests`, `urllib3`, `boto3`, … — will be
captured and written to stdout (as JSON for Alloy, or as human-readable text
when running interactively).

Why install handlers on the ROOT logger (not on a per-module logger):
    stdlib propagation walks records UP the logger tree until it hits a
    handler. Installing handlers only on the root means every record emitted
    anywhere in the process — yours and third-party — flows through exactly
    one handler, gets enriched by exactly one filter, and is formatted by
    exactly one formatter. One source of truth for log shape.

Why write to STDOUT (not a file):
    The library targets containerised deployments, where the platform — Docker
    daemon, Kubernetes kubelet, systemd-journald, an Alloy DaemonSet — already
    captures stdout into a durable, rotated location. Writing files from the
    app would just duplicate that work and add per-service mounts, log-dir
    permissions, and rotation tuning to the deployment surface. The 12-factor
    rule (https://12factor.net/logs) is "treat logs as event streams; the app
    writes unbuffered to stdout, the environment routes them."
    With Alloy: `discovery.docker` (or `loki.source.kubernetes`) tails container
    stdout directly. No app-level file mounts. Multiple replicas of the same
    service show up as separate Loki streams automatically via container labels.

Why JSON when not at a TTY, human-readable when at one:
    A developer running `python -m myapp` at a terminal wants readable output;
    a container in production wants JSON for Alloy's `stage.json`. We detect
    which one we're in via `stdout.isatty()` by default, and let the user
    force it either way with `json_format=True` / `False`.

See docs/design/why-json-logs.md for the format choice and the README's
"Deployment" section for the Alloy config. See docs/design/stdlib-logging-primer.md
for the stdlib mechanics this file relies on.
"""

from __future__ import annotations

import logging
import sys
from typing import IO, Any

from .filters import TaskContextFilter
from .formatters import JsonFormatter

# A stable name we attach to handlers we install, so repeated `setup_logging()`
# calls (e.g. in tests) replace rather than stack.
_HANDLER_TAG = "task_logging.handler"


def setup_logging(
    *,
    service: str,
    env: str | None = None,
    level: int | str = logging.INFO,
    json_format: bool | None = None,
    stream: IO[str] | None = None,
    capture_locals: bool = True,
    static_fields: dict[str, Any] | None = None,
    quiet_loggers: dict[str, int] | None = None,
) -> logging.Logger:
    """Configure the root logger to write to stdout for Alloy / Loki ingestion.

    Args:
        service:
            Service name. Becomes a Loki label via Alloy and is stamped on
            every log record. **Use a low-cardinality value** (e.g.
            "OrderService"), not something per-request.
        env:
            Optional environment label, e.g. "prod" / "dev".
        level:
            Root log level. Stdlib levels (`logging.INFO`) or names ("INFO").
        json_format:
            How to render each record:
                - True  → always JSON (use this in containers / production).
                - False → always human-readable text (use at a terminal).
                - None (default) → auto-detect: JSON when stdout is not a
                  TTY (containers, pipes), text when it is (interactive
                  terminals).
        stream:
            The stream to write to. Defaults to `sys.stdout`. Override for
            tests or unusual deployments where you want the records routed
            elsewhere (e.g. a `StringIO` in tests).
        capture_locals:
            If True, exception logs include a repr-snapshot of the local
            variables at the deepest frame. Disable in low-trust environments
            where locals might leak secrets.
        static_fields:
            Extra fields stamped on every record (e.g. `{"region": "us-west-2"}`).
        quiet_loggers:
            Map of logger name -> level. Useful for taming chatty third-party
            libraries, e.g. `{"urllib3": logging.WARNING}`.

    Returns:
        The configured root logger.

    Example:
        Production (in a Docker container) — stdout is not a TTY, so we
        emit JSON automatically::

            setup_logging(
                service="OrderService",
                env="prod",
                quiet_loggers={"urllib3": logging.WARNING},
            )

        Development at a terminal — auto-detected human-readable text. Or
        force one format explicitly::

            setup_logging(service="OrderService", json_format=False)
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

    target_stream = stream if stream is not None else sys.stdout

    # Auto-detect JSON vs human format from TTY status if not explicitly set.
    # `isatty()` may not exist on every stream-like object (e.g. StringIO in
    # tests has it; arbitrary file-likes might not), so guard with getattr.
    if json_format is None:
        isatty = getattr(target_stream, "isatty", None)
        json_format = not (callable(isatty) and isatty())

    ctx_filter = TaskContextFilter(service=service, env=env, extra=static_fields)
    formatter: logging.Formatter = (
        JsonFormatter(capture_locals=capture_locals)
        if json_format
        else _HumanFormatter()
    )

    handler = logging.StreamHandler(stream=target_stream)
    handler.setFormatter(formatter)
    # Filter on the HANDLER, not on the root logger. Logger-filters only see
    # records emitted directly on that logger, not propagated child records.
    # Handler-filters see every record that reaches the handler, which is
    # what we want for whole-process enrichment.
    handler.addFilter(ctx_filter)
    _tag(handler)
    root.addHandler(handler)

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
    """A compact, human-friendly format that surfaces the task_id."""

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
