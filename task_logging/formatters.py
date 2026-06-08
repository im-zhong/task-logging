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

# Stdlib LogRecord attributes we deliberately exclude from the JSON output.
# Everything else on record.__dict__ — including stdlib's own fields and any
# extras stamped on by filters / `extra=` / task_context — is emitted as-is.
#
# Reasons each is dropped (see docs/design/json-schema.md "Group 5"):
#   args / msg            -- already substituted into `message`
#   msecs / relativeCreated / asctime
#                         -- redundant with the float `created`
#   levelno               -- redundant with `levelname`
#   exc_info              -- handled specially: the raw tuple is replaced
#                            with our rendered {name, details, stack_trace,
#                            locals_dict} object under the same key
#   exc_text / stack_info -- already encoded inside `exc_info.stack_trace`
#   processName           -- almost always "MainProcess", noise
#   filename              -- redundant with `pathname` (basename only)
#   taskName              -- asyncio task name, conflicts semantically with
#                            our `task_id`
#
# Reference: https://docs.python.org/3/library/logging.html#logrecord-attributes
_DROPPED_LOGRECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "exc_info",  # rewritten under the same key with our structured shape
        "exc_text",
        "filename",
        "levelno",
        "msecs",
        "msg",
        "processName",
        "relativeCreated",
        "stack_info",
        # "taskName",  # Python 3.12+
    }
)


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one line of JSON.

    The formatter emits **every attribute on `record.__dict__`** — stdlib's
    built-in fields, anything stamped on by filters (service, env, hostname,
    task_id), and any user fields bound via `task_context(**extra)` — minus
    a small drop-list of redundant / internal attributes
    (`_DROPPED_LOGRECORD_ATTRS`). Two values are computed specially:

      - `message`  – `record.getMessage()` (lazy %-formatting)
      - `exc_info` – `record.exc_info` rendered into a structured object
                     (name / details / stack_trace / locals_dict)

    JSON keys mirror stdlib LogRecord attribute names exactly. The official
    attribute reference is also our schema reference:
    https://docs.python.org/3/library/logging.html#logrecord-attributes

    A typical record looks like::

        {
          "created":    1717839622.503112,
          "levelname":  "INFO",
          "name":       "billing.settlement",
          "message":    "charging account",
          "process":    4321,
          "thread":     140234567890,
          "threadName": "MainThread",
          "module":     "settlement",
          "funcName":   "charge",
          "pathname":   "/app/billing/settlement.py",
          "lineno":     87,
          "exc_info":   null,
          "service":    "Billing",         // added by TaskContextFilter
          "env":        "prod",
          "hostname":   "worker-7",
          "task_id":    "task-42",
          "user_id":    "u-1"              // user extra via task_context
        }

    For the rationale behind each key name — what is kept, what is dropped,
    and the stability promise — see docs/design/json-schema.md.
    """

    def __init__(self, *, capture_locals: bool = True) -> None:
        super().__init__()
        self._capture_locals = capture_locals

    def format(self, record: logging.LogRecord) -> str:
        # We emit EVERYTHING on record.__dict__ except a small drop-list,
        # rather than enumerating each key by hand. Reasons:
        #   - record.__dict__ already holds stdlib's own attributes AND any
        #     fields stamped on by filters / `extra=` / task_context, all
        #     in one place. One loop, no duplication, no "did I forget to
        #     add the new field to the dict?" maintenance burden.
        #   - Adding a stdlib attribute we already wanted (or accepting a
        #     new field a user binds via task_context) is automatic — no
        #     code change needed.
        #   - The few stdlib attrs we DO want to suppress (raw msg/args,
        #     redundant timestamps, internal exc_text, ...) are listed once,
        #     positively, in _DROPPED_LOGRECORD_ATTRS above.
        #
        # Stability note: top-level keys are public API. Alloy's stage.json
        # in the README references them by name. Renaming a key here is a
        # breaking change for every Alloy config in the wild. Add freely;
        # don't rename/remove without a major bump.
        payload: dict[str, Any] = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _DROPPED_LOGRECORD_ATTRS and not key.startswith("_")
        }

        # ----- two specials, for two DIFFERENT reasons -----
        # See docs/design/json-schema.md "Why message and exc_info are special"
        # for the longer treatment, including alternatives we rejected.

        # `message` is special because the VALUE ISN'T ON record.__dict__ YET.
        # stdlib delays %-substitution until something asks for it, so the
        # record carries `msg` (format string) and `args` (tuple) but no
        # `message`. record.getMessage() does the substitution. We compute
        # it here for the same reason stdlib's own Formatter does — and the
        # raw `msg` / `args` are in _DROPPED_LOGRECORD_ATTRS to avoid
        # bloating each line with the un-substituted form.
        payload["message"] = record.getMessage()

        # `exc_info` is special because the VALUE ISN'T JSON-SERIALISABLE.
        # On a LogRecord it's the raw sys.exc_info() tuple — (type, value,
        # tb) — none of whose elements are JSON-encodable. Letting it fall
        # through json.dumps(default=...) would produce a useless
        # "(<class 'X'>, X(...), <traceback at 0x...>)" string with no
        # stack trace and no locals. We render it into a structured object
        # ({name, details, stack_trace, locals_dict}) under the SAME key
        # so the JSON name still matches the LogRecord name; the raw tuple
        # is dropped via _DROPPED_LOGRECORD_ATTRS to make room.
        payload["exc_info"] = (
            self._render_exc_info(record.exc_info) if record.exc_info else None
        )

        return json.dumps(payload, default=_json_default, ensure_ascii=False)

    def _render_exc_info(
        self,
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None]
        | bool,
    ) -> dict[str, Any] | None:
        """Turn a raw exc_info tuple into a JSON-serialisable dict.

        This is JsonFormatter's analogue to stdlib's `Formatter.formatException`,
        but it returns a `dict` (for embedding in our JSON payload) rather
        than a `str` (for appending to a text line). We deliberately do NOT
        override `formatException`:

          - stdlib's `format()` calls `formatException` only as part of its
            text-concatenation pipeline; we never enter that pipeline, so
            an override would never be invoked.
          - `formatException` is contractually a `str`-returning method;
            ours returns a `dict`.
          - stdlib caches `formatException`'s result on `record.exc_text`,
            which would then be picked up by any OTHER handler's stdlib
            formatter on the same record and silently used as if it were
            a normal traceback string.

        Subclasses that want to customise exception rendering (drop locals,
        anonymise paths, redact secrets, ...) should override THIS method.

        See docs/design/json-schema.md "Why we don't override formatException"
        for the longer treatment.
        """
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
