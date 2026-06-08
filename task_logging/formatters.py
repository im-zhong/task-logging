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

Why JSON keys MIRROR stdlib LogRecord attribute names:
    `levelname`, `pathname`, `funcName`, `lineno`, `created`, `process`,
    `thread`, `threadName` — all named exactly as stdlib's logging.LogRecord
    names them. So:
      - anyone who knows stdlib logging already knows our JSON schema
      - the docs page for LogRecord IS our schema reference
        (https://docs.python.org/3/library/logging.html#logrecord-attributes)
      - we don't pick fights with conventions every consumer already knows
        ("why is funcName called func here?")
    The earlier version of this formatter renamed several keys (level,
    logger, msg, func, file, line, ts, ...) for "JSON niceness." That cost
    more than it saved — every reader of the JSON had to learn our renames
    on top of stdlib's names. Keys we ADD that don't exist on a LogRecord
    (service, env, hostname, task_id) are our own concepts and don't have
    stdlib counterparts to match.

See docs/design/why-json-logs.md for the format choice and
docs/design/json-schema.md for the per-key rationale and stability promise.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from types import TracebackType
from typing import Any

# https://docs.python.org/3/library/logging.html#logrecord-attributes
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
        # mirrored from stdlib LogRecord
        "created",
        "levelname",
        "name",
        "message",
        "process",
        "thread",
        "threadName",
        "module",
        "funcName",
        "pathname",
        "lineno",
        "exc_info",
        # added by us (no stdlib equivalent)
        "service",
        "env",
        "hostname",
        "task_id",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one line of JSON.

    Output schema (keys are stable; consumers may rely on them).

    Mirrored from stdlib LogRecord — names match the official attribute
    table at https://docs.python.org/3/library/logging.html#logrecord-attributes ::

        {
          "created":    1717839622.503112,    # Unix timestamp (float)
          "levelname":  "INFO" | "ERROR" | ...,
          "name":       record.name,           # the logger name
          "message":    the formatted message  (record.getMessage())
          "process":    record.process,        # PID
          "thread":     record.thread,         # thread id
          "threadName": record.threadName,
          "module":     record.module,
          "funcName":   record.funcName,
          "pathname":   record.pathname,
          "lineno":     record.lineno,
          "exc_info":   {name, details, stack_trace, locals_dict} | null,

          # added by us (no stdlib equivalent on LogRecord)
          "service":    set by TaskContextFilter,
          "env":        set by TaskContextFilter (may be null),
          "hostname":   machine hostname,
          "task_id":    current task id (may be null),
          ...:          any extra fields you bound via task_context(**extra)
        }

    For the rationale behind each key name — what was kept, what was dropped,
    and the stability promise — see docs/design/json-schema.md.
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
            # `created` is the raw Unix timestamp (matches LogRecord.created),
            # used by Alloy as the canonical timestamp via
            # `stage.timestamp { format = "Unix" }` — far more accurate than
            # "the time Alloy happened to read the line."
            "created": record.created,
            "levelname": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "process": record.process,
            "thread": record.thread,
            "threadName": record.threadName,
            "module": record.module,
            "funcName": record.funcName,
            "pathname": record.pathname,
            "lineno": record.lineno,
            # added by us via TaskContextFilter — no stdlib equivalent
            "service": getattr(record, "service", None),
            "env": getattr(record, "env", None),
            "hostname": getattr(record, "hostname", None),
            "task_id": getattr(record, "task_id", None),
        }

        # Exception info, if any. Either set via logger.exception() /
        # exc_info=True or because the record was emitted from inside an
        # except block. We render the raw exc_info tuple into a structured
        # object and write it under the same key stdlib uses on the record
        # (`exc_info`), so the JSON name matches the LogRecord name.
        if record.exc_info:
            payload["exc_info"] = self._render_exc_info(record.exc_info)
        else:
            payload["exc_info"] = None

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
