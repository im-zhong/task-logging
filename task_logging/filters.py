"""Logging filter that enriches `LogRecord` with task log attrs.

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
    is local and reversible (setup_task_logging() can replace its own
    handlers).

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
    record downstream in this handler's chain only.

Merge order (lowest → highest priority; later writes overwrite earlier):
    1. auto-detected     (currently just hostname)
    2. global_log_attrs  (passed to setup_task_logging once at startup)
    3. local_log_attrs   (whatever the active task_log_context bound)
    The user always wins over auto-detection; nested task_log_context blocks
    win over global setup. This matches the principle "users decide what's
    in the record."

See docs/design/task-context.md and docs/design/stdlib-logging-primer.md (§4).
"""

from __future__ import annotations

import copy
import logging
import socket
from typing import Any

from ._logrecord import STDLIB_LOGRECORD_ATTRS
from .context import get_task_log_attrs

# Cached at module import. hostname is effectively immutable for the life
# of the process; PID we don't need to stamp because stdlib already sets
# `record.process` for us.
_HOSTNAME = socket.gethostname()

# Attribute names this filter writes itself with auto-detected values. The
# library is deliberately stingy here — every name added to this set is one
# more domain assumption baked in. Currently just hostname; everything else
# (service, env, task_id, ...) is supplied by the user.
#
# Note: these names are NOT in _RESERVED_LOGRECORD_ATTRS. Users are
# explicitly allowed to override auto-detected values — that's the whole
# point of "user-given keys override auto-detected." We just write our
# auto value first, then let the merge loop overwrite it if the user
# supplied their own.
_LOG_ATTRS_WE_ADD: frozenset[str] = frozenset({"hostname"})

# Reserved keys: anything a user binds via `task_log_context({...})` or
# `setup_task_logging(global_log_attrs={...})` is silently dropped if its
# name collides with one of these. We protect every documented stdlib
# LogRecord attribute (so e.g. `task_log_context({"msg": "..."})` doesn't
# corrupt `record.msg` and break formatting). Auto-detected names are NOT
# reserved — they're meant to be overridable.
_RESERVED_LOGRECORD_ATTRS: frozenset[str] = STDLIB_LOGRECORD_ATTRS


class TaskLogFilter(logging.Filter):
    """Attach task log attrs to every `LogRecord` on its way to a handler.

    Returns a COPY of the record with the enrichment applied; never drops
    records, never mutates the original. See module docstring for why.

    Sources merged onto each record (lowest → highest priority):
        - `record.hostname`        (auto-detected at module import)
        - `global_log_attrs`       (from `setup_task_logging`)
        - active `task_log_context` attrs (from any enclosing context)

    Any user key colliding with a documented stdlib LogRecord attribute
    (e.g. `msg`, `levelname`, `created`) is silently dropped to protect
    record integrity. Auto-detected names like `hostname` are NOT reserved
    — users may override them by supplying the same key.
    """

    def __init__(self, global_log_attrs: dict[str, Any] | None = None) -> None:
        super().__init__()
        # Defensive copy so caller mutations after construction don't bleed in.
        self._global_log_attrs: dict[str, Any] = (
            dict(global_log_attrs) if global_log_attrs else {}
        )

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        # Shallow-copy is enough: LogRecord's interesting state is its
        # __dict__, which copy.copy duplicates. The expensive bits — exc_info
        # tuples, stack frames — are immutable from our perspective and
        # shared safely by reference.
        record = copy.copy(record)

        # 1. Auto-detected. Lowest priority; user can override.
        record.hostname = _HOSTNAME

        # 2 & 3. Merge global_log_attrs (lowest of the user-supplied) and
        # the currently-active task_log_context attrs (highest). Skip
        # reserved keys so a well-meaning user can't accidentally clobber
        # `record.msg`, `record.levelname`, or `record.hostname`.
        local_attrs = get_task_log_attrs()
        for key, value in {**self._global_log_attrs, **local_attrs}.items():
            if key in _RESERVED_LOGRECORD_ATTRS:
                continue
            setattr(record, key, value)

        # Returning a LogRecord (not a bool) is stdlib-blessed: stdlib's
        # Filterer.filter accepts either, and a returned record replaces the
        # original for THIS handler's chain only. Other handlers on the same
        # record see the unmodified original.
        return record
