"""Logging filters that enrich `LogRecord` with task context and service info.

Why a Filter (not a custom Logger subclass, not setLogRecordFactory):
    A logging.Filter on a *handler* runs for EVERY record that reaches the
    handler, no matter which logger emitted it. That's exactly what we want:
    install one filter on the root handler and every record in the process —
    yours, requests, urllib3, boto3 — gets enriched.

    A filter on a *logger* would only see records emitted directly on that
    logger; child-logger records that propagate up are NOT subject to it.
    That asymmetry is documented in stdlib but trips most people up.

    setLogRecordFactory works too but is a process-global mutation that's
    hard to reverse and tests can't isolate cleanly. A handler-level filter
    is local and reversible (setup_logging() can replace its own handlers).

See docs/design/stdlib-logging-primer.md (§4) for the full reasoning.
"""

from __future__ import annotations

import logging
import socket
from typing import Any

from .context import get_task_context

# Cached at module import. hostname is effectively immutable for the life
# of the process; PID we don't need to stamp because stdlib already sets
# `record.process` for us.
_HOSTNAME = socket.gethostname()


class TaskContextFilter(logging.Filter):
    """Attach task context + service metadata to every `LogRecord`.

    The filter never drops records (always returns True). It only enriches.

    Fields written onto the record (all NEW — none of these exist on a
    stock LogRecord; PID/process info comes from stdlib's record.process):
        - `service`:  the service name passed to `setup_logging`
        - `env`:      the environment name (e.g. "prod", "dev"); may be None
        - `hostname`: the machine hostname
        - `task_id`:  the current task id, or None
        - plus every extra field bound via `task_context(**extra)`
    """

    def __init__(
        self, service: str, env: str | None = None, extra: dict[str, Any] | None = None
    ) -> None:
        super().__init__()
        self._service = service
        self._env = env
        self._extra: dict[str, Any] = dict(extra) if extra else {}

    def filter(self, record: logging.LogRecord) -> bool:
        # Static fields known at setup time. We deliberately do NOT set
        # record.pid — stdlib already populates record.process with the PID,
        # and adding our own duplicate field under a different name would
        # contradict the principle "JSON keys mirror LogRecord names."
        record.service = self._service
        record.env = self._env
        record.hostname = _HOSTNAME

        # Pull whatever the active task_context() bound. ContextVar lookup is
        # cheap (a dict get on a per-thread/task variable); doing it on every
        # record is fine.
        ctx = get_task_context()
        record.task_id = ctx.get("task_id")

        # Merge static extras (lowest priority) and dynamic context fields
        # (highest priority). Skip reserved LogRecord attributes so a
        # well-meaning user can't accidentally clobber `record.msg` or
        # `record.levelname` by binding e.g. task_context(msg="...").
        for key, value in {**self._extra, **ctx}.items():
            if key in _RESERVED_LOGRECORD_ATTRS:
                continue
            setattr(record, key, value)

        # Filters can return False to drop a record. We never drop — this
        # filter is purely an enricher, and dropping logs based on context
        # state would be a surprising behaviour for callers to debug.
        return True


# Attributes that already exist on a LogRecord — never clobber these.
_RESERVED_LOGRECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        # Fields we set ourselves above.
        "service",
        "env",
        "hostname",
        "task_id",
    }
)
