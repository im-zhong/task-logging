"""Tests for the `@log_func_call` decorator.

Two usage forms covered: bare functions and instance methods. Both should
emit ENTER/EXIT/RAISE records via stdlib logging without any per-instance
plumbing on the class side.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from task_logging import JsonFormatter, TaskLogFilter, log_func_call


@pytest.fixture
def buf() -> io.StringIO:
    return io.StringIO()


@pytest.fixture(autouse=True)
def _wired_root(buf: io.StringIO) -> Iterator[None]:
    """Install the canonical handler chain on the root logger for one test.

    We snapshot+restore the root logger so the test stays isolated even if
    pytest or a sibling test left handlers in place.
    """
    root = logging.getLogger()
    prior_level = root.level
    prior_handlers = list(root.handlers)
    for h in prior_handlers:
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TaskLogFilter())
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


def test_log_decorator_on_plain_function(buf: io.StringIO) -> None:
    @log_func_call()
    def add(x: int, y: int) -> int:
        return x + y

    assert add(1, 2) == 3
    output = buf.getvalue()
    assert "ENTER" in output
    assert "EXIT" in output
    assert "add" in output


def test_log_decorator_in_class(buf: io.StringIO) -> None:
    class MyClass:
        @log_func_call()
        def add(self, x: int, y: int) -> int:
            return x + y

    assert MyClass().add(1, 2) == 3
    output = buf.getvalue()
    # __qualname__ surfaces "Class.method" so methods are distinguishable
    # from plain functions in the logs.
    assert "MyClass.add" in output
    assert "ENTER" in output
    assert "EXIT" in output


def test_log_decorator_logs_raise_and_reraises(buf: io.StringIO) -> None:
    @log_func_call()
    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        boom()

    output = buf.getvalue()
    assert "ENTER" in output
    assert "RAISE" in output
    # No EXIT for the failing call — RAISE replaces it.
    assert "EXIT" not in output
