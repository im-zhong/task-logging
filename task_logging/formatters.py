"""JSON formatter for stdlib `logging`, designed to be parsed by Grafana Alloy.

Each `LogRecord` is rendered as a single-line JSON object with a stable schema
so Alloy's `stage.json` can extract fields reliably.

Why JSON (not plain text, not logfmt):
    Loki accepts arbitrary text — JSON is not required. We choose it because
    it gives us:
      - typed, named fields in LogQL (`| json | task_id="..."` instead of
        substring grep)
      - clean Alloy promotion: low-cardinality fields (level) become Loki
        labels; high-cardinality fields (task_id) become structured metadata,
        so Loki indexes the cheap stuff and stays queryable on the rest
      - schema growth without breaking parsers — adding a key never breaks
        consumers, unlike a regex over a fixed column order
      - native nesting for stack traces / locals snapshots
      - one durable contract every downstream tool agrees on (Alloy, jq,
        future Splunk sidecars, ...). Plain text means every consumer ships
        its own regex and they silently disagree at the edges.
    Cost: humans can't read raw JSON easily, so the console handler in
    setup_logging defaults to a human formatter; only the *file* (which Alloy
    reads, not you) is JSON.

See docs/design/why-json-logs.md for the full discussion.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

# LogRecord attributes that are bookkeeping / already encoded elsewhere — we
# do NOT spill them into the JSON payload as "extras".
_BUILTIN_LOGRECORD_ATTRS: frozenset[str] = frozenset(
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

# Top-level fields the formatter writes itself.
_FORMATTER_OWN_KEYS: frozenset[str] = frozenset(
    {
        "ts",
        "level",
        "logger",
        "msg",
        "service",
        "env",
        "hostname",
        "pid",
        "thread",
        "thread_name",
        "task_id",
        "module",
        "func",
        "file",
        "line",
        "exc",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one line of JSON.

    Output schema (keys are stable; consumers may rely on them):

        {
          "ts":          ISO-8601 UTC timestamp with microseconds
          "level":       "INFO" / "ERROR" / ...
          "logger":      record.name
          "msg":         the formatted message
          "service":     set by TaskContextFilter
          "env":         set by TaskContextFilter (may be null)
          "hostname":    machine hostname
          "pid":         process id
          "thread":      thread id
          "thread_name": thread name
          "task_id":     current task id (may be null)
          "module":      record.module
          "func":        record.funcName
          "file":        record.pathname
          "line":        record.lineno
          "exc":         {name, details, stack_trace, locals_dict} or null
          ...:           any extra fields you bound via task_context(**extra)
        }

    For the rationale behind each key name, what was renamed from stdlib
    LogRecord, what was dropped, and the stability promise, see
    docs/design/json-schema.md.
    """

    def __init__(self, *, capture_locals: bool = True) -> None:
        super().__init__()
        self._capture_locals = capture_locals

    def format(self, record: logging.LogRecord) -> str:
        # Top-level keys are kept STABLE — Alloy's stage.json config in the
        # README references them by name. Renaming a key here is a breaking
        # change for every Alloy config in the wild. Add new keys freely;
        # don't rename or remove existing ones without a major bump.
        payload: dict[str, Any] = {
            # ts is the application's timestamp, used by Alloy as the canonical
            # timestamp via stage.timestamp — far more accurate than "the time
            # Alloy happened to read the line."
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": getattr(record, "service", None),
            "env": getattr(record, "env", None),
            "hostname": getattr(record, "hostname", None),
            "pid": getattr(record, "pid", None),
            "thread": record.thread,
            "thread_name": record.threadName,
            "task_id": getattr(record, "task_id", None),
            "module": record.module,
            "func": record.funcName,
            "file": record.pathname,
            "line": record.lineno,
        }

        # Exception info, if any. Either set via logger.exception()/exc_info=True
        # or because the record was emitted from inside an except block.
        if record.exc_info:
            payload["exc"] = self._render_exc_info(record.exc_info)
        else:
            payload["exc"] = None

        # Anything extra that filters / `extra=` / task_context bound onto the
        # record gets merged in at the top level. Keys collisions: payload wins
        # for our own keys; the user's extras win otherwise.
        for key, value in record.__dict__.items():
            if key in _BUILTIN_LOGRECORD_ATTRS or key in _FORMATTER_OWN_KEYS:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value

        return json.dumps(payload, default=_json_default, ensure_ascii=False)

    def _render_exc_info(
        self,
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None]
        | bool,
    ) -> dict[str, Any] | None:
        # logging may pass `True` to mean "use sys.exc_info()".
        if exc_info is True:
            exc_info = sys.exc_info()
        if not isinstance(exc_info, tuple):
            return None
        exc_type, exc_value, exc_tb = exc_info
        if exc_type is None or exc_value is None:
            return None

        rendered: dict[str, Any] = {
            "name": exc_type.__name__,
            "details": str(exc_value),
            "stack_trace": "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            ),
            "locals_dict": {},
        }

        if self._capture_locals and exc_tb is not None:
            # Walk to the DEEPEST frame — the one where the exception was
            # actually raised — not the outermost handler. The deepest frame's
            # locals are what you'd see in a debugger sitting on the raise
            # statement, which is the post-mortem view that's actually useful.
            deepest: TracebackType = exc_tb
            while deepest.tb_next is not None:
                deepest = deepest.tb_next
            frame_locals = deepest.tb_frame.f_locals or {}
            # repr() everything: arbitrary user values are rarely JSON-serialisable
            # (sockets, file handles, ORM objects, ...). repr is best-effort and
            # never raises for sane __repr__ implementations; _json_default
            # below is the last-resort fallback for anything pathological.
            rendered["locals_dict"] = {k: repr(v) for k, v in frame_locals.items()}

        return rendered


def _json_default(obj: Any) -> str:
    """Last-resort serialiser for values that aren't natively JSON-able."""
    try:
        return repr(obj)
    except Exception as e:  # pragma: no cover - extremely defensive
        return f"<unrepr-able: {type(obj).__name__}: {e}>"
