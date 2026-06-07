"""Logging filters that enrich `LogRecord` with task context and service info."""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

from .context import get_task_context

_HOSTNAME = socket.gethostname()
_PID = os.getpid()


class TaskContextFilter(logging.Filter):
    """Attach task context + service metadata to every `LogRecord`.

    The filter never drops records (always returns True). It only enriches.

    Fields written onto the record:
        - `service`:  the service name passed to `setup_logging`
        - `env`:      the environment name (e.g. "prod", "dev"); may be None
        - `hostname`: the machine hostname
        - `pid`:      the process id
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
        record.service = self._service
        record.env = self._env
        record.hostname = _HOSTNAME
        record.pid = _PID

        # Pull whatever the active task_context() bound.
        ctx = get_task_context()
        record.task_id = ctx.get("task_id")

        # Merge static extras (lowest priority) and dynamic context fields
        # (highest priority). Don't overwrite reserved LogRecord attributes.
        for key, value in {**self._extra, **ctx}.items():
            if key in _RESERVED_LOGRECORD_ATTRS:
                continue
            setattr(record, key, value)

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
        "pid",
        "task_id",
    }
)
