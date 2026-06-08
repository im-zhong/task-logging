"""Smoke tests for `task_log_context` over a manually-wired stdlib pipeline.

The library deliberately ships no `setup_task_logging` wrapper — wiring is
six lines of stdlib that users (and tests) write themselves. These tests
exercise the context-propagation primitive against that canonical wiring.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from task_logging import JsonFormatter, TaskLogFilter, task_log_context


@pytest.fixture
def buf() -> io.StringIO:
    return io.StringIO()


@pytest.fixture(autouse=True)
def _wired_root(buf: io.StringIO) -> Iterator[None]:
    """Install the canonical handler chain on the root logger for one test."""
    root = logging.getLogger()
    prior_level = root.level
    prior_handlers = list(root.handlers)
    for h in prior_handlers:
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(
        TaskLogFilter(global_log_attrs={"service": "task-logging", "env": "test"})
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()
        for h in prior_handlers:
            root.addHandler(h)
        root.setLevel(prior_level)


def _records(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_log_context(buf: io.StringIO) -> None:
    logger = logging.getLogger(__name__)

    logger.info("test log context")
    with task_log_context({"task_id": "123"}):
        logger.info("test log context")

    records = _records(buf)
    assert [r.get("task_id") for r in records] == [None, "123"]
    # Global attrs ride along on every record.
    assert all(r["service"] == "task-logging" and r["env"] == "test" for r in records)


def _raise_zero_division_error() -> tuple[int, int, float]:
    a = 1
    b = 2
    return a, b, 1 / 0


def test_exception_log(buf: io.StringIO) -> None:
    logger = logging.getLogger(__name__)

    # logger.info() in an except block does NOT capture exc_info — stdlib
    # only reads sys.exc_info() when the call's `exc_info` arg is truthy.
    # logger.exception() flips that bit, which is the difference under test.
    try:
        _raise_zero_division_error()
    except ZeroDivisionError:
        logger.info("ExceptionDetails")
        logger.exception("ExceptionDetails")

    info_record, exc_record = _records(buf)
    assert info_record["exc_info"] is None
    assert exc_record["exc_info"] is not None
    assert exc_record["exc_info"]["name"] == "ZeroDivisionError"
    assert exc_record["exc_info"]["details"] == "division by zero"
    # Locals at the deepest frame — the snapshot the post-mortem view shows.
    assert exc_record["exc_info"]["locals_dict"] == {"a": "1", "b": "2"}
