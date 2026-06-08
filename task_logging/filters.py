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
    is local and reversible — `root.removeHandler(h)` undoes it.

Why we COPY the record instead of mutating it:
    The cookbook's "Imparting contextual information in handlers" pattern.
    A LogRecord is passed by reference to every handler in the chain, so
    if we mutated it, our enrichment would leak into any other handler the
    host application has installed (e.g. a Sentry handler, or a debug
    StreamHandler the user added). Returning a fresh copy from filter()
    confines the enrichment to OUR handler's chain. The cost is one shallow
    copy per record per handler, which is negligible.

    stdlib supports this directly: a Filter's `filter()` may return a
    LogRecord (instead of a bool) and stdlib's Filterer.filter will use
    that record downstream in this handler's chain only. Note: this
    "return a LogRecord to substitute" semantic landed in Python 3.12
    (see the "Changed in version 3.12" note on `logging.Filter.filter`).
    The library's `requires-python = ">=3.12"` is partly because of this —
    on 3.10/3.11 the returned record would be treated as a truthy bool and
    our enrichment would silently vanish.

Why we DON'T protect stdlib field names from being overwritten:
    Earlier revisions had a `_RESERVED_LOGRECORD_ATTRS` set that silently
    dropped user keys colliding with stdlib LogRecord attribute names
    (`msg`, `levelname`, `name`, ...). We removed it. Reasons:

      - Silent drop is bad UX. User binds `task_log_context({"name": "X"})`,
        sees no `name=X` in Grafana, has no idea why. With no protection
        they see exactly what they bound, immediately learn to pick a
        different key.
      - Damage is contained. We copy the record per handler-call, so any
        weirdness only affects this one record's JSON. If the override
        causes `record.getMessage()` to raise (e.g. by clobbering `msg`
        in a way incompatible with `args`), stdlib's `Handler.emit`
        catches the formatter exception via `handleError`, prints a
        traceback to stderr, and drops the line. The user's program is
        unaffected.
      - The library doesn't pretend to know better than the user. If you
        ask for `levelname=URGENT` you get `levelname=URGENT`.

Why we DON'T auto-detect anything (no hostname, no env, no nothing):
    Earlier revisions auto-stamped `record.hostname = socket.gethostname()`
    as a "convenience." Removed too. The amount of code, comments, and
    override-precedence logic supporting "auto-detect one thing but let
    the user override it" was wildly out of proportion to the value.
    Hostname is one line of `socket.gethostname()` the user can put in
    `global_log_attrs` themselves; the deployment platform usually adds a
    better identifier (Kubernetes pod name, Docker container label) anyway.
    Keep the library to the principle: users decide what's in the record.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .context import get_task_log_attrs


class TaskLogFilter(logging.Filter):
    """Attach task log attrs to every `LogRecord` on its way to a handler.

    *** Attach this filter to a HANDLER, not a Logger. ***

    Logger-level filters only see records emitted *directly* on that logger;
    they do NOT see records that propagate up from child loggers. So a filter
    on the root logger would NOT enrich records emitted by `urllib3` or
    `boto3`. Handler-level filters see every record that reaches the handler
    via propagation. That's the behaviour that makes "tag third-party logs
    automatically" possible. See the module docstring and
    docs/design/stdlib-logging-primer.md for the full story.

    Canonical wiring::

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(TaskLogFilter(global_log_attrs={"service": "x"}))
        logging.getLogger().addHandler(handler)

    Returns a COPY of the record with the enrichment applied; never drops
    records, never mutates the original.

    Sources merged onto each record (lower → higher priority; later writes
    overwrite earlier):
        - `global_log_attrs` (passed at filter construction)
        - active `task_log_context` attrs (whatever's in scope right now)

    The library does NOT protect stdlib LogRecord field names. If a user
    binds e.g. `task_log_context({"name": "X"})`, `record.name` becomes
    "X" — that's what they asked for. See module docstring for why.
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

        # Merge global (lowest of the user-supplied) and the currently-active
        # task_log_context attrs (highest). Each value lands as an attribute
        # on the record and rides through the formatter into the JSON output.
        for key, value in {**self._global_log_attrs, **get_task_log_attrs()}.items():
            setattr(record, key, value)

        # Returning a LogRecord (not a bool) is stdlib-blessed since 3.12:
        # stdlib's Filterer.filter accepts either, and a returned record
        # replaces the original for THIS handler's chain only. Other handlers
        # on the same record see the unmodified original.
        return record
