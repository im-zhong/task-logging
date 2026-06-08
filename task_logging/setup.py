"""Top-level wiring: install handlers + filters on the root logger.

Call `setup_task_logging()` once at process startup. Everything that uses
stdlib `logging` afterwards — your code, `requests`, `urllib3`, `boto3`, … —
will be captured and written to stdout as JSON, ready for Alloy / Loki
ingestion.

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
    With Alloy: `discovery.docker` (or `loki.source.kubernetes`) tails
    container stdout directly. No app-level file mounts. Multiple replicas of
    the same service show up as separate Loki streams automatically via
    container labels.

Why ALWAYS JSON (no human-readable mode):
    Two output formats means two code paths to test, two schemas to document,
    and recurring "is the field there or not?" questions when reading logs.
    With one format, what you see in Grafana is exactly what you see when you
    inspect locally. For human reading at a terminal, `docker logs <ctr> | jq`
    or any JSON pretty-printer is one pipe away — and shows the structured
    fields (e.g. `task_id`, `exc_info.locals_dict`, ...) that a "pretty"
    formatter would have hidden.

Why no domain-specific kwargs (no `service=`, no `env=`):
    Earlier revisions had `setup_logging(service=..., env=...)`. We dropped
    those: a logging library shouldn't decide what fields its users care
    about. `global_log_attrs={...}` accepts any dict, with `service` /
    `env` / `region` / whatever as conventions the user picks, not names
    the library bakes in.

See docs/design/why-json-logs.md for the format choice and the README's
"Deployment" section for the Alloy config. See
docs/design/stdlib-logging-primer.md for the stdlib mechanics this file
relies on.
"""

from __future__ import annotations

import logging
import sys
from typing import IO, Any

from .filters import TaskLogFilter
from .formatters import JsonFormatter

# A stable name we attach to handlers we install, so repeated
# setup_task_logging() calls (e.g. in tests) replace rather than stack.
_HANDLER_TAG = "task_logging.handler"


def setup_task_logging(
    *,
    global_log_attrs: dict[str, Any] | None = None,
    level: int | str = logging.INFO,
    stream: IO[str] | None = None,
    capture_locals: bool = True,
    quiet_loggers: dict[str, int] | None = None,
) -> logging.Logger:
    """Configure the root logger to write JSON to stdout for Alloy / Loki.

    Args:
        global_log_attrs:
            A dict of attrs stamped on every record for the lifetime of the
            process. Pick whatever keys your domain wants — e.g.
            ``{"service": "OrderService", "env": "prod", "region": "us"}``.
            Any key colliding with a stdlib `LogRecord` attribute (or
            `hostname`, which we auto-detect) is silently ignored to protect
            record integrity.
        level:
            Root log level. Stdlib levels (`logging.INFO`) or names ("INFO").
        stream:
            The stream to write to. Defaults to `sys.stdout`. Override for
            tests or unusual deployments (e.g. a `StringIO` in tests).
        capture_locals:
            If True, exception logs include a repr-snapshot of the local
            variables at the deepest frame. Disable in low-trust environments
            where locals might leak secrets.
        quiet_loggers:
            Map of logger name -> level. Useful for taming chatty third-party
            libraries, e.g. `{"urllib3": logging.WARNING}`.

    Returns:
        The configured root logger.

    Example:
        >>> setup_task_logging(
        ...     global_log_attrs={"service": "OrderService", "env": "prod"},
        ...     quiet_loggers={"urllib3": logging.WARNING},
        ... )

        For human-readable output at a terminal, pipe through `jq`:

            $ python -m myapp | jq
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Idempotency: drop handlers we previously installed (identified by our
    # private tag) and leave foreign ones alone. Without this, calling
    # setup_task_logging() twice (in tests, hot-reloads, multi-step
    # bootstrap) would stack handlers and produce duplicated log lines. We
    # don't blanket-remove all handlers because a host application may have
    # legitimately installed its own (e.g. Sentry's breadcrumb handler).
    for h in list(root.handlers):
        if getattr(h, "_task_logging_tag", None) == _HANDLER_TAG:
            root.removeHandler(h)
            h.close()

    target_stream = stream if stream is not None else sys.stdout

    ctx_filter = TaskLogFilter(global_log_attrs=global_log_attrs)
    formatter = JsonFormatter(capture_locals=capture_locals)

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
