"""Stdlib LogRecord attribute names — single source of truth.

Both `TaskContextFilter` (filters.py) and `JsonFormatter` (formatters.py) need
to know which attribute names belong to stdlib's `LogRecord`, but for
different reasons:

  - The filter doesn't let user-provided keys (via `task_context(**extra)` or
    `static_fields=`) overwrite stdlib attrs — a typo like
    `task_context(msg="...")` would otherwise corrupt `record.msg`.
  - The formatter curates which stdlib attrs to emit in JSON; the rest are
    internal/redundant and dropped via `_DROPPED_LOGRECORD_ATTRS`.

Hand-listing the stdlib names in BOTH files would let them drift (e.g. one
version of this codebase had `taskName` in the formatter's drop list but
not in the filter's reserved set, so `task_context(taskName="x")` would
silently corrupt records). One source of truth, both files import from
here.

Reference: https://docs.python.org/3/library/logging.html#logrecord-attributes
"""

from __future__ import annotations

# All public LogRecord attributes documented by stdlib. Keep in sync with
# the docs page above when bumping Python version support.
STDLIB_LOGRECORD_ATTRS: frozenset[str] = frozenset(
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
        "taskName",  # added in Python 3.12
    }
)
