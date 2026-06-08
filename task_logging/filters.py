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

Why we COPY the record instead of mutating it:
    The cookbook's "Imparting contextual information in handlers" pattern.
    A LogRecord is passed by reference to every handler in the chain, so
    if we mutated it, our enrichment would leak into any other handler the
    host application has installed (e.g. a Sentry handler, or a debug
    StreamHandler the user added). Returning a fresh copy from filter()
    confines the enrichment to OUR handler's chain. The cost is one shallow
    copy per record per handler, which is negligible.

    stdlib supports this directly: a Filter's `filter()` may return a
    LogRecord (instead of a bool) and stdlib's Filterer.filter will use that
    record downstream in this handler's chain only. See cpython
    Lib/logging/__init__.py:Filterer.filter.

See docs/design/task-context.md and docs/design/stdlib-logging-primer.md (§4).
"""

from __future__ import annotations

import copy
import logging
import socket
from typing import Any

from ._logrecord import STDLIB_LOGRECORD_ATTRS
from .context import get_task_context

# Cached at module import. hostname is effectively immutable for the life
# of the process; PID we don't need to stamp because stdlib already sets
# `record.process` for us.
_HOSTNAME = socket.gethostname()

# Attribute names we add ourselves. Plus the stdlib-built-in names (imported
# from _logrecord), this is the full set of "don't let user-supplied keys
# overwrite these."
_FIELDS_WE_ADD: frozenset[str] = frozenset(
    {
        "service",
        "env",
        "hostname",
        "task_id",
    }
)

# Reserved keys: anything a user binds via `task_context(**extra)` or
# `static_fields=` is silently dropped if its name collides with one of
# these. We protect (a) every documented stdlib LogRecord attribute, so
# `task_context(msg="x")` doesn't corrupt `record.msg`, and (b) the four
# fields this filter writes itself, so a stale extras key can't unset
# them mid-mutation.
_RESERVED_LOGRECORD_ATTRS: frozenset[str] = STDLIB_LOGRECORD_ATTRS | _FIELDS_WE_ADD


class TaskContextFilter(logging.Filter):
    """Attach task context + service metadata to every `LogRecord`.

    The filter returns a COPY of the record with the enrichment applied;
    it never drops records and never mutates the original. See the module
    docstring for why we copy instead of mutating.

    Fields written onto the (copied) record — all NEW, none of these exist
    on a stock LogRecord; PID/process info comes from stdlib's
    `record.process`:
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

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        # Shallow-copy is enough: LogRecord's interesting state is its
        # __dict__, which copy.copy duplicates. The expensive bits — exc_info
        # tuples, stack frames — are immutable from our perspective and shared
        # safely by reference.
        record = copy.copy(record)

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
        # (highest priority). Skip reserved keys so a well-meaning user
        # can't accidentally clobber `record.msg`, `record.levelname`, or
        # any of our own four fields by binding e.g. task_context(msg="...").
        for key, value in {**self._extra, **ctx}.items():
            if key in _RESERVED_LOGRECORD_ATTRS:
                continue
            setattr(record, key, value)

        # Returning a LogRecord (not a bool) is stdlib-blessed: stdlib's
        # Filterer.filter accepts either, and a returned record replaces the
        # original for THIS handler's chain only. Other handlers on the same
        # record see the unmodified original.
        return record
