"""Unit tests for `JsonFormatter`, exercised directly against `LogRecord`.

These tests construct records by hand (no handler, no filter, no root
logger) so failures point at the formatter itself, not at the wiring
around it. Integration coverage of the full pipeline lives in
`test_task_logging.py`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from types import TracebackType

from task_logging import JsonFormatter


def _make_record(
    *,
    name: str = "biz",
    level: int = logging.INFO,
    msg: str = "hi",
    args: tuple[object, ...] = (),
    exc_info: (
        tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None]
        | None
    ) = None,
) -> logging.LogRecord:
    """Build a record the way stdlib's Logger._log would."""
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def test_format_emits_one_line_of_json() -> None:
    record = _make_record(msg="hello")
    line = JsonFormatter().format(record)

    # Single-line JSON is the contract Alloy / `jq` rely on.
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["levelname"] == "INFO"
    assert payload["name"] == "biz"
    assert payload["exc_info"] is None


def test_message_substitutes_args_lazily() -> None:
    """`message` is `record.getMessage()`, with %-args already applied."""
    record = _make_record(msg="task=%s code=%d", args=("abc", 42))
    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "task=abc code=42"
    # Raw msg / args are intentionally dropped — the substituted form is
    # what consumers want, and shipping the un-substituted form alongside
    # would just bloat each line.
    assert "msg" not in payload
    assert "args" not in payload


def test_dropped_stdlib_fields_are_absent() -> None:
    """Redundant / internal LogRecord attrs are filtered out of the JSON."""
    record = _make_record()
    payload = json.loads(JsonFormatter().format(record))

    for absent in (
        "msg",
        "args",
        "exc_text",
        "filename",
        "levelno",
        "msecs",
        "processName",
        "relativeCreated",
        "stack_info",
    ):
        assert absent not in payload, f"{absent} should be dropped"

    # And the keys we DO keep are present.
    for present in (
        "message",
        "levelname",
        "name",
        "module",
        "funcName",
        "pathname",
        "lineno",
        "created",
        "asctime",  # ISO-8601 string for human readers; see formatters.py
        "process",
        "thread",
        "exc_info",
    ):
        assert present in payload, f"{present} should be kept"


def test_asctime_is_iso8601_utc_matching_created() -> None:
    """`asctime` is the ISO-8601 UTC string for the same instant as `created`.

    Pinned because:
      - The 'Z' suffix matters — RFC 3339 / ISO 8601, no offset ambiguity.
      - Microsecond precision must round-trip cleanly with `created` (a
        Unix float with microsecond resolution).
      - Other tools (Alloy's `format = "RFC3339Nano"`, jq's `fromdateiso8601`)
        rely on this exact shape.
    """
    record = _make_record()
    payload = json.loads(JsonFormatter().format(record))

    asctime = payload["asctime"]
    assert asctime.endswith("Z"), f"expected trailing Z, got {asctime!r}"
    assert "T" in asctime, f"expected ISO date-T-time separator, got {asctime!r}"

    # Round-trip: parsing `asctime` back should give the same instant as
    # `created`, to microsecond precision. (Sub-microsecond drift can
    # sneak in via float rounding, hence the 1e-6 tolerance.)
    parsed = datetime.fromisoformat(asctime)
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    assert abs(parsed.timestamp() - payload["created"]) < 1e-6


def test_extra_attrs_on_record_are_emitted() -> None:
    """Anything stamped onto record.__dict__ rides through to the JSON."""
    record = _make_record()
    record.task_id = "t-1"
    record.service = "Billing"
    record.user_id = "u-7"

    payload = json.loads(JsonFormatter().format(record))
    assert payload["task_id"] == "t-1"
    assert payload["service"] == "Billing"
    assert payload["user_id"] == "u-7"


def test_underscore_prefixed_attrs_are_dropped() -> None:
    """Private attrs (`_*`) are treated as internal bookkeeping, not output."""
    record = _make_record()
    record._task_logging_tag = "task_logging.handler"  # noqa: SLF001
    record._private = "hide me"  # noqa: SLF001

    payload = json.loads(JsonFormatter().format(record))
    assert "_task_logging_tag" not in payload
    assert "_private" not in payload


def test_exc_info_is_rendered_as_structured_dict() -> None:
    """The raw exc_info tuple is replaced with {name, details, stack_trace, locals_dict}."""

    def divide(a: int, b: int) -> float:
        return a / b

    try:
        divide(1, 0)
    except ZeroDivisionError:
        record = _make_record(msg="boom", exc_info=sys.exc_info())

    payload = json.loads(JsonFormatter().format(record))
    exc = payload["exc_info"]
    assert exc["name"] == "ZeroDivisionError"
    assert exc["details"] == "division by zero"
    assert "Traceback" in exc["stack_trace"]
    # Locals come from the DEEPEST frame — `divide`, where the `/` ran.
    assert exc["locals_dict"] == {"a": "1", "b": "0"}


def test_capture_locals_false_yields_empty_locals_dict() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = _make_record(msg="oops", exc_info=sys.exc_info())

    payload = json.loads(JsonFormatter(capture_locals=False).format(record))
    assert payload["exc_info"]["name"] == "RuntimeError"
    assert payload["exc_info"]["locals_dict"] == {}


def test_non_ascii_message_is_emitted_literally() -> None:
    """ensure_ascii=False keeps CJK / emoji readable instead of `\\uXXXX`-escaped."""
    record = _make_record(msg="订单已创建 🎉")
    line = JsonFormatter().format(record)

    assert "订单已创建" in line
    assert "🎉" in line
    # The escape form must NOT be present — that's what ensure_ascii=False buys us.
    assert "\\u8ba2" not in line


def test_unrepr_able_value_falls_back_to_default_serializer() -> None:
    """Values that aren't JSON-able go through `_json_default` (`repr`)."""

    class Weird:
        def __repr__(self) -> str:
            return "<Weird sentinel>"

    record = _make_record()
    record.thing = Weird()

    payload = json.loads(JsonFormatter().format(record))
    assert payload["thing"] == "<Weird sentinel>"
